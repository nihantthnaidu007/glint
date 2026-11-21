"""Generate and run the robustness garbage suite."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

GARBAGE_DIR = Path("eval/corpus/garbage")
EXPECTED_PATH = GARBAGE_DIR / "expected.json"


def _run_ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", *args], check=True, capture_output=True)


def make_garbage() -> None:
    """Create the 5 robustness test inputs and expected.json under eval/corpus/garbage/."""
    GARBAGE_DIR.mkdir(parents=True, exist_ok=True)

    # 1. corrupt: random bytes renamed .mp4
    corrupt_path = GARBAGE_DIR / "corrupt.mp4"
    corrupt_path.write_bytes(os.urandom(4096))

    # 2. too_short: 2 second clip
    short_path = GARBAGE_DIR / "too_short.mp4"
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=30:duration=2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(short_path),
        ]
    )

    # 3. black: 15 seconds of black frames
    black_path = GARBAGE_DIR / "black.mp4"
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=1280x720:rate=30:duration=15",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(black_path),
        ]
    )

    # 4. long: > 3 minute recording built by looping clean use then a mutant's bug
    long_path = GARBAGE_DIR / "long.mp4"
    clean_src = Path("eval/corpus/todomvc/clean/c01/video_r0.webm")
    mutant_src = Path("eval/corpus/todomvc/m001/video_r0.webm")
    if clean_src.is_file() and mutant_src.is_file():
        clean_mp4 = GARBAGE_DIR / "_tmp_clean.mp4"
        mutant_mp4 = GARBAGE_DIR / "_tmp_mutant.mp4"
        _run_ffmpeg(
            [
                "-stream_loop",
                "30",
                "-i",
                str(clean_src),
                "-t",
                "175",
                "-vf",
                "scale=1280:720",
                "-r",
                "30",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(clean_mp4),
            ]
        )
        _run_ffmpeg(
            [
                "-i",
                str(mutant_src),
                "-vf",
                "scale=1280:720",
                "-r",
                "30",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(mutant_mp4),
            ]
        )
        concat_txt = GARBAGE_DIR / "_concat.txt"
        concat_txt.write_text(
            f"file '{clean_mp4.name}'\nfile '{mutant_mp4.name}'\n", encoding="utf-8"
        )
        _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c", "copy", str(long_path)])
        clean_mp4.unlink(missing_ok=True)
        mutant_mp4.unlink(missing_ok=True)
        concat_txt.unlink(missing_ok=True)
    else:
        _run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=1280x720:rate=30:duration=185",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(long_path),
            ]
        )

    # 5. clean_use: original app used normally, no bug
    clean_use_path = GARBAGE_DIR / "clean_use.webm"
    if clean_src.is_file():
        shutil.copy2(clean_src, clean_use_path)
    else:
        _run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=1280x720:rate=30:duration=6",
                "-c:v",
                "libvpx",
                str(clean_use_path),
            ]
        )

    expected: dict[str, dict[str, Any]] = {
        "corrupt": {
            "file": str(corrupt_path),
            "app": "apps/todomvc",
            "allowed": ["REJECTED_INPUT"],
        },
        "too_short": {
            "file": str(short_path),
            "app": "apps/todomvc",
            "allowed": ["REJECTED_INPUT"],
        },
        "black": {
            "file": str(black_path),
            "app": "apps/todomvc",
            "allowed": ["ABSTAIN"],
        },
        "long": {
            "file": str(long_path),
            "app": "eval/corpus/todomvc/m001/app"
            if Path("eval/corpus/todomvc/m001/app").is_dir()
            else "apps/todomvc",
            "allowed": ["VERIFIED_FIX", "NO_VERIFIED_FIX", "ABSTAIN", "REJECTED_INPUT"],
        },
        "clean_use": {
            "file": str(clean_use_path),
            "app": "apps/todomvc",
            "allowed": ["ABSTAIN", "NO_VERIFIED_FIX"],
        },
    }
    EXPECTED_PATH.write_text(json.dumps(expected, indent=2), encoding="utf-8")
    print(f"Wrote {len(expected)} garbage cases and {EXPECTED_PATH}")


async def _run_garbage_async() -> int:
    from glint.pipeline import new_run_id, run_pipeline

    from glint import config

    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    any_fail = False
    print("case | status | allowed | internal_error | seconds | result")
    print("-" * 72)
    for case, spec in expected.items():
        t0 = time.monotonic()
        result = await run_pipeline(
            Path(spec["file"]),
            Path(spec["app"]),
            "todomvc",
            new_run_id(),
            None,
        )
        elapsed = time.monotonic() - t0
        ok = (
            result.status in spec["allowed"]
            and not result.internal_error
            and elapsed <= config.RUN_BUDGET_S + 10
        )
        if not ok:
            any_fail = True
        verdict = "PASS" if ok else "FAIL"
        print(
            f"{case} | {result.status} | {','.join(spec['allowed'])} | "
            f"{result.internal_error} | {elapsed:.1f} | {verdict}"
        )
    return 1 if any_fail else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and run Glint robustness garbage suite.")
    parser.add_argument("--make", action="store_true", help="Generate garbage inputs and expected.json.")
    parser.add_argument("--run", action="store_true", help="Run pipeline on all garbage cases.")
    args = parser.parse_args()
    if args.make:
        make_garbage()
    elif args.run:
        sys.exit(asyncio.run(_run_garbage_async()))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
