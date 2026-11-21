"""Candidate gates: patch rules, G2 (repro passes), G3 (never worse), G4 (Decision: defect gone)."""
from __future__ import annotations

import asyncio
import logging
import random
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Page, Route, async_playwright

from glint import config, gemini, workspace
from glint.gemini import VideoRef
from glint.repro import FAILED_ASSERTION, PASSED, VIEWPORT, run_test
from glint.schemas import Candidate, GateResult, ReproSpec

log = logging.getLogger("glint.gates")

MONKEY_SEED = 1234
MONKEY_WORDS = ["alpha", "beta 2", "gamma!", "x", "longer todo item text"]
ACTION_TIMEOUT_MS = 800
LOAD_TIMEOUT_MS = 8000
MAX_ERRORS_REPORTED = 3
DETAIL_CHARS = 300

def outcome_block(spec: ReproSpec) -> dict:
    """Decision API text block for G4: the observed (buggy) behavior and the expected correct behavior."""
    return {
        "type": "text",
        "text": (
            f"Observed incorrect behavior (the bug): {spec.observed}\n"
            f"Expected correct behavior after the same steps: {spec.expected}"
        ),
    }


# Visible, enabled, same-page interactive elements. Links that leave the app are skipped.
INTERACTIVE_JS = """
() => {
  const nodes = Array.from(document.querySelectorAll('a, button, input, textarea, select, label, li'));
  const out = [];
  nodes.forEach((el, index) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (rect.width < 2 || rect.height < 2) return;
    if (style.visibility === 'hidden' || style.display === 'none' || el.disabled) return;
    const tag = el.tagName.toLowerCase();
    const link = el.closest('a[href]');
    if (link && new URL(link.href, window.location.href).origin !== window.location.origin) return;
    out.push({ index: index, tag: tag, type: (el.getAttribute('type') || '').toLowerCase() });
  });
  return out;
}
"""
INTERACTIVE_SELECTOR = "a, button, input, textarea, select, label, li"

# Checked again right before each action, because the page can change between choosing an element and acting.
LEAVES_ORIGIN_JS = """
(el) => {
  const link = el.closest('a[href]');
  return !!link && new URL(link.href, window.location.href).origin !== window.location.origin;
}
"""
LOCAL_SCHEMES = ("data", "blob", "about")


async def _monkey_step(page: Page, rng: random.Random) -> None:
    elements = await page.evaluate(INTERACTIVE_JS)
    if not elements:
        return
    el = rng.choice(elements)
    target = page.locator(INTERACTIVE_SELECTOR).nth(el["index"])
    if await target.evaluate(LEAVES_ORIGIN_JS, timeout=ACTION_TIMEOUT_MS):
        return  # never click a link that leaves the app
    roll = rng.random()
    is_text_input = el["tag"] == "textarea" or (
        el["tag"] == "input" and el["type"] in ("", "text", "search")
    )
    if is_text_input:
        await target.fill(rng.choice(MONKEY_WORDS), timeout=ACTION_TIMEOUT_MS)
        await target.press("Enter", timeout=ACTION_TIMEOUT_MS)
    elif el["tag"] == "input" and el["type"] in ("checkbox", "radio"):
        await target.click(timeout=ACTION_TIMEOUT_MS)
    elif roll < 0.25:
        await target.dblclick(timeout=ACTION_TIMEOUT_MS)
    else:
        await target.click(timeout=ACTION_TIMEOUT_MS)


def is_favicon_load_error(text: str, url: str) -> bool:
    """True only for the browser's own failed request for /favicon.ico, which the app did not cause."""
    return text.startswith("Failed to load resource") and urlsplit(url).path == "/favicon.ico"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _on_console(kind: str, text: str, url: str, errors: list[str], blocked: set[str] | None = None) -> None:
    """Record a console error, except the favicon request and requests G3 itself blocked."""
    if kind != "error" or is_favicon_load_error(text, url):
        return
    if blocked and text.startswith("Failed to load resource") and url in blocked:
        return
    errors.append(f"console error: {text}")


