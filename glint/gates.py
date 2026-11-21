"""Candidate gates: patch rules, G2 (repro passes), G3 (never worse), G4 (Decision: defect gone)."""
from __future__ import annotations

import asyncio
import logging
import random
import shutil
from pathlib import Path

from playwright.async_api import Page, async_playwright

from glint import config, gemini, workspace
from glint.gemini import VideoRef
from glint.perceive import symptom_block
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
    """Decision API text block for G4: the reported symptom plus the expected correct behavior.

    With the symptom alone, any screen that does not show the bug scores high (an unrelated screenshot
    scored 0.89). Adding the expected behavior separates them: fixed 0.91, still buggy 0.19, unrelated 0.17.
    """
    symptom = symptom_block(spec)["text"]
    return {"type": "text", "text": f"{symptom}\nExpected correct behavior after the same steps: {spec.expected}"}

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
    if (tag === 'a') {
      const href = el.getAttribute('href') || '';
      if (!(href === '' || href.startsWith('#'))) return;
    }
    out.push({ index: index, tag: tag, type: (el.getAttribute('type') || '').toLowerCase() });
  });
  return out;
}
"""
INTERACTIVE_SELECTOR = "a, button, input, textarea, select, label, li"


async def _monkey_step(page: Page, rng: random.Random) -> None:
    elements = await page.evaluate(INTERACTIVE_JS)
    if not elements:
        return
    el = rng.choice(elements)
    target = page.locator(INTERACTIVE_SELECTOR).nth(el["index"])
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


async def monkey_check(base_url: str, actions: int = config.MONKEY_ACTIONS, seed: int = MONKEY_SEED) -> list[str]:
    """Load the app and perform seeded random actions. Returns every page error and console error seen."""
    errors: list[str] = []
    rng = random.Random(seed)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport=VIEWPORT)
            page.on("pageerror", lambda exc: errors.append(f"page error: {exc}"))
            page.on("console", lambda msg: errors.append(f"console error: {msg.text}") if msg.type == "error" else None)
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


async def _g4(candidate: Candidate, video_ref: VideoRef, spec: ReproSpec, shot: Path) -> GateResult:
    answers = await gemini.decide(
        [
            gemini.video_block(video_ref, config.DECISION_VIDEO_RESOLUTION),
            gemini.image_block(shot),
            outcome_block(spec),
        ],
        {"g4": config.Q_G4},
    )
    p_yes = answers["g4"]
    passed = p_yes >= config.TH_G4_DEFECT_GONE
    detail = (
        f"Defect judged gone in the after screenshot (p={p_yes:.2f})."
        if passed
        else f"Defect not clearly gone in the after screenshot (p={p_yes:.2f}, need {config.TH_G4_DEFECT_GONE:.2f})."
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
    c.gates.append(await _g4(c, video_ref, spec, after_shot))


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
