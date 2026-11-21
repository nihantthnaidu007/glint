"""FastAPI server for Glint HTTP API, SSE event streaming, and static web UI."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import importlib
import json
import logging
from pathlib import Path, PurePosixPath, PureWindowsPath
import secrets
import shutil
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from glint import config
from glint.schemas import Event, RunResult

logger = logging.getLogger(__name__)

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
SSE_PING_INTERVAL_S: float = 10.0
REPLAY_MAX_GAP_S: float = 3.0
UPLOAD_CHUNK_BYTES: int = 1024 * 1024

_TERMINAL_STATUS_EVENTS = {"run.finished"}


class ApiError(Exception):
    """Structured API error rendered as {"error": {"code": ..., "message": ...}}."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class _RunState:
    run_id: str
    run_dir: Path
    events: list[Event] = field(default_factory=list)
    seen_seqs: set[int] = field(default_factory=set)
    subscribers: set[asyncio.Queue[Event | None]] = field(default_factory=set)
    finished: bool = False
    task: asyncio.Task[None] | None = None


_runs: dict[str, _RunState] = {}
_active_live_run_id: str | None = None


def _runs_dir() -> Path:
    return Path(config.RUNS_DIR)


def _live_app_dir() -> Path:
    return REPO_ROOT / "live" / "app"


def _todomvc_app_dir() -> Path:
    return REPO_ROOT / "apps" / "todomvc"


def _summary_path() -> Path:
    return REPO_ROOT / "eval" / "results" / "summary.json"


def _web_dir() -> Path:
    return REPO_ROOT / "web"


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def _list_apps() -> list[dict[str, Any]]:
    apps: list[dict[str, Any]] = []
    if _live_app_dir().is_dir():
        apps.append({"id": "live", "name": "Live demo bug", "url": "http://localhost:8100"})
    apps.append({"id": "todomvc", "name": "TodoMVC (original)", "url": None})
    return apps


def _resolve_app_dir(app_id: str) -> Path | None:
    if app_id == "live":
        live_dir = _live_app_dir()
        return live_dir if live_dir.is_dir() else None
    if app_id == "todomvc":
        return _todomvc_app_dir()
    return None


def _fallback_run_id() -> str:
    return f"r_{datetime.now(UTC):%Y%m%d_%H%M%S}_{secrets.token_hex(2)}"


def _load_pipeline() -> tuple[Any, Any]:
    try:
        pipeline_mod = importlib.import_module("glint.pipeline")
        run_pipeline = pipeline_mod.run_pipeline
        new_run_id = pipeline_mod.new_run_id
    except (ImportError, ModuleNotFoundError, AttributeError) as exc:
        raise ApiError(503, "PIPELINE_NOT_READY", "Pipeline is not ready yet.") from exc
    return run_pipeline, new_run_id


def _read_events_file(events_path: Path) -> list[Event]:
    if not events_path.is_file():
        return []
    events: list[Event] = []
    for raw_line in events_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        events.append(Event.model_validate_json(line))
    return events


def _append_event_if_missing(events_path: Path, event: Event) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    if events_path.is_file():
        existing = _read_events_file(events_path)
        if any(ev.seq == event.seq for ev in existing):
            return
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(event.model_dump_json() + "\n")


def _format_sse(event: Event) -> str:
    return f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"


def _publish_event(state: _RunState, event: Event, *, write_file: bool = True) -> None:
    if event.seq in state.seen_seqs:
        return
    state.seen_seqs.add(event.seq)
    state.events.append(event)
    if write_file:
        _append_event_if_missing(state.run_dir / "events.jsonl", event)
    for queue in list(state.subscribers):
        queue.put_nowait(event)


def _close_subscribers(state: _RunState) -> None:
    if state.finished:
        return
    state.finished = True
    for queue in list(state.subscribers):
        queue.put_nowait(None)


def _validate_relative_path(raw_path: str) -> None:
    if not raw_path:
        raise ApiError(400, "BAD_PATH", "File path is required.")
    if raw_path.startswith(("/", "\\")) or (len(raw_path) >= 2 and raw_path[1] == ":"):
        raise ApiError(400, "BAD_PATH", "Invalid file path.")
    posix_parts = PurePosixPath(raw_path).parts
    win_parts = PureWindowsPath(raw_path).parts
    if ".." in posix_parts or ".." in win_parts or ".." in raw_path.replace("\\", "/").split("/"):
        raise ApiError(400, "BAD_PATH", "Path traversal is not allowed.")