async def monkey_check(base_url: str, actions: int = config.MONKEY_ACTIONS, seed: int = MONKEY_SEED) -> list[str]:
    """Load the app and perform seeded random actions. Returns every page error and console error seen.

    Requests to any other origin are aborted, links that leave the app are never clicked, and console
    errors caused by those aborted requests are not counted.
    """
    errors: list[str] = []
    rng = random.Random(seed)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport=VIEWPORT)
            own_origin = _origin(base_url)
            blocked: set[str] = set()

            async def keep_local(route: Route) -> None:
                url = route.request.url
                if _origin(url) == own_origin or urlsplit(url).scheme in LOCAL_SCHEMES:
                    await route.continue_()
                else:
                    blocked.add(url)
                    await route.abort()

            # G3 never touches anything but the workspace's own server: no request may leave its origin.
            await context.route("**/*", keep_local)
            page = await context.new_page()
            page.on("pageerror", lambda exc: errors.append(f"page error: {exc}"))
            page.on(
                "console",
                lambda msg: _on_console(msg.type, msg.text, msg.location.get("url", ""), errors, blocked),
            )
            page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
            try:
                await page.goto(base_url, wait_until="load", timeout=LOAD_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001 - a page that does not load fails the gate
                return [f"page did not load: {type(exc).__name__}"]
            for _ in range(actions):
                try:
                    await _monkey_step(page, rng)
                except Exception as exc:  # noqa: BLE001 - an element that moved or vanished is not an app error
                    log.debug("monkey action skipped: %s", type(exc).__name__)
            await page.wait_for_timeout(200)
        finally:
            await browser.close()
    return errors


def _short(text: str) -> str:
    return " ".join(text.split())[:DETAIL_CHARS]


async def _g2(candidate: Candidate, test_path: Path, base_url: str, work_dir: Path, shot_out: Path) -> GateResult:
    shot: Path | None = None
    for run in range(1, config.CANDIDATE_PASS_RUNS + 1):
        outcome, detail, shot = await run_test(test_path, base_url, work_dir / f"g2_run_{run}")
        if outcome != PASSED:
            if shot is not None:
                shutil.copyfile(shot, shot_out)
            kind = "still fails" if outcome == FAILED_ASSERTION else "breaks"
            return GateResult(
                gate="G2",
                passed=False,
                detail=_short(f"Repro test {kind} on the patched app (run {run}): {detail}"),
                candidate_id=candidate.id,
            )
    if shot is None:
        return GateResult(
            gate="G2", passed=False, detail="The passing run produced no screenshot.", candidate_id=candidate.id
        )
    shutil.copyfile(shot, shot_out)
    runs = config.CANDIDATE_PASS_RUNS
    return GateResult(
        gate="G2", passed=True, detail=f"Repro test passed {runs} of {runs} runs.", candidate_id=candidate.id
    )


async def _g3(candidate: Candidate, base_url: str) -> GateResult:
    errors = await monkey_check(base_url)
    if errors:
        shown = "; ".join(errors[:MAX_ERRORS_REPORTED])
        return GateResult(
            gate="G3",
            passed=False,
            detail=_short(f"{len(errors)} error(s) during load and {config.MONKEY_ACTIONS} random actions: {shown}"),
            candidate_id=candidate.id,
        )
    return GateResult(
        gate="G3",
        passed=True,
        detail=f"No page or console errors during load and {config.MONKEY_ACTIONS} random actions.",
        candidate_id=candidate.id,
    )


async def _g4(candidate: Candidate, spec: ReproSpec, before_shot: Path, after_shot: Path) -> GateResult:
    """Decision 1 compares the failing-repro screenshot (before) with the candidate's screenshot (after)."""
    answers = await gemini.decide(
        [gemini.image_block(before_shot), gemini.image_block(after_shot), outcome_block(spec)],
        {"g4": config.Q_G4},
    )
    p_yes = answers["g4"]
    passed = p_yes >= config.TH_G4_DEFECT_GONE
    detail = (
        f"Expected behavior judged present in the after screenshot (p={p_yes:.2f})."
        if passed
        else f"Expected behavior not clearly shown in the after screenshot (p={p_yes:.2f}, "
        f"need {config.TH_G4_DEFECT_GONE:.2f})."
    )
    return GateResult(gate="G4", passed=passed, p_yes=p_yes, detail=detail, candidate_id=candidate.id)


async def _run_gates(
    c: Candidate, app_dir: Path, repro_test: Path, video_ref: VideoRef, bundle_dir: Path, spec: ReproSpec, ws: Path
) -> None:
    assert c.patch is not None
    try:
        c.files_changed, c.lines_changed = workspace.apply_patch(ws, c.patch)
    except workspace.PatchRejected as exc:
        c.files_changed = sorted(c.patch.files)
        c.gates.append(GateResult(gate="G3", passed=False, detail=_short(f"Patch rejected: {exc}"), candidate_id=c.id))
        return

    diffs_dir = bundle_dir / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)
    (diffs_dir / f"{c.id}.diff").write_text(workspace.unified_diff(app_dir, ws), encoding="utf-8")

    # Each candidate runs its own copy of the frozen test, so parallel runs never share a conftest or output.
    work_dir = bundle_dir / "candidates" / c.id
    work_dir.mkdir(parents=True, exist_ok=True)
    test_copy = work_dir / "repro_test.py"
    shutil.copyfile(repro_test, test_copy)
    shots_dir = bundle_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    after_shot = shots_dir / f"{c.id}_after.png"

    async with workspace.serve(ws) as base_url:
        g2 = await _g2(c, test_copy, base_url, work_dir, after_shot)
        c.gates.append(g2)
        if not g2.passed:
            return
        g3 = await _g3(c, base_url)
        c.gates.append(g3)
        if not g3.passed:
            return
    before_shot = shots_dir / "repro_fail.png"
    if not before_shot.is_file():
        c.gates.append(
            GateResult(gate="G4", passed=False, detail="The failing-repro screenshot is missing.", candidate_id=c.id)
        )
        return
    c.gates.append(await _g4(c, spec, before_shot, after_shot))


