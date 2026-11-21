"""Deterministic Playwright fuzzer and differential oracle for Glint eval."""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright

logger = logging.getLogger(__name__)

_DISABLE_ANIMATIONS_CSS = """
*, *::before, *::after {
    animation-duration: 0s !important;
    animation-delay: 0s !important;
    transition-duration: 0s !important;
    transition-delay: 0s !important;
}
"""

_INTERACTIVE_SELECTOR = "button, input, a, label, li, [role=button], [type=checkbox]"
_WORDS = [
    "buy milk",
    "walk dog",
    "read book",
    "fix bug",
    "ship code",
    "call mom",
    "write tests",
    "clean desk",
]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def _serve_dir(app_dir: Path) -> Iterator[str]:
    """Serve app_dir on a free localhost port using python -m http.server."""
    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(app_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"http.server exited early with {proc.returncode}")
            try:
                with urllib.request.urlopen(url, timeout=0.5) as resp:
                    if resp.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.05)
        else:
            raise TimeoutError(f"http.server did not respond 200 at {url}")
        yield url
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def generate_sequence(seed: int, length: int = 12) -> list[dict[str, Any]]:
    """Return a deterministic list of seeded step descriptors for length steps."""
    steps: list[dict[str, Any]] = []
    for step_idx in range(length):
        rng = random.Random(f"{seed}:{step_idx}")
        steps.append(
            {
                "step": step_idx,
                "seed": seed,
                "element_roll": rng.random(),
                "action_roll": rng.random(),
                "text": rng.choice(_WORDS),
            }
        )
    return steps


async def snapshot(page: Page) -> dict[str, Any]:
    """Capture a deterministic DOM + localStorage snapshot of the page."""
    return await page.evaluate(
        """() => {
            const rawText = document.body ? (document.body.innerText || "") : "";
            const text = rawText.replace(/\\s+/g, " ").trim();
            const checkboxes = Array.from(
                document.querySelectorAll('input[type="checkbox"]')
            ).map(el => Boolean(el.checked));
            const counts = {
                li: document.querySelectorAll("li").length,
                input: document.querySelectorAll("input").length,
                button: document.querySelectorAll("button").length,
            };
            const lsItems = [];
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                lsItems.push([k, localStorage.getItem(k)]);
            }
            lsItems.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
            return {
                text,
                checkboxes,
                counts,
                local_storage: lsItems,
                title: document.title || "",
            };
        }"""
    )


async def _pick_and_perform_step(page: Page, seed: int, step_idx: int) -> dict[str, Any]:
    """Choose a visible interactive element deterministically and perform an action."""
    rng = random.Random(f"{seed}:{step_idx}")

    visible_items: list[dict[str, Any]] = await page.evaluate(
        """(sel) => {
            const els = Array.from(document.querySelectorAll(sel));
            const out = [];
            els.forEach((el, domIdx) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const visible = (
                    rect.width > 0 &&
                    rect.height > 0 &&
                    style.visibility !== "hidden" &&
                    style.display !== "none"
                );
                if (!visible) return;
                if (el.tagName.toLowerCase() === "a") {
                    const rawHref = el.getAttribute("href") || "";
                    if (rawHref && !rawHref.startsWith("#") && el.origin !== window.location.origin) {
                        return;
                    }
                }
                out.push({
                    domIdx,
                    tag: el.tagName.toLowerCase(),
                    type: (el.getAttribute("type") || "").toLowerCase(),
                    cls: el.className || "",
                });
            });
            return out;
        }""",
        _INTERACTIVE_SELECTOR,
    )

    if not visible_items or rng.random() < 0.05:
        await page.keyboard.press("Escape")
        return {"kind": "escape", "step": step_idx}

    # Prefer text inputs occasionally when present so lists get populated
    text_inputs = [
        item for item in visible_items if item["tag"] == "input" and item["type"] in ("", "text")
    ]
    if text_inputs and rng.random() < 0.35:
        chosen = rng.choice(text_inputs)
    else:
        chosen = rng.choice(visible_items)

    locator = page.locator(_INTERACTIVE_SELECTOR).nth(chosen["domIdx"])
    tag = chosen["tag"]
    itype = chosen["type"]

    if tag == "input" and itype == "checkbox":
        is_checked = await locator.is_checked()
        await locator.click(force=True)
        kind = "uncheck" if is_checked else "check"
        return {"kind": kind, "domIdx": chosen["domIdx"], "step": step_idx}

    if tag == "input" and itype in ("", "text", "search"):
        text = rng.choice(_WORDS)
        await locator.fill(text)
        await locator.press("Enter")
        return {"kind": "fill_enter", "domIdx": chosen["domIdx"], "text": text, "step": step_idx}

    if tag == "label" and rng.random() < 0.35:
        await locator.dblclick()
        return {"kind": "dblclick", "domIdx": chosen["domIdx"], "step": step_idx}

    await locator.click()
    return {"kind": "click", "domIdx": chosen["domIdx"], "step": step_idx}


