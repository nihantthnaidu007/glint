"""The Glint pipeline. run_pipeline() is the only entry point to the product and it never raises."""
from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import logging
import os
import secrets
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from glint import config, gates, gemini, media, patch, perceive, repro
from glint.gemini import VideoRef
from glint.schemas import (
    Candidate,
    Event,
    EventType,
    GateResult,
    ReproSpec,
    RunResult,
    Stage,
    Status,
    VideoInfo,
)

log = logging.getLogger("glint.pipeline")

EmitFn = Callable[[Event], Awaitable[None]]

VIDEO_NAME = "video.mp4"
REPRO_NAME = "repro_test.py"
REPRO_SHOT = "shots/repro_fail.png"


def new_run_id() -> str:
    """A fresh run id: r_YYYYMMDD_HHMMSS_<4 hex>."""
    return f"r_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"


class _Stop(Exception):
    """Ends the run early with a decided status and reason. Not an error."""

    def __init__(self, status: Status, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _StageTimeout(Exception):
    """A single stage used up its own time limit."""


class _Run:
    """Mutable state of one pipeline run, plus event emission."""

    def __init__(self, run_id: str, app_id: str, bundle: Path, emit: EmitFn | None) -> None:
        self.run_id = run_id
        self.app_id = app_id
        self.bundle = bundle
        self._emit_fn = emit
        self.seq = 0
        self.stage: Stage = "input"
        self.started = time.monotonic()
        self.created_at = datetime.now(UTC)
        self.video: VideoInfo | None = None
        self.spec: ReproSpec | None = None
        self.repro_test: str | None = None
        self.repro_hash: str | None = None
        self.candidates: list[Candidate] = []
        self.gates: list[GateResult] = []

    @property
    def repro_ready(self) -> bool:
        return self.repro_test is not None

    async def emit(self, event_type: EventType, data: dict[str, Any]) -> None:
        """Append one Event to events.jsonl and pass it to the caller's emit function. Never raises."""
        self.seq += 1
        event = Event(
            run_id=self.run_id, seq=self.seq, ts=datetime.now(UTC), type=event_type, stage=self.stage, data=data
        )
        try:
            with (self.bundle / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(event.model_dump_json() + "\n")
        except OSError as exc:
            log.warning("could not append to events.jsonl: %s", type(exc).__name__)
        if self._emit_fn is not None:
            try:
                await self._emit_fn(event)
            except Exception:
                log.exception("emit callback failed")

    async def stage_emit(self, event_type: str, data: dict[str, Any]) -> None:
        """The (event_type, data) callback handed to stages that do not own run_id or seq."""
        await self.emit(event_type, data)  # type: ignore[arg-type]


async def _timed[T](stage_key: str, call: Awaitable[T]) -> T:
    """Await call under the stage's own time limit from config.TIMEOUT_S."""
    try:
        async with asyncio.timeout(config.TIMEOUT_S[stage_key]):
            return await call
    except TimeoutError as exc:
        raise _StageTimeout(stage_key) from exc


async def _with_one_retry[T](
    run: _Run, label: str, first_key: str, first: Callable[[], Awaitable[T]], retry_key: str, retry: Callable[[], Awaitable[T]]
) -> T:
    """Run first() under its stage limit; if it times out or raises, log it and run retry() once."""
    try:
        return await _timed(first_key, first())
    except _StageTimeout:
        why = "took too long"
    except Exception as exc:  # noqa: BLE001 - one failed model call gets one more chance
        log.warning("%s failed (%s), retrying once", label, type(exc).__name__)
        why = f"failed ({type(exc).__name__})"
    await run.emit("log", {"message": f"{label} {why}, retrying once."})
    return await _timed(retry_key, retry())


def _lines_changed(app_dir: Path, c: Candidate) -> int:
    """Added plus removed lines of a candidate's patch against the app, for display before the gates run."""
    if c.patch is None:
        return 0
    total = 0
    for rel, new_text in c.patch.files.items():
        try:
            old_text = (app_dir / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            old_text = ""
        diff = difflib.ndiff(old_text.splitlines(), new_text.splitlines())
        total += sum(1 for line in diff if line.startswith(("+ ", "- ")))
    return total


def _g4_p(c: Candidate) -> float:
    return next((g.p_yes or 0.0 for g in c.gates if g.gate == "G4"), 0.0)


def _gate_passed(c: Candidate, gate: str) -> bool:
    return any(g.gate == gate and g.passed for g in c.gates)


def select_winner(candidates: list[Candidate]) -> tuple[Candidate | None, str]:
    """Pick the verified candidate, if any. Returns (winner, basis text for the reason sentence).

    A candidate that passed G2 and G3 is verified if G4 passed, or if its patch is identical (same files,
    same contents) to at least CONSENSUS_MIN - 1 other candidates that also passed G2 and G3.
    G4-passing winners are preferred: fewest changed lines, then highest G4 probability.
    """
    functional = [c for c in candidates if c.patch is not None and _gate_passed(c, "G2") and _gate_passed(c, "G3")]
    visual = sorted((c for c in functional if _gate_passed(c, "G4")), key=lambda c: (c.lines_changed, -_g4_p(c)))
    if visual:
        return visual[0], "visual check passed"
    groups: dict[tuple[tuple[str, str], ...], list[Candidate]] = {}
    for c in functional:
        assert c.patch is not None
        groups.setdefault(tuple(sorted(c.patch.files.items())), []).append(c)
    agreeing = max(groups.values(), key=len, default=[])
    if len(agreeing) >= config.CONSENSUS_MIN:
        with_patch = sum(1 for c in candidates if c.patch is not None)
        basis = f"{len(agreeing)} of {with_patch} independent fixes agree; visual check inconclusive"
        return agreeing[0], basis
    return None, ""


def _select(run: _Run) -> tuple[Status, str | None, str]:
    """Verdict from the candidates checked so far."""
    winner, basis = select_winner(run.candidates)
    if winner is None:
        return "NO_VERIFIED_FIX", None, "No candidate fix passed every check, so Glint returns the failing test only."
    lines = "1 line" if winner.lines_changed == 1 else f"{winner.lines_changed} lines"
    files = ", ".join(winner.files_changed) or "the app"
    return "VERIFIED_FIX", winner.id, f"Verified fix {winner.id} changed {lines} in {files} ({basis})."


# --- stages -------------------------------------------------------------------------------------


async def _stage_input(run: _Run, video_path: Path, app_dir: Path) -> VideoRef:
    run.stage = "input"
    await run.emit("run.started", {"app_id": run.app_id})
    if not app_dir.is_dir():
        raise _Stop("REJECTED_INPUT", "The app directory does not exist.")
    if not video_path.is_file():
        raise _Stop("REJECTED_INPUT", "The video file does not exist.")
    original = run.bundle / f"input{video_path.suffix.lower()}"
    if video_path.resolve() != original.resolve():
        try:
            shutil.copyfile(video_path, original)
        except OSError as exc:
            log.warning("could not copy the input video into the bundle: %s", type(exc).__name__)
    try:
        info = await media.normalize(video_path, run.bundle / VIDEO_NAME)
    except media.InputRejected as exc:
        raise _Stop("REJECTED_INPUT", f"The video was rejected: {exc.message}") from exc
    run.video = info.model_copy(update={"path": VIDEO_NAME})
    await run.emit("input.normalized", {**run.video.model_dump(), "video": VIDEO_NAME})
    return await gemini.upload_video(run.bundle / VIDEO_NAME)


async def _stage_g0(run: _Run, video_ref: VideoRef) -> None:
    run.stage = "g0"
    gate = await _with_one_retry(
        run, "The bug check (G0)", "g0", lambda: perceive.is_defect(video_ref), "g0", lambda: perceive.is_defect(video_ref)
    )
    run.gates.append(gate)
    await run.emit("gate.result", gate.model_dump())
    if not gate.passed:
        raise _Stop(
            "ABSTAIN",
            f"The recording does not clearly show a bug (p={gate.p_yes:.2f}), so Glint did not attempt a fix.",
        )


async def _stage_perceive(run: _Run, video_ref: VideoRef, app_dir: Path) -> ReproSpec:
    run.stage = "perceive"
    duration = run.video.duration_s if run.video else None
    spec = await _with_one_retry(
        run,
        "Watching the recording",
        "perceive",
        lambda: perceive.perceive(video_ref, app_dir, duration),
        "perceive_retry",
        lambda: perceive.perceive(video_ref, app_dir, duration, config.PERCEIVE_RETRY_THINKING),
    )
    run.spec = spec
    (run.bundle / "spec.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    frames: list[str] = []
    for t in spec.evidence_timestamps_s:
        rel = f"frames/t_{t:.1f}.png"
        try:
            (run.bundle / "frames").mkdir(exist_ok=True)
            await media.grab_frame(run.bundle / VIDEO_NAME, t, run.bundle / rel)
            frames.append(rel)
        except Exception as exc:  # noqa: BLE001 - a missing marker frame is cosmetic
            log.warning("could not grab frame at %.1fs: %s", t, type(exc).__name__)
    await run.emit("spec.ready", {**spec.model_dump(), "frames": frames})
    return spec


async def _stage_repro(run: _Run, video_ref: VideoRef, spec: ReproSpec, app_dir: Path) -> Path:
    run.stage = "repro"
    test_path, repro_gates = await repro.establish_repro(video_ref, spec, app_dir, run.bundle, run.stage_emit)
    run.gates.extend(repro_gates)
    if test_path is None:
        why = next((g.detail for g in repro_gates if not g.passed), "no test failed on the app")
        why = " ".join(why.split())[:160].rstrip(".")
        raise _Stop("ABSTAIN", f"Glint could not reproduce the bug with an automated test ({why}).")
    # Hash the bytes on disk, the same way the check before select does. Hashing the decoded text instead
    # breaks on Windows, where the file is written with CRLF line endings but read back as LF.
    run.repro_hash = hashlib.sha256(test_path.read_bytes()).hexdigest()
    code = test_path.read_text(encoding="utf-8")
    (run.bundle / "repro_test.sha256").write_text(run.repro_hash + "\n", encoding="utf-8")
    run.repro_test = REPRO_NAME
    await run.emit("repro.ready", {"test_code": code, "screenshot": REPRO_SHOT})
    return test_path


async def _check_and_report(
    run: _Run, c: Candidate, app_dir: Path, test_path: Path, video_ref: VideoRef, spec: ReproSpec
) -> None:
    """Run one candidate through the gates and emit its events. The candidate is already in run.candidates."""
    await run.emit(
        "candidate.created",
        {
            "candidate_id": c.id,
            "source": c.source,
            "files_changed": sorted(c.patch.files) if c.patch else [],
            "lines_changed": _lines_changed(app_dir, c),
        },
    )
    await gates.check_candidate(c, app_dir, test_path, video_ref, run.bundle, spec)
    for gate in c.gates:
        await run.emit("gate.result", gate.model_dump())
    diff_rel = f"diffs/{c.id}.diff"
    shot_rel = f"shots/{c.id}_after.png"
    await run.emit(
        "candidate.final",
        {
            "candidate_id": c.id,
            "survived": c.survived,
            "reject_reason": c.reject_reason,
            "diff": diff_rel if (run.bundle / diff_rel).is_file() else None,
            "screenshot": shot_rel if (run.bundle / shot_rel).is_file() else None,
        },
    )


async def _check_all(
    run: _Run, new: list[Candidate], app_dir: Path, test_path: Path, video_ref: VideoRef, spec: ReproSpec
) -> None:
    run.candidates.extend(new)
    await asyncio.gather(*(_check_and_report(run, c, app_dir, test_path, video_ref, spec) for c in new))


async def _stage_candidates(
    run: _Run, video_ref: VideoRef, spec: ReproSpec, app_dir: Path, test_path: Path, allow_repair: bool
) -> None:
    run.stage = "candidates"
    initial = await patch.candidates(spec, app_dir, config.N_CANDIDATES)
    await _check_all(run, initial, app_dir, test_path, video_ref, spec)
    if not allow_repair or select_winner(run.candidates)[0] is not None:
        return
    # A repair round can only help when some candidate failed a check that a different patch could pass.
    # When every candidate already passed G2 and G3 and was stopped only by G4, new patches would be the same.
    if not any(not g.passed and g.gate in ("G2", "G3") for c in initial for g in c.gates):
        return

    run.stage = "repair"
    failed_gates = sorted({g.gate for c in initial for g in c.gates if not g.passed})
    await run.emit("repair.started", {"feedback_gates": failed_gates})
    repaired = await patch.repair(spec, app_dir, initial)
    await _check_all(run, repaired, app_dir, test_path, video_ref, spec)


async def _execute(run: _Run, video_path: Path, app_dir: Path, allow_repair: bool) -> tuple[Status, str | None, str]:
    video_ref = await _stage_input(run, video_path, app_dir)
    await _stage_g0(run, video_ref)
    spec = await _stage_perceive(run, video_ref, app_dir)
    test_path = await _stage_repro(run, video_ref, spec, app_dir)
    await _stage_candidates(run, video_ref, spec, app_dir, test_path, allow_repair)
    current = hashlib.sha256(test_path.read_bytes()).hexdigest()
    if current != run.repro_hash:
        raise RuntimeError("the frozen repro test changed after it was frozen")
    run.stage = "select"
    return _select(run)


def _stopped_early(run: _Run, why: str) -> tuple[Status, str | None, str]:
    """Verdict when the run was cut short. A candidate that already passed every check still counts."""
    stage = run.stage
    if run.repro_ready:
        status, winner_id, reason = _select(run)
        if status == "VERIFIED_FIX":
            return status, winner_id, reason
        return "NO_VERIFIED_FIX", None, f"Stopped at {stage}: {why}."
    return "ABSTAIN", None, f"Stopped at {stage}: {why}."


# --- entry point --------------------------------------------------------------------------------


async def run_pipeline(
    video_path: Path,
    app_dir: Path,
    app_id: str,
    run_id: str,
    emit: EmitFn | None = None,
    *,
    allow_repair: bool = True,
) -> RunResult:
    """Never raises. Every failure becomes a RunResult with a status and a reason.
    Writes the evidence bundle to RUNS_DIR/<run_id>/ (section 8) and events.jsonl as it goes."""
    bundle = Path(config.RUNS_DIR) / run_id
    run = _Run(run_id, app_id, bundle, emit)
    internal_error = False
    status: Status
    winner_id: str | None
    try:
        bundle.mkdir(parents=True, exist_ok=True)
        gemini.bind_run(run_id)
        async with asyncio.timeout(config.RUN_BUDGET_S):
            status, winner_id, reason = await _execute(run, Path(video_path), Path(app_dir), allow_repair)
    except _Stop as stop:
        status, winner_id, reason = stop.status, None, stop.reason
    except _StageTimeout:
        status, winner_id, reason = _stopped_early(run, "this step ran out of time")
    except TimeoutError:
        status, winner_id, reason = _stopped_early(run, "time budget")
    except Exception as exc:
        log.exception("run %s failed at stage %s", run_id, run.stage)
        internal_error = True
        status = "NO_VERIFIED_FIX" if run.repro_ready else "ABSTAIN"
        winner_id, reason = None, f"Stopped at {run.stage}: {type(exc).__name__}."

    stage_reached: Stage = run.stage
    result = RunResult(
        run_id=run_id,
        app_id=app_id,
        status=status,
        reason=reason,
        stage_reached=stage_reached,
        video=run.video,
        spec=run.spec,
        repro_test=run.repro_test,
        candidates=run.candidates,
        winner_id=winner_id,
        gates=run.gates,
        internal_error=internal_error,
        created_at=run.created_at,
    )
    try:
        run.stage = "select"
        await run.emit("verdict", {"status": status, "winner_id": winner_id, "reason": reason})
        run.stage = "done"
        result.usage = gemini.usage_snapshot(run_id)
        result.latency_s = round(time.monotonic() - run.started, 2)
        # result.json is written before run.finished is emitted, so a client that reacts to run.finished
        # always finds the result on disk.
        (bundle / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        await run.emit(
            "run.finished",
            {"status": status, "latency_s": result.latency_s, "cost_usd": round(result.usage.cost_usd, 4)},
        )
    except Exception:
        log.exception("run %s could not finish writing its bundle", run_id)
        result.internal_error = True
    return result


# --- CLI ----------------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Glint pipeline on one bug video and one app directory.")
    parser.add_argument("--video", type=Path, required=True, help="screen recording of the bug")
    parser.add_argument("--app", type=Path, required=True, help="directory of the buggy app")
    parser.add_argument("--app-id", required=True, help="id of the app, for example todomvc")
    parser.add_argument("--no-repair", action="store_true", help="skip the repair round")
    parser.add_argument(
        "--expect-any",
        action="store_true",
        help="exit 0 for any status as long as a result was produced and nothing crashed",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("GLINT_LOG_LEVEL", "INFO").upper(), format="%(levelname)s %(name)s: %(message)s"
    )
    run_id = new_run_id()
    result = asyncio.run(
        run_pipeline(args.video, args.app, args.app_id, run_id, allow_repair=not args.no_repair)
    )
    print(f"run_id:  {result.run_id}")
    print(f"status:  {result.status}")
    print(f"reason:  {result.reason}")
    print(f"stage:   {result.stage_reached}")
    for gate in result.gates:
        p_yes = "" if gate.p_yes is None else f" p_yes={gate.p_yes:.2f}"
        print(f"gate:    {gate.gate} {'pass' if gate.passed else 'FAIL'}{p_yes}")
    for c in result.candidates:
        checks = " ".join(
            f"{g.gate}={'pass' if g.passed else 'FAIL'}" + ("" if g.p_yes is None else f"({g.p_yes:.2f})")
            for g in c.gates
        )
        print(f"cand:    {c.id} survived={c.survived} lines={c.lines_changed} {checks} {c.reject_reason[:90]}")
    print(f"winner:  {result.winner_id}")
    print(f"latency: {result.latency_s:.1f}s")
    print(f"cost:    ${result.usage.cost_usd:.4f} ({result.usage.calls} model calls)")
    print(f"bundle:  {Path(config.RUNS_DIR) / result.run_id}")
    # A result with any status is a success for the CLI; only a caught crash is a failure.
    sys.exit(1 if result.internal_error else 0)


if __name__ == "__main__":
    main()
