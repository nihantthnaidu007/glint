"""Record R0 bug videos and clean videos for the Glint eval corpus."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

from eval.fuzz import _INTERACTIVE_SELECTOR, _pick_and_perform_step, _serve_dir

logger = logging.getLogger(__name__)

_CURSOR_SCRIPT = """
(() => {
    if (document.getElementById("glint-cursor")) return;
    const dot = document.createElement("div");
    dot.id = "glint-cursor";
    Object.assign(dot.style, {
        position: "fixed",
        width: "14px",
        height: "14px",
        borderRadius: "50%",
        background: "rgba(232, 163, 61, 0.9)",
        border: "2px solid #1F2328",
        pointerEvents: "none",
        zIndex: "999999",
        transform: "translate(-50%, -50%)",
        left: "-100px",
        top: "-100px",
    });
    document.body.appendChild(dot);
    const move = (e) => {
        dot.style.left = e.clientX + "px";
        dot.style.top = e.clientY + "px";
    };
    window.addEventListener("mousemove", move, true);
    window.addEventListener("mousedown", (e) => {
        move(e);
        dot.style.transform = "translate(-50%, -50%) scale(1.35)";
    }, true);
    window.addEventListener("mouseup", () => {
        dot.style.transform = "translate(-50%, -50%) scale(1)";
    }, true);
})();
"""


async def _move_cursor_to(page: Page, locator: Any) -> None:
    try:
        box = await locator.bounding_box()
        if box:
            await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    except PlaywrightError as exc:
        logger.debug("Could not move cursor to locator: %s", exc)


_USER_TARGET_JS = """
(el) => {
    const hittable = (node) => {
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return rect.width >= 4 && rect.height >= 4 && parseFloat(style.opacity) > 0
            && style.visibility !== "hidden" && style.display !== "none";
    };
    if (hittable(el)) return "self";
    const next = el.nextElementSibling;
    if (next && next.tagName === "LABEL" && hittable(next)) return "next-label";
    if (el.id && document.querySelector('label[for="' + el.id + '"]')) return "for-label";
    if (el.closest("label")) return "parent-label";
    return "none";
}
"""


async def _user_target(page: Page, locator: Any) -> Any | None:
    """The element a real user would click for this control.

    Some checkboxes are hidden (1x1 px, opacity 0) and operated through their label. A forced click on the
    hidden input does something no user can do, so the recording would show a click with no effect.
    Returns the label in that case, the element itself when it can be hit, or None when nothing can be clicked.
    """
    kind = await locator.evaluate(_USER_TARGET_JS)
    if kind == "self":
        return locator
    if kind == "next-label":
        return locator.locator("xpath=following-sibling::label[1]")
    if kind == "for-label":
        element_id = await locator.get_attribute("id")
        return page.locator(f'label[for="{element_id}"]').first
    if kind == "parent-label":
        return locator.locator("xpath=ancestor::label[1]")
    return None


async def _replay_actions(page: Page, sequence: list[dict[str, Any]], pause_ms: int = 0) -> None:
    for action in sequence:
        kind = action.get("kind")
        if kind == "escape":
            await page.keyboard.press("Escape")
            continue
        dom_idx = action.get("domIdx", 0)
        locator = page.locator(_INTERACTIVE_SELECTOR).nth(dom_idx)
        if await locator.count() == 0:
            logger.debug("Skipping action %s: element %d is not on the page", kind, dom_idx)
            continue
        if kind in ("check", "uncheck"):
            target = await _user_target(page, locator)
            if target is None:
                logger.debug("Skipping %s: element %d cannot be clicked by a user", kind, dom_idx)
                continue
            await _move_cursor_to(page, target)
            await target.click()
            if pause_ms:
                await page.wait_for_timeout(pause_ms)
            continue
        await _move_cursor_to(page, locator)
        if kind == "fill_enter":
            await locator.fill(action.get("text", "buy milk"))
            await locator.press("Enter")
        elif kind == "dblclick":
            await locator.dblclick()
        else:
            await locator.click()
        if pause_ms:
            await page.wait_for_timeout(pause_ms)


async def _plan_clean_actions(browser: Browser, url: str, seed: int, length: int) -> list[dict[str, Any]]:
    """Choose the clean-use actions on a page that is not recorded, so the recorded page can replay them
    with user-realistic clicks."""
    context = await browser.new_context(viewport={"width": 1280, "height": 720})
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        return [await _pick_and_perform_step(page, seed, step_idx) for step_idx in range(length)]
    finally:
        await context.close()


async def _record_one(
    browser: Browser,
    app_dir: Path,
    out_webm: Path,
    *,
    sequence: list[dict[str, Any]] | None = None,
    clean_seed: int | None = None,
    clean_length: int = 6,
) -> None:
    out_webm.parent.mkdir(parents=True, exist_ok=True)
    with _serve_dir(app_dir) as url, tempfile.TemporaryDirectory(prefix="glint_rec_") as vid_dir:
        clean_actions = (
            await _plan_clean_actions(browser, url, clean_seed, clean_length) if clean_seed is not None else None
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            record_video_dir=vid_dir,
            record_video_size={"width": 1280, "height": 720},
        )
        await context.route(
            "**/*",
            lambda route: route.continue_()
            if route.request.url.startswith(("http://127.0.0.1:", "http://localhost:", "data:", "blob:"))
            else route.abort(),
        )
        page = await context.new_page()
        video = page.video
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.evaluate(_CURSOR_SCRIPT)
            await page.wait_for_timeout(1000)
            if sequence is not None:
                await _replay_actions(page, sequence)
            elif clean_actions is not None:
                await _replay_actions(page, clean_actions, pause_ms=200)
            await page.wait_for_timeout(1500)
        finally:
            await context.close()
        if video is not None:
            rec_path = Path(await video.path())
            shutil.move(str(rec_path), str(out_webm))


async def _record_corpus(app_name: str, only: str | None) -> None:
    corpus_dir = Path("eval/corpus") / app_name
    orig_app = Path("apps") / app_name

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=450)
        try:
            mutant_dirs = sorted(
                d for d in corpus_dir.iterdir() if d.is_dir() and d.name.startswith("m")
            )
            for m_dir in mutant_dirs:
                if only and m_dir.name != only:
                    continue
                meta = json.loads((m_dir / "meta.json").read_text(encoding="utf-8"))
                out_webm = m_dir / "video_r0.webm"
                await _record_one(
                    browser,
                    m_dir / "app",
                    out_webm,
                    sequence=meta.get("divergent_sequence", []),
                )
                print(f"Recorded {out_webm}")

            for idx in range(1, 6):
                if only and only != f"c{idx:02d}":
                    continue
                clean_dir = corpus_dir / "clean" / f"c{idx:02d}"
                out_webm = clean_dir / "video_r0.webm"
                await _record_one(
                    browser,
                    orig_app,
                    out_webm,
                    clean_seed=100 + idx,
                    clean_length=6,
                )
                print(f"Recorded {out_webm}")
        finally:
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Record R0 bug and clean videos for eval corpus.")
    parser.add_argument("--app", required=True, help="App name under apps/ and eval/corpus/.")
    parser.add_argument("--only", default=None, help="Record only this mutant or clean video id (e.g. m001 or c01).")
    args = parser.parse_args()
    asyncio.run(_record_corpus(args.app, args.only))


if __name__ == "__main__":
    main()