def _copy_bundle_for_replay(src_dir: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        if item.name in {"events.jsonl", "result.json"}:
            continue
        target = dst_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        elif item.is_file():
            shutil.copy2(item, target)


async def _run_live_pipeline(
    run_pipeline: Any,
    video_path: Path,
    app_dir: Path,
    app_id: str,
    run_id: str,
    state: _RunState,
) -> None:
    global _active_live_run_id

    async def emit(event: Event) -> None:
        _publish_event(state, event, write_file=True)

    try:
        result = await run_pipeline(video_path, app_dir, app_id, run_id, emit)
        result_path = state.run_dir / "result.json"
        if isinstance(result, RunResult) and not result_path.exists():
            result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unhandled error in background pipeline for run %s", run_id)
        result_path = state.run_dir / "result.json"
        if not result_path.exists():
            fallback = RunResult(
                run_id=run_id,
                app_id=app_id,
                status="ABSTAIN",
                reason="Internal pipeline error.",
                stage_reached="input",
                internal_error=True,
                created_at=datetime.now(UTC),
            )
            result_path.write_text(fallback.model_dump_json(indent=2), encoding="utf-8")
        if not any(ev.type == "run.finished" for ev in state.events):
            next_seq = max(state.seen_seqs, default=0) + 1
            finish_event = Event(
                run_id=run_id,
                seq=next_seq,
                ts=datetime.now(UTC),
                type="run.finished",
                stage="done",
                data={"status": "ABSTAIN", "latency_s": 0.0, "cost_usd": 0.0},
            )
            _publish_event(state, finish_event, write_file=True)
    finally:
        _close_subscribers(state)
        if _active_live_run_id == run_id:
            _active_live_run_id = None


async def _run_replay_task(
    source_events: list[Event],
    src_dir: Path,
    new_id: str,
    state: _RunState,
) -> None:
    try:
        prev_ts: datetime | None = None
        for ev in source_events:
            if prev_ts is not None:
                gap_s = max(0.0, (ev.ts - prev_ts).total_seconds())
                delay_s = min(gap_s, REPLAY_MAX_GAP_S)
                if delay_s > 0:
                    await asyncio.sleep(delay_s)
            prev_ts = ev.ts
            replayed = ev.model_copy(update={"run_id": new_id, "replay": True})
            _publish_event(state, replayed, write_file=True)

        src_result_path = src_dir / "result.json"
        dst_result_path = state.run_dir / "result.json"
        if src_result_path.is_file() and not dst_result_path.exists():
            result = RunResult.model_validate_json(src_result_path.read_text(encoding="utf-8"))
            updated = result.model_copy(update={"run_id": new_id})
            dst_result_path.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unhandled error in replay task for run %s", new_id)
    finally:
        _close_subscribers(state)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    for state in list(_runs.values()):
        if state.task is not None and not state.task.done():
            state.task.cancel()
            try:
                await state.task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error while cancelling task for run %s", state.run_id)


app = FastAPI(title="Glint", lifespan=_lifespan)
app.mount("/web", StaticFiles(directory=str(_web_dir()), check_dir=False), name="web")


@app.exception_handler(ApiError)
async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return _error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "NOT_FOUND" if exc.status_code == 404 else f"HTTP_{exc.status_code}"
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return _error_response(exc.status_code, code, message)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return _error_response(422, "VALIDATION_ERROR", "Invalid request parameters.")


@app.exception_handler(Exception)
async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
    logger.exception("Unhandled server exception")
    return _error_response(500, "INTERNAL", "Internal server error.")


@app.get("/api/health")
async def get_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "models": {
            "neon": config.NEON,
            "hydrogen": config.HYDROGEN,
            "decision": config.DECISION,
        },
    }


@app.get("/api/apps")
async def get_apps() -> list[dict[str, Any]]:
    return _list_apps()


@app.post("/api/runs", status_code=202)
async def create_run(
    video: UploadFile = File(...),
    app_id: str = Form(...),
    source: str = Form("upload"),
) -> JSONResponse:
    global _active_live_run_id

    app_dir = _resolve_app_dir(app_id)
    if app_dir is None:
        raise ApiError(400, "UNKNOWN_APP", f"Unknown app '{app_id}'.")

    if source not in {"record", "upload"}:
        raise ApiError(400, "BAD_SOURCE", f"Unsupported source '{source}'.")

    ext = Path(video.filename or "").suffix.lower()
    if ext not in config.ACCEPTED_EXT:
        accepted = ", ".join(sorted(config.ACCEPTED_EXT))
        raise ApiError(400, "BAD_EXTENSION", f"Unsupported file extension '{ext}'. Accepted: {accepted}.")

    if _active_live_run_id is not None:
        raise ApiError(429, "RUN_IN_PROGRESS", "A run is already in progress.")

    run_pipeline, new_run_id = _load_pipeline()
    run_id = new_run_id()
    run_dir = _runs_dir() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    input_path = run_dir / f"input{ext}"

    limit_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    total_bytes = 0
    try:
        with input_path.open("wb") as fh:
            while True:
                chunk = await video.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > limit_bytes:
                    raise ApiError(
                        413,
                        "VIDEO_TOO_LARGE",
                        f"Upload exceeds limit of {config.MAX_UPLOAD_MB} MB.",
                    )
                fh.write(chunk)
    except Exception:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    finally:
        await video.close()

    if _active_live_run_id is not None:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise ApiError(429, "RUN_IN_PROGRESS", "A run is already in progress.")

    state = _RunState(run_id=run_id, run_dir=run_dir)
    _runs[run_id] = state
    _active_live_run_id = run_id
    state.task = asyncio.create_task(
        _run_live_pipeline(run_pipeline, input_path, app_dir, app_id, run_id, state)
    )
    return JSONResponse(status_code=202, content={"run_id": run_id})


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> JSONResponse:
    run_dir = _runs_dir() / run_id
    result_path = run_dir / "result.json"
    if result_path.is_file():
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        return JSONResponse(status_code=200, content=payload)

    state = _runs.get(run_id)
    if (state is not None and not state.finished) or run_dir.is_dir():
        return JSONResponse(status_code=200, content={"run_id": run_id, "running": True})

    raise ApiError(404, "RUN_NOT_FOUND", f"Run '{run_id}' not found.")


