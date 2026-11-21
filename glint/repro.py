"""Reproduction: Hydrogen writes a Playwright test, kept only if it fails on the buggy app (G1) and the
failure screenshot shows the same defect as the recording (G1B)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from glint import config, gemini, media, workspace
from glint.gemini import VideoRef
from glint.perceive import app_source_parts, is_defect, perceive, symptom_block
from glint.schemas import GateResult, ReproSpec

log = logging.getLogger("glint.repro")

# establish_repro reports progress as (event type, data payload) per CONTRACTS.md section 9.
# The pipeline wraps this into Event objects, because it owns run_id and seq.
StageEmit = Callable[[str, dict[str, Any]], Awaitable[None]]

FAILED_ASSERTION = "failed_assertion"
FAILED_OTHER = "failed_other"
PASSED = "passed"

DETAIL_CHARS = 400
VIEWPORT = {"width": 1280, "height": 720}
EXPECT_TIMEOUT_MS = 2000
ACTION_TIMEOUT_MS = 3000

REPRO_RULES = """Repro test file contract:
- One file, `repro_test.py`, one function `def test_repro(page):` using pytest-playwright's sync `page` fixture.
- First line of the body: `page.goto(os.environ["BASE_URL"])`.
- Uses `expect(...)` assertions only. No `time.sleep`, no `wait_for_timeout` over 500 ms.
- Viewport is set by the runner, not the test."""

REPRO_PROMPT = """You write one automated browser test that reproduces a bug in a web application.

You are given a bug report (JSON) and the application's full source code. The application currently HAS the bug.
Write a Playwright test in Python that performs the user's steps and then asserts the EXPECTED (correct) behavior.
Because the bug is present, the test must FAIL now with an assertion error, and it must PASS once the bug is fixed.

{rules}

More requirements:
- Start the file with `import os` and `from playwright.sync_api import expect`.
- The app starts empty in a fresh browser profile. Create any data the steps need.
- Every action (click, fill, press, check) must target an element that exists in the app as it is now, so that
  only the final assertions can fail. Read the source to pick selectors that really exist.
- Put the assertions about the expected behavior at the end, after all the steps. Assert the specific things the
  bug report lists under "expected" (which items are visible, counts, texts). Do not assert unrelated things.
- Do not assert the buggy behavior. Do not use try/except, pytest.raises, skip, or xfail.
- Output only the Python source of the file. No explanation, no markdown.

Bug report:
{spec}
"""

CONFTEST = f'''"""Written by Glint next to the repro test. Fixes the viewport, timeouts, and disables animations."""
import pytest
from playwright.sync_api import expect

expect.set_options(timeout={EXPECT_TIMEOUT_MS})

NO_ANIMATION_JS = """
(() => {{
  const add = () => {{
    const style = document.createElement('style');
    style.textContent = '*, *::before, *::after {{ animation: none !important; transition: none !important; '
      + 'caret-color: transparent !important; scroll-behavior: auto !important; }}';
    (document.head || document.documentElement).appendChild(style);
  }};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add();
}})();
"""


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {{**browser_context_args, "viewport": {VIEWPORT!r}}}


@pytest.fixture(autouse=True)
def _glint_page_setup(page):
    page.set_default_timeout({ACTION_TIMEOUT_MS})
    page.add_init_script(NO_ANIMATION_JS)
    yield
'''


# --- writing the test ---------------------------------------------------------------------------


def strip_fences(text: str) -> str:
    """Return the code inside a markdown fence if the model wrapped its answer in one."""
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    code = match.group(1) if match else text
    return code.strip() + "\n"


def validate_repro_source(code: str) -> str | None:
    """Return a reason the source breaks the repro test contract, or None if it is acceptable."""
    if not re.search(r"^def test_repro\(page\b", code, re.MULTILINE):
        return "the file does not define test_repro(page)"
    if 'os.environ["BASE_URL"]' not in code and "os.environ['BASE_URL']" not in code:
        return 'the test does not call page.goto(os.environ["BASE_URL"])'
    if "time.sleep" in code:
        return "the test uses time.sleep, which is not allowed"
    if "expect(" not in code:
        return "the test has no expect(...) assertion"
    try:
        compile(code, "repro_test.py", "exec")
    except SyntaxError as exc:
        return f"the file has a syntax error: {exc.msg} at line {exc.lineno}"
    return None


async def write_repro(spec: ReproSpec, app_dir: Path, feedback: str | None) -> str:
    """Ask Hydrogen for the repro test source. Raises ValueError if the output breaks the file contract."""
    prompt = REPRO_PROMPT.format(rules=REPRO_RULES, spec=spec.model_dump_json(indent=2))
    parts: list = [*app_source_parts(app_dir), prompt]
    if feedback:
        parts.append(
            "Your previous test was rejected. Fix the problem below and output the full corrected file.\n"
            f"Problem: {feedback}"
        )
    raw = await gemini.hydrogen(parts)
    code = strip_fences(str(raw))
    problem = validate_repro_source(code)
    if problem:
        raise ValueError(problem)
    return code


# --- running the test ---------------------------------------------------------------------------


def classify_junit(xml_text: str) -> tuple[str, str]:
    """Classify a junit.xml report as failed_assertion, failed_other, or passed, with a short detail."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return FAILED_OTHER, f"junit.xml could not be parsed: {exc}"[:DETAIL_CHARS]
    cases = list(root.iter("testcase"))
    if not cases:
        return FAILED_OTHER, "no test was collected"
    for case in cases:
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is None:
                continue
            message = (node.get("message") or "").strip() or (node.text or "").strip()
            detail = message[:DETAIL_CHARS]
            is_assertion = tag == "failure" and re.match(r"(\w+\.)*AssertionError\b", message) is not None
            return (FAILED_ASSERTION if is_assertion else FAILED_OTHER), detail
        if case.find("skipped") is not None:
            return FAILED_OTHER, "the test was skipped"
    return PASSED, ""


