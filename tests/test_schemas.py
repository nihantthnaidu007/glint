"""Every shared schema can be built with sample values and survives a JSON round trip."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from glint import schemas

NOW = datetime(2026, 11, 21, 14, 30, 12, tzinfo=UTC)


def sample_video() -> schemas.VideoInfo:
    return schemas.VideoInfo(
        path="video.mp4", duration_s=21.5, width=1280, height=720, has_audio=True, trimmed=False
    )


def sample_spec() -> schemas.ReproSpec:
    return schemas.ReproSpec(
        summary="Completed todos stay active",
        steps=["Type 'milk' and press Enter", "Click the toggle next to 'milk'"],
        expected="The item is struck through",
        observed="The item stays active",
        evidence_timestamps_s=[4.2],
        evidence_notes=["toggle clicked, no strike-through"],
        suspect_files=["js/app.js"],
        confidence=0.8,
    )


def sample_gate(candidate_id: str | None = None) -> schemas.GateResult:
    return schemas.GateResult(gate="G4", passed=True, p_yes=0.91, detail="defect gone", candidate_id=candidate_id)


def sample_candidate() -> schemas.Candidate:
    return schemas.Candidate(
        id="c1",
        source="initial",
        patch=schemas.Patch(files={"js/app.js": "var a = 1;\n"}, rationale="flip comparison"),
        lines_changed=2,
        files_changed=["js/app.js"],
        gates=[sample_gate("c1")],
        survived=True,
    )


def sample_result() -> schemas.RunResult:
    return schemas.RunResult(
        run_id="r_20261121_143012_a3f9",
        app_id="todomvc",
        status="VERIFIED_FIX",
        reason="Verified fix c1 changed 2 lines in js/app.js.",
        stage_reached="done",
        video=sample_video(),
        spec=sample_spec(),
        repro_test="repro_test.py",
        candidates=[sample_candidate()],
        winner_id="c1",
        gates=[schemas.GateResult(gate="G0", passed=True, p_yes=0.7)],
        usage=schemas.Usage(calls=9, input_tokens=1000, output_tokens=200, cost_usd=0.31),
        latency_s=88.4,
        created_at=NOW,
    )


def sample_event() -> schemas.Event:
    return schemas.Event(
        run_id="r_20261121_143012_a3f9", seq=1, ts=NOW, type="run.started", stage="input", data={"app_id": "todomvc"}
    )


SAMPLES: list[BaseModel] = [
    sample_video(),
    sample_spec(),
    sample_gate(),
    schemas.Patch(files={"index.html": "<html></html>"}),
    sample_candidate(),
    schemas.Usage(),
    sample_result(),
    sample_event(),
]


@pytest.mark.parametrize("model", SAMPLES, ids=lambda m: type(m).__name__)
def test_json_round_trip(model: BaseModel) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model


def test_defaults() -> None:
    result = schemas.RunResult(
        run_id="r", app_id="a", status="ABSTAIN", reason="No defect seen.", stage_reached="g0", created_at=NOW
    )
    assert result.candidates == [] and result.winner_id is None and result.internal_error is False
    assert result.usage == schemas.Usage()
    assert sample_event().replay is False


def test_invalid_values_rejected() -> None:
    with pytest.raises(ValidationError):
        schemas.ReproSpec(summary="s", steps=[], expected="e", observed="o", confidence=1.5)
    with pytest.raises(ValidationError):
        schemas.GateResult(gate="G9", passed=True)
    with pytest.raises(ValidationError):
        schemas.Event(run_id="r", seq=1, ts=NOW, type="nope", stage="input")
