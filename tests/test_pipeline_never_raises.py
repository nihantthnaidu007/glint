"""run_pipeline never raises: model failures and bad input both become a RunResult. No network."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from glint import config, gemini, patch, perceive, pipeline, repro
from glint.schemas import Candidate, Event, GateResult, Patch, ReproSpec, RunResult

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


SPEC = ReproSpec(summary="s", steps=["do it"], expected="e", observed="o", confidence=0.9)


def stub_later_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace upload and everything after perceive with local stand-ins, so the run can reach select offline."""

    async def fake_upload(path: Path) -> gemini.VideoRef:
        return gemini.VideoRef(name="files/x", uri="https://example.invalid/x", mime_type="video/mp4")

    async def fake_repro(video_ref, spec, app_dir, bundle_dir, emit=None):
        (bundle_dir / "repro_test.py").write_text("def test_repro(page):\n    pass\n")
        return bundle_dir / "repro_test.py", [GateResult(gate="G1", passed=True), GateResult(gate="G1B", passed=True)]

    async def no_candidates(*args: object, **kwargs: object) -> list:
        return []

    monkeypatch.setattr(gemini, "upload_video", fake_upload)
    monkeypatch.setattr(repro, "establish_repro", fake_repro)
    monkeypatch.setattr(patch, "candidates", no_candidates)
    monkeypatch.setattr(patch, "repair", no_candidates)