def find_screenshot(out_dir: Path) -> Path | None:
    """The newest PNG that pytest-playwright wrote under out_dir, if any."""
    shots = sorted(out_dir.rglob("*.png"), key=lambda p: p.stat().st_mtime)
    return shots[-1] if shots else None


async def run_test(test_path: Path, base_url: str, out_dir: Path) -> tuple[str, str, Path | None]:
    """Run the repro test once with the contract's command. Returns (outcome, detail, screenshot)."""
    test_path = test_path.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (test_path.parent / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    junit = out_dir / "junit.xml"
    cmd = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test_path),
        "--browser", "chromium", "--screenshot", "on", "--output", str(out_dir), "--junitxml", str(junit),
    ]  # fmt: skip
    env = {**os.environ, "BASE_URL": base_url, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(test_path.parent),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=config.TIMEOUT_S["repro_attempt"])
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    if not junit.is_file():
        tail = stdout.decode("utf-8", errors="replace").strip()[-DETAIL_CHARS:]
        return FAILED_OTHER, f"pytest produced no report: {tail}", None
    outcome, detail = classify_junit(junit.read_text(encoding="utf-8", errors="replace"))
    return outcome, detail, find_screenshot(out_dir)


# --- establishing the repro (G1 + G1B) ----------------------------------------------------------


async def _emit(emit: StageEmit | None, event_type: str, data: dict[str, Any]) -> None:
    if emit is not None:
        await emit(event_type, data)


async def _attempt(
    attempt: int, video_ref: VideoRef, spec: ReproSpec, app_dir: Path, work_dir: Path, feedback: str | None
) -> tuple[str, str, Path | None, Path | None, list[GateResult]]:
    """One write-and-run attempt. Returns (outcome, detail, test_path, screenshot, gates evaluated)."""
    try:
        code = await write_repro(spec, app_dir, feedback)
    except ValueError as exc:
        return FAILED_OTHER, str(exc), None, None, []
    attempt_dir = work_dir / f"attempt_{attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    test_path = attempt_dir / "repro_test.py"
    test_path.write_text(code, encoding="utf-8")

    outcome, detail, shot = FAILED_OTHER, "", None
    async with workspace.serve(app_dir) as base_url:
        for run in range(1, config.REPRO_FAIL_RUNS + 1):
            outcome, detail, shot = await run_test(test_path, base_url, attempt_dir / f"run_{run}")
            if outcome != FAILED_ASSERTION:
                break

    if outcome != FAILED_ASSERTION:
        g1_detail = (
            "The test passed on the buggy app, so it does not reproduce the bug."
            if outcome == PASSED
            else f"The test broke before reaching its assertions: {detail}"
        )
        return outcome, detail, test_path, shot, [GateResult(gate="G1", passed=False, detail=g1_detail)]

    g1 = GateResult(
        gate="G1",
        passed=True,
        detail=f"Failed by assertion {config.REPRO_FAIL_RUNS} of {config.REPRO_FAIL_RUNS} runs: {detail}",
    )
    if shot is None:
        g1b = GateResult(gate="G1B", passed=False, detail="The failing run produced no screenshot.")
        return outcome, detail, test_path, None, [g1, g1b]
    answers = await gemini.decide(
        [
            gemini.video_block(video_ref, config.DECISION_VIDEO_RESOLUTION),
            gemini.image_block(shot),
            symptom_block(spec),
        ],
        {"g1b": config.Q_G1B},
    )
    p_yes = answers["g1b"]
    matched = p_yes >= config.TH_G1B_SYMPTOM_MATCH
    g1b = GateResult(
        gate="G1B",
        passed=matched,
        p_yes=p_yes,
        detail=(
            f"Failure screenshot matches the recording (p={p_yes:.2f})."
            if matched
            else f"Failure screenshot does not match the recording (p={p_yes:.2f}, "
            f"need {config.TH_G1B_SYMPTOM_MATCH:.2f})."
        ),
    )
    return outcome, detail, test_path, shot, [g1, g1b]