async def _run_sequence_with_browser(
    browser: Browser,
    url: str,
    seed: int,
    length: int = 12,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Run one sequence on an already-served URL, returning (snapshots, actions, page_errors)."""
    context = await browser.new_context(viewport={"width": 1280, "height": 720})
    await context.route(
        "**/*",
        lambda route: route.continue_()
        if route.request.url.startswith(("http://127.0.0.1:", "http://localhost:", "data:", "blob:"))
        else route.abort(),
    )
    page = await context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))
    try:
        await page.goto(url, wait_until="domcontentloaded")
        await page.add_style_tag(content=_DISABLE_ANIMATIONS_CSS)
        await page.wait_for_timeout(50)

        snaps = [await snapshot(page)]
        actions: list[dict[str, Any]] = []
        for step_idx in range(length):
            action = await _pick_and_perform_step(page, seed, step_idx)
            actions.append(action)
            await page.wait_for_timeout(20)
            snaps.append(await snapshot(page))
        return snaps, actions, page_errors
    finally:
        await context.close()


async def run_sequence(app_dir: Path, seed: int, length: int = 12) -> list[dict[str, Any]]:
    """Serve app_dir and run a deterministic sequence, returning length + 1 snapshots."""
    with _serve_dir(app_dir) as url:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                snaps, _, _ = await _run_sequence_with_browser(browser, url, seed, length)
                return snaps
            finally:
                await browser.close()


async def check_loads_cleanly(app_dir: Path) -> bool:
    """Return True if app_dir loads in Chromium with zero pageerrors."""
    with _serve_dir(app_dir) as url:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(viewport={"width": 1280, "height": 720})
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda err: errors.append(str(err)))
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(100)
                await context.close()
                return len(errors) == 0
            finally:
                await browser.close()


async def differential(
    original: Path,
    candidate: Path,
    n_seq: int,
    seed: int,
    *,
    length: int = 12,
    browser: Browser | None = None,
    orig_cache: dict[tuple[str, int, int], list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Compare original and candidate across n_seq sequences (seeds seed..seed+n_seq-1)."""
    orig_key_prefix = str(original.resolve())

    async def _run(br: Browser) -> dict[str, Any] | None:
        with _serve_dir(original) as orig_url, _serve_dir(candidate) as cand_url:
            for offset in range(n_seq):
                seq_seed = seed + offset
                cache_key = (orig_key_prefix, seq_seed, length)
                if orig_cache is not None and cache_key in orig_cache:
                    orig_snaps = orig_cache[cache_key]
                    cand_snaps, cand_actions, _ = await _run_sequence_with_browser(
                        br, cand_url, seq_seed, length
                    )
                    actions = cand_actions
                else:
                    (orig_snaps, orig_actions, _), (cand_snaps, _, _) = await asyncio.gather(
                        _run_sequence_with_browser(br, orig_url, seq_seed, length),
                        _run_sequence_with_browser(br, cand_url, seq_seed, length),
                    )
                    actions = orig_actions
                    if orig_cache is not None:
                        orig_cache[cache_key] = orig_snaps

                for step_idx, (o_snap, c_snap) in enumerate(zip(orig_snaps, cand_snaps, strict=True)):
                    if o_snap != c_snap:
                        return {
                            "sequence": actions[:step_idx] if step_idx > 0 else [],
                            "step": step_idx,
                            "original": o_snap,
                            "candidate": c_snap,
                        }
        return None

    if browser is not None:
        return await _run(browser)

    async with async_playwright() as p:
        br = await p.chromium.launch(headless=True)
        try:
            return await _run(br)
        finally:
            await br.close()


async def _selftest() -> None:
    app_dir = Path("apps/todomvc")
    s1 = await run_sequence(app_dir, seed=42, length=12)
    s2 = await run_sequence(app_dir, seed=42, length=12)
    assert s1 == s2, "Determinism check failed: same seed produced different snapshots"
    assert len(s1) == 13, f"Expected 13 snapshots, got {len(s1)}"
    diff = await differential(app_dir, app_dir, n_seq=20, seed=0)
    assert diff is None, f"Identical app diverged: {diff}"
    print("fuzz selftest passed: determinism OK, 20 differential sequences on identical app returned None.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Glint Playwright fuzzer and differential oracle.")
    parser.add_argument("--selftest", action="store_true", help="Run determinism and identity self-test.")
    args = parser.parse_args()
    if args.selftest:
        asyncio.run(_selftest())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