async def test_slow_first_perceive_is_retried(runs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_later_stages(monkeypatch)
    monkeypatch.setitem(config.TIMEOUT_S, "perceive", 0.2)
    calls: list[str] = []

    async def fake_g0(video_ref):
        return GateResult(gate="G0", passed=True, p_yes=0.9)

    async def slow_then_fast(video_ref, app_dir, duration_s=None, thinking=config.NEON_THINKING):
        calls.append(thinking)
        if len(calls) == 1:
            await asyncio.sleep(5)
        return SPEC

    monkeypatch.setattr(perceive, "is_defect", fake_g0)
    monkeypatch.setattr(perceive, "perceive", slow_then_fast)
    result = await pipeline.run_pipeline(make_video(tmp_path / "v.mp4"), APP, "todomvc", "r_test_retry")
    assert calls == [config.NEON_THINKING, config.PERCEIVE_RETRY_THINKING]
    assert result.status == "NO_VERIFIED_FIX" and result.internal_error is False
    assert result.spec == SPEC and result.stage_reached == "select"
    events = check_bundle(runs_dir / "r_test_retry", result)
    logs = [e for e in events if e.type == "log"]
    assert len(logs) == 1 and "retrying once" in logs[0].data["message"] and logs[0].stage == "perceive"
    assert any(e.type == "spec.ready" for e in events)


async def test_perceive_failing_twice_abstains(runs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_later_stages(monkeypatch)
    monkeypatch.setitem(config.TIMEOUT_S, "perceive", 0.2)
    monkeypatch.setitem(config.TIMEOUT_S, "perceive_retry", 0.2)

    async def fake_g0(video_ref):
        return GateResult(gate="G0", passed=True, p_yes=0.9)

    async def always_slow(*args: object, **kwargs: object) -> ReproSpec:
        await asyncio.sleep(5)
        return SPEC

    monkeypatch.setattr(perceive, "is_defect", fake_g0)
    monkeypatch.setattr(perceive, "perceive", always_slow)
    result = await pipeline.run_pipeline(make_video(tmp_path / "v.mp4"), APP, "todomvc", "r_test_retry2")
    assert result.status == "ABSTAIN" and result.internal_error is False
    assert result.reason == "Stopped at perceive: this step ran out of time."


async def test_g0_error_is_retried(runs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_later_stages(monkeypatch)
    attempts: list[int] = []

    async def flaky_decide(input_blocks: list[dict], questions: dict[str, str]) -> dict[str, float]:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("decision api hiccup")
        return {"g0": 0.9}

    async def fake_perceive(*args: object, **kwargs: object) -> ReproSpec:
        return SPEC

    monkeypatch.setattr(gemini, "decide", flaky_decide)
    monkeypatch.setattr(perceive, "perceive", fake_perceive)
    result = await pipeline.run_pipeline(make_video(tmp_path / "v.mp4"), APP, "todomvc", "r_test_g0retry")
    assert len(attempts) == 2
    assert result.status == "NO_VERIFIED_FIX" and result.internal_error is False
    assert result.gates[0].gate == "G0" and result.gates[0].passed
    events = read_events(runs_dir / "r_test_g0retry")
    assert any(e.type == "log" and e.stage == "g0" for e in events)


async def test_crlf_repro_test_does_not_break_the_freeze_check(
    runs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows the repro test is written with CRLF line endings. The freeze hash must still match."""
    stub_later_stages(monkeypatch)

    async def crlf_repro(video_ref, spec, app_dir, bundle_dir, emit=None):
        (bundle_dir / "repro_test.py").write_bytes(b"def test_repro(page):\r\n    pass\r\n")
        return bundle_dir / "repro_test.py", [GateResult(gate="G1", passed=True), GateResult(gate="G1B", passed=True)]

    async def fake_g0(video_ref):
        return GateResult(gate="G0", passed=True, p_yes=0.9)

    async def fake_perceive(*args: object, **kwargs: object) -> ReproSpec:
        return SPEC

    monkeypatch.setattr(repro, "establish_repro", crlf_repro)
    monkeypatch.setattr(perceive, "is_defect", fake_g0)
    monkeypatch.setattr(perceive, "perceive", fake_perceive)
    result = await pipeline.run_pipeline(make_video(tmp_path / "v.mp4"), APP, "todomvc", "r_test_crlf")
    assert result.internal_error is False
    assert result.status == "NO_VERIFIED_FIX" and result.stage_reached == "select"
    assert "changed after it was frozen" not in result.reason


def _cand(cid: str, content: str, g2: bool = True, g3: bool = True, g4: float | None = 0.2, lines: int = 2) -> Candidate:
    gates_ = [GateResult(gate="G2", passed=g2, candidate_id=cid)]
    if g2:
        gates_.append(GateResult(gate="G3", passed=g3, candidate_id=cid))
    if g2 and g3 and g4 is not None:
        gates_.append(GateResult(gate="G4", passed=g4 >= config.TH_G4_DEFECT_GONE, p_yes=g4, candidate_id=cid))
    survived = all(g.passed for g in gates_) and len(gates_) == 3
    return Candidate(
        id=cid, source="initial", patch=Patch(files={"app.js": content}), gates=gates_, survived=survived,
        lines_changed=lines, files_changed=["app.js"],
    )


def test_select_winner_prefers_a_g4_pass() -> None:
    cands = [_cand("c1", "A"), _cand("c2", "A"), _cand("c3", "A"), _cand("c4", "B", g4=0.9)]
    winner, basis = pipeline.select_winner(cands)
    assert winner is not None and winner.id == "c4" and basis == "visual check passed"


def test_select_winner_consensus_when_g4_is_inconclusive() -> None:
    cands = [_cand("c1", "A"), _cand("c2", "A"), _cand("c3", "A"), _cand("c4", "A")]
    winner, basis = pipeline.select_winner(cands)
    assert winner is not None and winner.id == "c1"
    assert basis == "4 of 4 independent fixes agree; visual check inconclusive"
    three = [_cand("c1", "A"), _cand("c2", "B"), _cand("c3", "A"), _cand("c4", "A")]
    winner, basis = pipeline.select_winner(three)
    assert winner is not None and winner.id == "c1" and basis.startswith("3 of 4 independent fixes agree")


def test_select_winner_needs_enough_identical_patches_that_passed_g2_and_g3() -> None:
    assert pipeline.select_winner([_cand("c1", "A"), _cand("c2", "A"), _cand("c3", "B"), _cand("c4", "C")]) == (None, "")
    # identical text, but one failed G2 and one failed G3: only two functional candidates agree
    cands = [_cand("c1", "A"), _cand("c2", "A"), _cand("c3", "A", g2=False), _cand("c4", "A", g3=False)]
    assert pipeline.select_winner(cands) == (None, "")
    # same content in a different file is not the same patch
    other = _cand("c3", "A")
    other.patch = Patch(files={"view.js": "A"})
    assert pipeline.select_winner([_cand("c1", "A"), _cand("c2", "A"), other]) == (None, "")
    assert pipeline.select_winner([]) == (None, "")