async def check_candidate(
    c: Candidate, app_dir: Path, repro_test: Path, video_ref: VideoRef, bundle_dir: Path, spec: ReproSpec
) -> Candidate:
    """Apply the candidate's patch in its own workspace and run G2, G3, G4, stopping at the first failure.

    Fills gates, survived, reject_reason, files_changed, lines_changed and writes diffs/<id>.diff and
    shots/<id>_after.png into bundle_dir. Never raises. spec supplies the symptom text for G4.
    """
    if c.patch is None:
        c.survived = False
        c.reject_reason = c.reject_reason or "No patch was produced."
        return c

    ws = workspace.make_workspace(app_dir)
    try:
        async with asyncio.timeout(config.TIMEOUT_S["gates_per_candidate"]):
            await _run_gates(c, app_dir, repro_test, video_ref, bundle_dir, spec, ws)
    except TimeoutError:
        c.survived = False
        c.reject_reason = "timeout"
        return c
    except Exception as exc:  # a broken candidate is rejected, it never crashes the run
        log.exception("gates failed for %s", c.id)
        c.survived = False
        c.reject_reason = f"Checks could not be completed ({type(exc).__name__})."
        return c
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    failed = next((g for g in c.gates if not g.passed), None)
    c.survived = failed is None and any(g.gate == "G4" for g in c.gates)
    c.reject_reason = "" if c.survived else (f"{failed.gate}: {failed.detail}" if failed else "Checks incomplete.")
    return c
