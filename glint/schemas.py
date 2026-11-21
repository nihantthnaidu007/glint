"""Glint shared data contracts. Frozen after P0. Only LEAD edits, via the change protocol."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field

Status = Literal["VERIFIED_FIX", "NO_VERIFIED_FIX", "ABSTAIN", "REJECTED_INPUT"]
Stage = Literal["input", "g0", "perceive", "repro", "candidates", "repair", "select", "done"]
GateName = Literal["G0", "G1", "G1B", "G2", "G3", "G4"]


class VideoInfo(BaseModel):
    path: str                      # normalized mp4 path
    duration_s: float
    width: int
    height: int
    has_audio: bool
    trimmed: bool                  # True if the original was longer than MAX_VIDEO_S


class ReproSpec(BaseModel):
    summary: str                   # one line: what is wrong
    steps: list[str]               # concrete user actions in order
    expected: str
    observed: str
    evidence_timestamps_s: list[float] = Field(default_factory=list)
    evidence_notes: list[str] = Field(default_factory=list)   # same length as timestamps, shown as timeline markers
    suspect_files: list[str] = Field(default_factory=list)    # app-relative paths that exist
    confidence: float = Field(ge=0.0, le=1.0)


class GateResult(BaseModel):
    gate: GateName
    passed: bool
    p_yes: float | None = None     # Decision gates only
    detail: str = ""
    candidate_id: str | None = None


class Patch(BaseModel):
    files: dict[str, str]          # app-relative path -> FULL new file content
    rationale: str = ""


class Candidate(BaseModel):
    id: str                        # "c1".."c4", repair candidates "r1".."r2"
    source: Literal["initial", "repair"]
    patch: Patch | None = None
    lines_changed: int = 0
    files_changed: list[str] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)
    survived: bool = False
    reject_reason: str = ""


class Usage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class RunResult(BaseModel):
    run_id: str
    app_id: str
    status: Status
    reason: str                    # one human-readable sentence, always set
    stage_reached: Stage
    video: VideoInfo | None = None
    spec: ReproSpec | None = None
    repro_test: str | None = None  # bundle-relative path, e.g. "repro_test.py"
    candidates: list[Candidate] = Field(default_factory=list)
    winner_id: str | None = None
    gates: list[GateResult] = Field(default_factory=list)   # run-level gates G0, G1, G1B
    usage: Usage = Field(default_factory=Usage)
    latency_s: float = 0.0
    internal_error: bool = False   # True if an exception was caught and converted to a status
    created_at: datetime


EventType = Literal[
    "run.started", "input.normalized", "gate.result", "spec.ready",
    "repro.attempt", "repro.ready", "candidate.created", "candidate.final",
    "repair.started", "verdict", "run.finished", "log",
]


class Event(BaseModel):
    run_id: str
    seq: int                       # starts at 1, strictly increasing per run
    ts: datetime
    type: EventType
    stage: Stage
    data: dict[str, Any] = Field(default_factory=dict)
    replay: bool = False
