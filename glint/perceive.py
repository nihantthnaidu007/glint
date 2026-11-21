"""Perception: G0 (is this a bug video) and Neon turning video plus app source into a ReproSpec."""
from __future__ import annotations

import logging
from pathlib import Path

from glint import config, gemini
from glint.gemini import VideoRef
from glint.schemas import GateResult, ReproSpec

log = logging.getLogger("glint.perceive")

SOURCE_EXT = {".js", ".html", ".css"}

PERCEIVE_PROMPT = """You are given a screen recording of a web application and the application's full source code.
The recording was made by a user reporting a bug. Watch the whole recording carefully, and listen to any narration.

Work out exactly what the user did, what the application did in response, and where the application's behavior
differs from what a user would correctly expect. Then fill in the bug report:

- summary: one line stating what is wrong.
- steps: the concrete user actions, in order, that an automated browser test can perform to reach the wrong
  behavior starting from a freshly loaded, empty app. Use the exact visible labels, placeholder texts, and the
  exact texts the user typed. One action per step. Include only the steps needed to show the bug.
- expected: what should be on screen after the last step if the app worked correctly. Be specific about which
  items, counts, or texts should be visible.
- observed: what is actually on screen after the last step in the recording. Be equally specific.
- evidence_timestamps_s: the times in seconds in the recording where the wrong behavior is visible.
- evidence_notes: one short note per timestamp, in the same order, saying what is visible at that moment.
- suspect_files: the source files most likely to contain the defect, using only paths from the file list below.
- confidence: your confidence from 0 to 1 that this is a real defect and that your description is right.

Describe only what the recording shows. Do not invent steps the user did not take.

Source files provided: {file_list}
"""


def app_source_files(app_dir: Path) -> list[Path]:
    """All source files of the app that models are allowed to read, sorted by relative path."""
    files = [p for p in app_dir.rglob("*") if p.is_file() and p.suffix.lower() in SOURCE_EXT]
    return sorted(files, key=lambda p: p.relative_to(app_dir).as_posix())


def app_source_parts(app_dir: Path) -> list[str]:
    """The app's source as prompt parts, one per file, each prefixed with its app-relative path."""
    parts: list[str] = []
    for path in app_source_files(app_dir):
        rel = path.relative_to(app_dir).as_posix()
        parts.append(f"=== {rel} ===\n{path.read_text(encoding='utf-8', errors='replace')}")
    return parts


def symptom_block(spec: ReproSpec) -> dict:
    """Decision API text block stating the reported symptom, for gates that compare a screenshot to the video.

    Without it Decision 1 cannot tell which on-screen state is the wrong one: on the same recording a
    screenshot of the buggy state scored 0.54 and one of the correct state 0.50. With it: 0.78 against 0.20.
    """
    return {"type": "text", "text": f"Reported incorrect behavior in the recording: {spec.observed}"}


async def is_defect(video_ref: VideoRef) -> GateResult:
    """G0: does the recording show an application misbehaving at all."""
    answers = await gemini.decide(
        [gemini.video_block(video_ref, config.DECISION_VIDEO_RESOLUTION)], {"g0": config.Q_G0}
    )
    p_yes = answers["g0"]
    passed = p_yes >= config.TH_G0_IS_DEFECT
    detail = (
        f"Recording looks like a defect (p={p_yes:.2f})."
        if passed
        else f"Recording does not clearly show a defect (p={p_yes:.2f}, need {config.TH_G0_IS_DEFECT:.2f})."
    )
    return GateResult(gate="G0", passed=passed, p_yes=p_yes, detail=detail)


def clean_spec(spec: ReproSpec, app_dir: Path, duration_s: float | None = None) -> ReproSpec:
    """Drop suspect files that do not exist, clamp timestamps, and keep notes aligned with timestamps."""
    root = app_dir.resolve()
    suspects: list[str] = []
    for rel in spec.suspect_files:
        candidate = (root / rel).resolve()
        if candidate.is_file() and candidate.is_relative_to(root):
            clean = candidate.relative_to(root).as_posix()
            if clean not in suspects:
                suspects.append(clean)

    timestamps: list[float] = []
    for t in spec.evidence_timestamps_s:
        t = max(0.0, float(t))
        if duration_s is not None:
            t = min(t, duration_s)
        timestamps.append(round(t, 2))
    notes = list(spec.evidence_notes[: len(timestamps)])
    notes += [""] * (len(timestamps) - len(notes))

    return spec.model_copy(
        update={"suspect_files": suspects, "evidence_timestamps_s": timestamps, "evidence_notes": notes}
    )


async def perceive(
    video_ref: VideoRef, app_dir: Path, duration_s: float | None = None, thinking: str = config.NEON_THINKING
) -> ReproSpec:
    """Neon watches the recording, reads the whole app, and returns a ReproSpec.

    duration_s is the normalized video's length; when given, evidence timestamps are clamped to it.
    """
    sources = app_source_parts(app_dir)
    file_list = ", ".join(p.relative_to(app_dir).as_posix() for p in app_source_files(app_dir))
    parts: list = [video_ref, *sources, PERCEIVE_PROMPT.format(file_list=file_list)]
    spec = await gemini.neon(parts, schema=ReproSpec, thinking=thinking)
    if not isinstance(spec, ReproSpec):
        raise TypeError("Neon did not return a ReproSpec")
    return clean_spec(spec, app_dir, duration_s)
