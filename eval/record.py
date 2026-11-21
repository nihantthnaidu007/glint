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


async def _replay_actions(page: Page, sequence: list[dict[str, Any]]) -> None:
    for action in sequence:
        kind = action.get("kind")
        if kind == "escape":
            await page.keyboard.press("Escape")
            continue
        dom_idx = action.get("domIdx", 0)
        locator = page.locator(_INTERACTIVE_SELECTOR).nth(dom_idx)
        await _move_cursor_to(page, locator)
        if kind in ("check", "uncheck"):
            await locator.click(force=True)
        elif kind == "fill_enter":
            await locator.fill(action.get("text", "buy milk"))
            await locator.press("Enter")
        elif kind == "dblclick":
            await locator.dblclick()
        else:
            await locator.click()


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
            elif clean_seed is not None:
                for step_idx in range(clean_length):
                    await _pick_and_perform_step(page, clean_seed, step_idx)
                    await page.wait_for_timeout(200)
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

            if only is None:
                for idx in range(1, 6):
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
    parser.add_argument("--only", default=None, help="Record only this mutant id (e.g. m001).")
    args = parser.parse_args()
    asyncio.run(_record_corpus(args.app, args.only))


if __name__ == "__main__":
    main()
