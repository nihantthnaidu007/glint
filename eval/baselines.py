"""The baseline Glint is compared against: one model call, video plus source in, patch out."""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from glint import config, gemini, media
from glint.patch import PatchWire, to_patch
from glint.perceive import app_source_parts
from glint.schemas import Patch

log = logging.getLogger("eval.baselines")

MODELS = {"neon": gemini.neon, "hydrogen": gemini.hydrogen}

BASELINE_PROMPT = """You fix one bug in a web application.

You are given a screen recording in which a user runs into a bug, and the application's full source code
(it currently HAS the bug). Each source file starts with a line "=== <path> ===".
Work out from the recording what goes wrong, find the defect in the source, and return a fix.

Rules for the fix:
- Edit at most {max_files} files, and only files that already exist in the source given above.
- Only files with these extensions may be edited: {extensions}.
- For every file you edit, return its path exactly as shown after "===" and its FULL new content, with every
  unchanged line kept exactly as it is. Do not reformat, reorder, or re-indent anything you are not fixing.
- Make the smallest change that fixes the behavior. Change at most {max_lines} lines in total.
- Fix the cause in the application's logic. Do not special-case the texts or values seen in the recording.
- Never create, edit, or mention test files.
- In rationale, say in one sentence what was wrong and what you changed.
- If you cannot find a defect, return an empty list of files.
"""


def baseline_prompt() -> str:
    """The baseline's instruction text, with the same patch limits the workspace enforces."""
    return BASELINE_PROMPT.format(
        max_files=config.PATCH_MAX_FILES,
        extensions=", ".join(sorted(config.PATCH_ALLOWED_EXT)),
        max_lines=config.PATCH_MAX_CHANGED_LINES,
    )


async def single_call(video_path: Path, app_dir: Path, model: str = "neon") -> Patch | None:
    """One model call: the video plus the full app source with paths, asked for full-file replacements.

    No repro test, no gates, no retries beyond the wrapper's own. Returns None when the model proposes no
    file changes. Raises when the video cannot be read or the model call fails or runs out of time.
    """
    if model not in MODELS:
        raise ValueError(f"unknown baseline model {model!r}, expected one of {sorted(MODELS)}")
    tmp = Path(tempfile.mkdtemp(prefix="glint_baseline_"))
    try:
        normalized = tmp / "video.mp4"
        await media.normalize(Path(video_path), normalized)
        async with asyncio.timeout(config.RUN_BUDGET_S):
            video_ref = await gemini.upload_video(normalized)
            parts: list = [video_ref, *app_source_parts(Path(app_dir)), baseline_prompt()]
            wire = await MODELS[model](parts, schema=PatchWire)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not isinstance(wire, PatchWire) or not wire.files:
        log.info("baseline proposed no file changes")
        return None
    return to_patch(wire)
