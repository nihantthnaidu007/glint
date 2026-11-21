"""run_pipeline never raises: model failures and bad input both become a RunResult. No network."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from glint import config, gemini, pipeline
from glint.schemas import Event, RunResult

APP = Path(__file__).resolve().parent.parent / "apps" / "todomvc"


def make_video(path: Path, seconds: int = 4) -> Path:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=10",
         "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )  # fmt: skip
    return path


def read_events(bundle: Path) -> list[Event]:
    lines = (bundle / "events.jsonl").read_text().splitlines()
    return [Event.model_validate_json(line) for line in lines]


def check_bundle(bundle: Path, result: RunResult) -> list[Event]:
    events = read_events(bundle)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[0].type == "run.started" and events[-1].type == "run.finished"
    assert events[-2].type == "verdict" and events[-2].data["status"] == result.status
    saved = RunResult.model_validate(json.loads((bundle / "result.json").read_text()))
    assert saved.status == result.status and saved.reason == result.reason
    assert result.reason and "Traceback" not in result.reason and "\n" not in result.reason
    return events


@pytest.fixture
def runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


async def test_model_failure_becomes_result(runs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("model down")

    for name in ("upload_video", "neon", "hydrogen", "decide"):
        monkeypatch.setattr(gemini, name, boom)
    seen: list[Event] = []

    async def emit(event: Event) -> None:
        seen.append(event)

    result = await pipeline.run_pipeline(make_video(tmp_path / "v.mp4"), APP, "todomvc", "r_test_boom", emit)
    assert result.internal_error is True
    assert result.status == "ABSTAIN"
    assert result.reason == "Stopped at input: RuntimeError."
    events = check_bundle(runs_dir / "r_test_boom", result)
    assert [e.seq for e in seen] == [e.seq for e in events]
    assert any(e.type == "input.normalized" for e in events)


async def test_failure_after_upload_is_abstain(runs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_upload(path: Path) -> gemini.VideoRef:
        return gemini.VideoRef(name="files/x", uri="https://example.invalid/x", mime_type="video/mp4")

    async def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("decision api down")

    monkeypatch.setattr(gemini, "upload_video", fake_upload)
    monkeypatch.setattr(gemini, "decide", boom)
    result = await pipeline.run_pipeline(make_video(tmp_path / "v.mp4"), APP, "todomvc", "r_test_g0")
    assert result.internal_error is True and result.status == "ABSTAIN" and result.stage_reached == "g0"
    assert result.reason == "Stopped at g0: ValueError."
    check_bundle(runs_dir / "r_test_g0", result)


async def test_random_bytes_are_rejected(runs_dir: Path, tmp_path: Path) -> None:
    bad = tmp_path / "x.mp4"
    bad.write_bytes(bytes(range(256)) * 64)
    result = await pipeline.run_pipeline(bad, APP, "todomvc", "r_test_bytes")
    assert result.status == "REJECTED_INPUT" and result.internal_error is False
    assert result.stage_reached == "input"
    check_bundle(runs_dir / "r_test_bytes", result)


async def test_missing_video_and_broken_emit(runs_dir: Path, tmp_path: Path) -> None:
    async def bad_emit(event: Event) -> None:
        raise RuntimeError("listener crashed")

    result = await pipeline.run_pipeline(tmp_path / "nope.mp4", APP, "todomvc", "r_test_missing", bad_emit)
    assert result.status == "REJECTED_INPUT" and result.internal_error is False
    check_bundle(runs_dir / "r_test_missing", result)


def test_new_run_id_format() -> None:
    run_id = pipeline.new_run_id()
    assert run_id.startswith("r_") and len(run_id) == len("r_20261121_143012_a3f9")