@app.get("/api/runs/{run_id}/events")
async def stream_run_events(run_id: str) -> StreamingResponse:
    run_dir = _runs_dir() / run_id
    state = _runs.get(run_id)
    if state is None and not run_dir.is_dir():
        raise ApiError(404, "RUN_NOT_FOUND", f"Run '{run_id}' not found.")

    queue: asyncio.Queue[Event | None] | None = None
    if state is not None and not state.finished:
        queue = asyncio.Queue()
        state.subscribers.add(queue)

    async def event_stream() -> AsyncIterator[str]:
        sent_seqs: set[int] = set()
        try:
            past_events = _read_events_file(run_dir / "events.jsonl")
            if state is not None and len(state.events) > len(past_events):
                by_seq = {ev.seq: ev for ev in past_events}
                for ev in state.events:
                    by_seq.setdefault(ev.seq, ev)
                past_events = [by_seq[seq] for seq in sorted(by_seq)]

            for ev in past_events:
                if ev.seq in sent_seqs:
                    continue
                sent_seqs.add(ev.seq)
                yield _format_sse(ev)
                if ev.type in _TERMINAL_STATUS_EVENTS:
                    return

            if queue is None:
                return

            while True:
                if state is not None and state.finished and queue.empty():
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=SSE_PING_INTERVAL_S)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    return
                if item.seq in sent_seqs:
                    continue
                sent_seqs.add(item.seq)
                yield _format_sse(item)
                if item.type in _TERMINAL_STATUS_EVENTS:
                    return
        finally:
            if state is not None and queue is not None:
                state.subscribers.discard(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs/{run_id}/files/{path:path}")
async def get_run_file(run_id: str, path: str) -> FileResponse:
    _validate_relative_path(run_id)
    _validate_relative_path(path)

    run_dir = _runs_dir() / run_id
    if not run_dir.is_dir():
        raise ApiError(404, "FILE_NOT_FOUND", f"File '{path}' not found.")

    base = run_dir.resolve()
    target = (run_dir / path).resolve()
    if not target.is_relative_to(base) or target == base:
        raise ApiError(400, "BAD_PATH", "Path traversal is not allowed.")
    if not target.is_file():
        raise ApiError(404, "FILE_NOT_FOUND", f"File '{path}' not found.")

    return FileResponse(target)


@app.post("/api/runs/{run_id}/replay", status_code=202)
async def replay_run(run_id: str) -> JSONResponse:
    src_dir = _runs_dir() / run_id
    src_events_path = src_dir / "events.jsonl"
    if not src_dir.is_dir() or not src_events_path.is_file():
        raise ApiError(404, "RUN_NOT_FOUND", f"Run '{run_id}' not found.")

    source_events = _read_events_file(src_events_path)
    try:
        _, new_run_id = _load_pipeline()
        new_id = new_run_id()
    except ApiError:
        new_id = _fallback_run_id()

    new_dir = _runs_dir() / new_id
    _copy_bundle_for_replay(src_dir, new_dir)

    state = _RunState(run_id=new_id, run_dir=new_dir)
    _runs[new_id] = state
    state.task = asyncio.create_task(_run_replay_task(source_events, src_dir, new_id, state))
    return JSONResponse(status_code=202, content={"run_id": new_id})


@app.get("/api/results")
async def get_results() -> JSONResponse:
    summary = _summary_path()
    if not summary.is_file():
        return JSONResponse(status_code=200, content={"available": False})
    payload = json.loads(summary.read_text(encoding="utf-8"))
    return JSONResponse(status_code=200, content=payload)


@app.get("/")
async def get_index() -> FileResponse:
    index_path = _web_dir() / "index.html"
    if not index_path.is_file():
        raise ApiError(404, "WEB_NOT_READY", "Web UI is not available.")
    return FileResponse(index_path, media_type="text/html")
