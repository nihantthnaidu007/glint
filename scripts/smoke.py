"""Smoke test: call Neon, Hydrogen, and Decision 1 once each with real inputs. Exit 1 on any failure."""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glint import config, gemini


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True, timeout=30)


def make_png(path: Path) -> Path:
    _ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=96x96", "-frames:v", "1", str(path)])
    return path


def make_video(path: Path) -> Path:
    _ffmpeg(["-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=15", "-pix_fmt", "yuv420p", str(path)])
    return path


async def step(name: str, model: str, call: Awaitable[object]) -> bool:
    started = time.monotonic()
    try:
        answer = await call
    except Exception as exc:  # noqa: BLE001 - the smoke test reports every failure verbatim
        print(f"[FAIL] {name} model={model} latency={time.monotonic() - started:.2f}s")
        print(f"       {type(exc).__name__}: {exc}")
        return False
    print(f"[ OK ] {name} model={model} latency={time.monotonic() - started:.2f}s")
    print(f"       answer: {answer!r}")
    return True


async def video_question(video: Path) -> str:
    started = time.monotonic()
    ref = await gemini.upload_video(video)
    took = time.monotonic() - started
    print(f"       uploaded {video.name} -> {ref.name} ({ref.mime_type}), ACTIVE after {took:.2f}s")
    answer = await gemini.neon([ref, "Describe what this short video shows in one sentence."])
    return str(answer)


async def main(quick: bool) -> int:
    gemini.bind_run("smoke")
    with tempfile.TemporaryDirectory(prefix="glint_smoke_") as tmp:
        png = make_png(Path(tmp) / "red.png")
        question = "What is the dominant color of this image? Answer with one word."
        results = [
            await step("neon image+text", config.NEON, gemini.neon([png, question])),
            await step("hydrogen image+text", config.HYDROGEN, gemini.hydrogen([png, question])),
            await step(
                "decision on image",
                config.DECISION,
                gemini.decide([gemini.image_block(png)], {"is_red": "Is this image mostly red?"}),
            ),
        ]
        if not quick:
            video = make_video(Path(tmp) / "test.mp4")
            results.append(await step("neon video question", config.NEON, video_question(video)))
    usage = gemini.usage_snapshot("smoke")
    print(
        f"usage: calls={usage.calls} input_tokens={usage.input_tokens} "
        f"output_tokens={usage.output_tokens} cost_usd={usage.cost_usd:.4f}"
    )
    print("SMOKE PASSED" if all(results) else "SMOKE FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Call all three Gemini models once with real inputs.")
    parser.add_argument("--quick", action="store_true", help="skip the video upload and video question")
    sys.exit(asyncio.run(main(parser.parse_args().quick)))
