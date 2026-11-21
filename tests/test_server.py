"""Tests for glint/server.py HTTP routes, error handling, SSE streaming, and replay."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from glint import config, schemas, server

BASE_TS = datetime(2026, 11, 21, 14, 30, 0, tzinfo=UTC)


def _make_event(
    run_id: str,
    seq: int,
    event_type: schemas.EventType,
    stage: schemas.Stage,
    data: dict[str, Any] | None = None,
    *,
    offset_s: float = 0.0,
) -> schemas.Event:
    return schemas.Event(
        run_id=run_id,
        seq=seq,
        ts=BASE_TS + timedelta(seconds=offset_s),
        type=event_type,
        stage=stage,
        data=data or {},
    )


def _make_result(run_id: str, app_id: str = "todomvc", status: schemas.Status = "VERIFIED_FIX") -> schemas.RunResult:
    return schemas.RunResult(
        run_id=run_id,
        app_id=app_id,
        status=status,
        reason="Verified fix c1 changed 1 line in js/app.js.",
        stage_reached="done",
        winner_id="c1" if status == "VERIFIED_FIX" else None,
        latency_s=1.2,
        created_at=BASE_TS,
    )


def _parse_sse_frames(raw_text: str) -> list[dict[str, str]]:
    frames: list[dict[str, str]] = []
    for block in raw_text.strip().split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(":"):
            frames.append({"comment": block})
            continue
        frame: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                frame[key.strip()] = value.strip()
        if frame:
            frames.append(frame)
    return frames


@pytest.fixture
def isolated_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "apps" / "todomvc").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "RUNS_DIR", str(runs_dir))
    monkeypatch.setattr(server, "REPO_ROOT", tmp_path)
    server._runs.clear()
    server._active_live_run_id = None
    yield tmp_path
    for state in list(server._runs.values()):
        if state.task is not None and not state.task.done():
            state.task.cancel()
    server._runs.clear()
    server._active_live_run_id = None


@pytest.fixture
def client(isolated_server: Path) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_health_and_apps(isolated_server: Path, client: httpx.AsyncClient) -> None:
    async with client:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "ok",
            "models": {
                "neon": config.NEON,
                "hydrogen": config.HYDROGEN,
                "decision": config.DECISION,
            },
        }

        apps_resp = await client.get("/api/apps")
        assert apps_resp.status_code == 200
        assert apps_resp.json() == [{"id": "todomvc", "name": "TodoMVC (original)", "url": None}]

        (isolated_server / "live" / "app").mkdir(parents=True)
        apps_with_live = await client.get("/api/apps")
        assert apps_with_live.status_code == 200
        assert apps_with_live.json() == [
            {"id": "live", "name": "Live demo bug", "url": "http://localhost:8100"},
            {"id": "todomvc", "name": "TodoMVC (original)", "url": None},
        ]


async def test_create_run_validation_and_pipeline_not_ready(
    isolated_server: Path,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with client:
        bad_app = await client.post(
            "/api/runs",
            data={"app_id": "unknown", "source": "upload"},
            files={"video": ("bug.mp4", b"fake-video", "video/mp4")},
        )
        assert bad_app.status_code == 400
        assert bad_app.json()["error"]["code"] == "UNKNOWN_APP"

        bad_live = await client.post(
            "/api/runs",
            data={"app_id": "live", "source": "upload"},
            files={"video": ("bug.mp4", b"fake-video", "video/mp4")},
        )
        assert bad_live.status_code == 400
        assert bad_live.json()["error"]["code"] == "UNKNOWN_APP"

        bad_ext = await client.post(
            "/api/runs",
            data={"app_id": "todomvc", "source": "upload"},
            files={"video": ("bug.txt", b"not-a-video", "text/plain")},
        )
        assert bad_ext.status_code == 400
        assert bad_ext.json()["error"]["code"] == "BAD_EXTENSION"

        monkeypatch.setitem(sys.modules, "glint.pipeline", None)
        not_ready = await client.post(
            "/api/runs",
            data={"app_id": "todomvc", "source": "upload"},
            files={"video": ("bug.mp4", b"fake-video", "video/mp4")},
        )
        assert not_ready.status_code == 503
        assert not_ready.json()["error"]["code"] == "PIPELINE_NOT_READY"


async def test_create_run_too_large_and_in_progress(
    isolated_server: Path,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_pipeline(
        video_path: Path,
        app_dir: Path,
        app_id: str,
        run_id: str,
        emit: Any,
    ) -> schemas.RunResult:
        started.set()
        await emit(_make_event(run_id, 1, "run.started", "input", {"app_id": app_id}))
        await release.wait()
        await emit(
            _make_event(run_id, 2, "run.finished", "done", {"status": "VERIFIED_FIX", "latency_s": 0.1, "cost_usd": 0.0})
        )
        return _make_result(run_id, app_id)

    counter = {"n": 0}

    def fake_new_run_id() -> str:
        counter["n"] += 1
        return f"r_20261121_14300{counter['n']}_abcd"

    monkeypatch.setitem(
        sys.modules,
        "glint.pipeline",
        SimpleNamespace(run_pipeline=fake_run_pipeline, new_run_id=fake_new_run_id),
    )

    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    oversized = b"x" * (1024 * 1024 + 64)

    async with client:
        too_large = await client.post(
            "/api/runs",
            data={"app_id": "todomvc", "source": "upload"},
            files={"video": ("big.mp4", oversized, "video/mp4")},
        )
        assert too_large.status_code == 413
        assert too_large.json()["error"]["code"] == "VIDEO_TOO_LARGE"
        assert not (isolated_server / "runs" / "r_20261121_143001_abcd").exists()

        first = await client.post(
            "/api/runs",
            data={"app_id": "todomvc", "source": "record"},
            files={"video": ("clip.webm", b"webm-bytes", "video/webm")},
        )
        assert first.status_code == 202
        run_id = first.json()["run_id"]
        assert (isolated_server / "runs" / run_id / "input.webm").read_bytes() == b"webm-bytes"

        await asyncio.wait_for(started.wait(), timeout=2.0)

        running_status = await client.get(f"/api/runs/{run_id}")
        assert running_status.status_code == 200
        assert running_status.json() == {"run_id": run_id, "running": True}

        second = await client.post(
            "/api/runs",
            data={"app_id": "todomvc", "source": "upload"},
            files={"video": ("second.mp4", b"mp4-bytes", "video/mp4")},
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "RUN_IN_PROGRESS"

        release.set()
        task = server._runs[run_id].task
        assert task is not None
        await asyncio.wait_for(task, timeout=2.0)

        done_status = await client.get(f"/api/runs/{run_id}")
        assert done_status.status_code == 200
        assert done_status.json()["status"] == "VERIFIED_FIX"
        assert done_status.json()["run_id"] == run_id


async def test_sse_late_subscriber_no_duplicates_or_losses(
    isolated_server: Path,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mid_reached = asyncio.Event()
    allow_finish = asyncio.Event()
    run_id = "r_20261121_143099_1111"

    async def fake_run_pipeline(
        video_path: Path,
        app_dir: Path,
        app_id: str,
        rid: str,
        emit: Any,
    ) -> schemas.RunResult:
        # Simulate pipeline writing events.jsonl itself AND calling emit for the first two events.
        ev1 = _make_event(rid, 1, "run.started", "input", {"app_id": app_id})
        ev2 = _make_event(rid, 2, "spec.ready", "perceive", {"summary": "Bug", "confidence": 0.9})
        events_path = isolated_server / "runs" / rid / "events.jsonl"
        events_path.write_text(ev1.model_dump_json() + "\n" + ev2.model_dump_json() + "\n", encoding="utf-8")
        await emit(ev1)
        await emit(ev2)
        mid_reached.set()
        await allow_finish.wait()
        ev3 = _make_event(rid, 3, "verdict", "select", {"status": "VERIFIED_FIX", "winner_id": "c1", "reason": "ok"})
        ev4 = _make_event(rid, 4, "run.finished", "done", {"status": "VERIFIED_FIX", "latency_s": 0.5, "cost_usd": 0.1})
        await emit(ev3)
        await emit(ev4)
        return _make_result(rid, app_id)

    monkeypatch.setitem(
        sys.modules,
        "glint.pipeline",
        SimpleNamespace(run_pipeline=fake_run_pipeline, new_run_id=lambda: run_id),
    )

    async with client:
        post_resp = await client.post(
            "/api/runs",
            data={"app_id": "todomvc", "source": "upload"},
            files={"video": ("bug.mp4", b"video-data", "video/mp4")},
        )
        assert post_resp.status_code == 202
        await asyncio.wait_for(mid_reached.wait(), timeout=2.0)

        async def trigger_finish() -> None:
            await asyncio.sleep(0.05)
            allow_finish.set()

        finisher = asyncio.create_task(trigger_finish())
        async with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
            assert stream.status_code == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
            raw = (await stream.aread()).decode("utf-8")
        await finisher

        frames = _parse_sse_frames(raw)
        seqs = [int(f["id"]) for f in frames if "id" in f]
        types = [f["event"] for f in frames if "event" in f]
        assert seqs == [1, 2, 3, 4]
        assert types == ["run.started", "spec.ready", "verdict", "run.finished"]

        # Ensure events.jsonl has no duplicate lines even though fake_run_pipeline wrote 1 & 2 itself.
        file_events = [
            schemas.Event.model_validate_json(line)
            for line in (isolated_server / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert [ev.seq for ev in file_events] == [1, 2, 3, 4]


async def test_sse_ping_and_missing_run(
    isolated_server: Path,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "SSE_PING_INTERVAL_S", 0.03)
    run_id = "r_20261121_143088_2222"
    run_dir = isolated_server / "runs" / run_id
    run_dir.mkdir(parents=True)
    state = server._RunState(run_id=run_id, run_dir=run_dir)
    server._runs[run_id] = state

    async def finish_later() -> None:
        await asyncio.sleep(0.09)
        server._publish_event(
            state,
            _make_event(run_id, 1, "run.finished", "done", {"status": "ABSTAIN", "latency_s": 0.1, "cost_usd": 0.0}),
        )
        server._close_subscribers(state)

    async with client:
        missing = await client.get("/api/runs/missing_run/events")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "RUN_NOT_FOUND"

        missing_status = await client.get("/api/runs/missing_run")
        assert missing_status.status_code == 404
        assert missing_status.json()["error"]["code"] == "RUN_NOT_FOUND"

        finisher = asyncio.create_task(finish_later())
        async with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
            assert stream.status_code == 200
            raw = (await stream.aread()).decode("utf-8")
        await finisher
        frames = _parse_sse_frames(raw)
        assert any(f.get("comment") == ": ping" for f in frames)
        assert [f["event"] for f in frames if "event" in f] == ["run.finished"]


async def test_files_route_and_path_traversal(
    isolated_server: Path,
    client: httpx.AsyncClient,
) -> None:
    run_id = "r_20261121_143077_3333"
    run_dir = isolated_server / "runs" / run_id
    (run_dir / "shots").mkdir(parents=True)
    (run_dir / "shots" / "repro_fail.png").write_bytes(b"png-bytes")
    (isolated_server / "secret.txt").write_text("top-secret", encoding="utf-8")

    async with client:
        ok = await client.get(f"/api/runs/{run_id}/files/shots/repro_fail.png")
        assert ok.status_code == 200
        assert ok.content == b"png-bytes"

        missing_file = await client.get(f"/api/runs/{run_id}/files/shots/nope.png")
        assert missing_file.status_code == 404
        assert missing_file.json()["error"]["code"] == "FILE_NOT_FOUND"

        traversal_1 = await client.get(f"/api/runs/{run_id}/files/..%2F..%2Fsecret.txt")
        assert traversal_1.status_code == 400
        assert traversal_1.json()["error"]["code"] == "BAD_PATH"

        traversal_2 = await client.get(f"/api/runs/{run_id}/files/shots/..%2Frepro_fail.png")
        assert traversal_2.status_code == 400
        assert traversal_2.json()["error"]["code"] == "BAD_PATH"


async def test_replay_route_and_capped_gaps(
    isolated_server: Path,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src_id = "r_20261121_143066_4444"
    src_dir = isolated_server / "runs" / src_id
    (src_dir / "shots").mkdir(parents=True)
    (src_dir / "shots" / "repro_fail.png").write_bytes(b"png-bytes")
    events = [
        _make_event(src_id, 1, "run.started", "input", {"app_id": "todomvc"}, offset_s=0.0),
        _make_event(src_id, 2, "spec.ready", "perceive", {"summary": "Bug", "confidence": 0.8}, offset_s=0.5),
        _make_event(
            src_id,
            3,
            "run.finished",
            "done",
            {"status": "VERIFIED_FIX", "latency_s": 12.0, "cost_usd": 0.2},
            offset_s=12.5,
        ),
    ]
    (src_dir / "events.jsonl").write_text("\n".join(e.model_dump_json() for e in events) + "\n", encoding="utf-8")
    (src_dir / "result.json").write_text(_make_result(src_id).model_dump_json(), encoding="utf-8")

    # Works even if glint.pipeline is not present.
    monkeypatch.setitem(sys.modules, "glint.pipeline", None)

    recorded_sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        recorded_sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)

    async with client:
        missing = await client.post("/api/runs/missing_run/replay")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "RUN_NOT_FOUND"

        resp = await client.post(f"/api/runs/{src_id}/replay")
        assert resp.status_code == 202
        new_id = resp.json()["run_id"]
        assert new_id != src_id
        assert new_id.startswith("r_")

        async with client.stream("GET", f"/api/runs/{new_id}/events") as stream:
            assert stream.status_code == 200
            raw = (await stream.aread()).decode("utf-8")

        task = server._runs[new_id].task
        assert task is not None
        await asyncio.wait_for(task, timeout=2.0)

        frames = _parse_sse_frames(raw)
        parsed = [schemas.Event.model_validate_json(f["data"]) for f in frames if "data" in f]
        assert [ev.seq for ev in parsed] == [1, 2, 3]
        assert all(ev.replay is True for ev in parsed)
        assert all(ev.run_id == new_id for ev in parsed)
        assert recorded_sleeps == [0.5, 3.0]

        copied_shot = await client.get(f"/api/runs/{new_id}/files/shots/repro_fail.png")
        assert copied_shot.status_code == 200
        assert copied_shot.content == b"png-bytes"

        replay_result = await client.get(f"/api/runs/{new_id}")
        assert replay_result.status_code == 200
        assert replay_result.json()["run_id"] == new_id


async def test_results_web_and_internal_error_handler(
    isolated_server: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        no_results = await client.get("/api/results")
        assert no_results.status_code == 200
        assert no_results.json() == {"available": False}

        summary_path = isolated_server / "eval" / "results" / "summary.json"
        summary_path.parent.mkdir(parents=True)
        summary_payload = {"available": True, "frozen_hash": "abc1234", "n_test_mutants": 20, "rows": []}
        summary_path.write_text(json.dumps(summary_payload), encoding="utf-8")

        with_results = await client.get("/api/results")
        assert with_results.status_code == 200
        assert with_results.json() == summary_payload

        no_web = await client.get("/")
        assert no_web.status_code == 404
        assert no_web.json()["error"]["code"] == "WEB_NOT_READY"

        web_dir = isolated_server / "web"
        web_dir.mkdir(parents=True)
        (web_dir / "index.html").write_text("<html><body>Glint</body></html>", encoding="utf-8")
        has_web = await client.get("/")
        assert has_web.status_code == 200
        assert "Glint" in has_web.text

        # Force an unexpected exception inside /api/results to test global 500 handler without stack traces.
        monkeypatch.setattr(server, "_summary_path", lambda: (_ for _ in ()).throw(RuntimeError("boom secret trace")))
        err_resp = await client.get("/api/results")
        assert err_resp.status_code == 500
        body = err_resp.json()
        assert body["error"]["code"] == "INTERNAL"
        assert "traceback" not in err_resp.text.lower()
        assert "boom secret trace" not in err_resp.text


async def test_webm_remux_keeps_unreadable_file_unchanged(tmp_path: Path) -> None:
    """A file ffmpeg cannot read (or no ffmpeg at all) is left exactly as uploaded, and no temp file remains."""
    src = tmp_path / "input.webm"
    src.write_bytes(b"not really a webm")
    await server._remux_webm_in_place(src)
    assert src.read_bytes() == b"not really a webm"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.webm"]