def _feedback(outcome: str, detail: str, gates: list[GateResult]) -> str:
    if outcome == PASSED:
        return (
            "The test PASSED on the buggy app. It must fail. Assert the expected (correct) behavior from the "
            "bug report, which the buggy app does not show."
        )
    if outcome == FAILED_OTHER:
        return (
            "The test did not fail with an assertion error. Every step must work on the app as it is now, and "
            f"only the final expect(...) assertions may fail. Error: {detail}"
        )
    if any(g.gate == "G1B" and not g.passed for g in gates):
        return (
            "the failure does not show the same defect as the video. Follow the steps in the bug report "
            "exactly so the page ends in the state described under 'observed'."
        )
    return detail


async def establish_repro(
    video_ref: VideoRef,
    spec: ReproSpec,
    app_dir: Path,
    bundle_dir: Path,
    emit: StageEmit | None = None,
) -> tuple[Path | None, list[GateResult]]:
    """Write and validate the repro test. Returns (bundle_dir/repro_test.py or None, the last G1/G1B results).

    On success the test is saved to bundle_dir/repro_test.py and its failing screenshot to
    bundle_dir/shots/repro_fail.png.
    """
    work_dir = bundle_dir / "repro_attempts"
    feedback: str | None = None
    gates: list[GateResult] = []
    for attempt in range(1, config.REPRO_MAX_ATTEMPTS + 1):
        test_path: Path | None = None
        shot: Path | None = None
        try:
            async with asyncio.timeout(config.TIMEOUT_S["repro_attempt"]):
                outcome, detail, test_path, shot, gates = await _attempt(
                    attempt, video_ref, spec, app_dir, work_dir, feedback
                )
        except TimeoutError:
            outcome, detail = FAILED_OTHER, "The attempt ran out of time."
            gates = [GateResult(gate="G1", passed=False, detail=detail)]

        await _emit(emit, "repro.attempt", {"attempt": attempt, "outcome": outcome, "detail": detail})
        for gate in gates:
            await _emit(emit, "gate.result", gate.model_dump())

        if gates and all(g.passed for g in gates) and test_path is not None and shot is not None:
            final_test = bundle_dir / "repro_test.py"
            shutil.copyfile(test_path, final_test)
            (bundle_dir / "conftest.py").write_text(CONFTEST, encoding="utf-8")
            shots_dir = bundle_dir / "shots"
            shots_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(shot, shots_dir / "repro_fail.png")
            return final_test, gates
        feedback = _feedback(outcome, detail, gates)
        log.info("repro attempt %d rejected: %s", attempt, feedback[:200])

    if not gates:
        gates = [GateResult(gate="G1", passed=False, detail="No valid repro test was produced.")]
    return None, gates


# --- CLI ----------------------------------------------------------------------------------------


async def _cli(video: Path, app_dir: Path) -> int:
    run_id = "repro_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_dir = Path(config.RUNS_DIR) / run_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    gemini.bind_run(run_id)

    try:
        info = await media.normalize(video, bundle_dir / "video.mp4")
    except media.InputRejected as exc:
        print(f"REJECTED_INPUT {exc.code}: {exc.message}")
        return 1
    print(f"video: {info.duration_s:.1f}s {info.width}x{info.height} audio={info.has_audio} trimmed={info.trimmed}")
    video_ref = await gemini.upload_video(Path(info.path))

    g0 = await is_defect(video_ref)
    print(f"G0: passed={g0.passed} p_yes={g0.p_yes:.3f}")
    if not g0.passed:
        print("ABSTAIN: the recording does not clearly show a defect.")
        return 1

    spec = await perceive(video_ref, app_dir, info.duration_s)
    print("spec:")
    print(spec.model_dump_json(indent=2))
    (bundle_dir / "spec.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")

    async def show(event_type: str, data: dict[str, Any]) -> None:
        if event_type == "repro.attempt":
            print(f"attempt {data['attempt']}: {data['outcome']} {data['detail'][:200]}")
        elif event_type == "gate.result":
            p_yes = "" if data["p_yes"] is None else f" p_yes={data['p_yes']:.3f}"
            print(f"  {data['gate']}: passed={data['passed']}{p_yes} {data['detail'][:200]}")

    test_path, _ = await establish_repro(video_ref, spec, app_dir, bundle_dir, show)
    usage = gemini.usage_snapshot(run_id)
    print(f"usage: calls={usage.calls} cost_usd={usage.cost_usd:.4f}")
    if test_path is None:
        print("NO REPRO: could not reproduce the bug.")
        return 1
    print(f"repro test: {test_path}")
    print(f"failing screenshot: {bundle_dir / 'shots' / 'repro_fail.png'}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run G0, perceive, and the repro stage on one video. Prints each step and the test path."
    )
    parser.add_argument("--video", type=Path, required=True, help="screen recording of the bug")
    parser.add_argument("--app", type=Path, required=True, help="directory of the buggy app")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("GLINT_LOG_LEVEL", "INFO").upper(), format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(_cli(args.video, args.app)))


if __name__ == "__main__":
    main()
