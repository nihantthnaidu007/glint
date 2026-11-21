"""Gemini model wrappers: Neon, Hydrogen, Decision 1, and video upload.

Every call goes through one semaphore, retries on 429/5xx/timeouts, and records token usage per run.
Keys come from GEMINI_API_KEY (default) and GEMINI_API_KEY_2 (used after the default key stays rate limited).
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import mimetypes
import os
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from glint import config
from glint.schemas import Usage

log = logging.getLogger("glint.gemini")

KEY_ENV_NAMES = ("GEMINI_API_KEY", "GEMINI_API_KEY_2")
UPLOAD_TIMEOUT_S = 120
UPLOAD_POLL_S = 1.0
BACKOFF_CAP_S = 8.0


class VideoRef(BaseModel):
    name: str
    uri: str
    mime_type: str


def load_env(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env into os.environ without overriding variables already set."""
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if name and value:
            os.environ.setdefault(name, value)


load_env()
logging.getLogger("glint").setLevel(os.getenv("GLINT_LOG_LEVEL", "INFO").upper())

# --- clients and key fallback -------------------------------------------------------------------

_clients: dict[int, genai.Client] = {}
_active_key = 0                       # index into KEY_ENV_NAMES; moves to 1 after a persistent 429
_local_videos: dict[str, Path] = {}   # uri -> local file, so a video can be re-uploaded under key 2
_fallback_refs: dict[str, VideoRef] = {}


def _client(index: int) -> genai.Client:
    if index not in _clients:
        key = os.getenv(KEY_ENV_NAMES[index], "")
        if not key:
            raise RuntimeError(f"{KEY_ENV_NAMES[index]} is not set")
        _clients[index] = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    return _clients[index]


def _has_fallback_key() -> bool:
    return bool(os.getenv(KEY_ENV_NAMES[1], ""))


# --- concurrency, retries -----------------------------------------------------------------------

_sem_state: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _semaphore() -> asyncio.Semaphore:
    global _sem_state
    loop = asyncio.get_running_loop()
    if _sem_state is None or _sem_state[0] is not loop:
        _sem_state = (loop, asyncio.Semaphore(config.MAX_CONCURRENCY))
    return _sem_state[1]


def _status_code(exc: BaseException) -> int | None:
    for attr in ("code", "status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException, httpx.TransportError)):
        return True
    code = _status_code(exc)
    return code == 429 or (code is not None and 500 <= code <= 599)


async def _with_retries(label: str, call: Callable[[genai.Client], Awaitable[Any]]) -> Any:
    """Run call(client) under the semaphore with backoff. Switches to the second key on a persistent 429."""
    global _active_key
    while True:
        key_index = _active_key
        last: BaseException | None = None
        for attempt in range(config.MODEL_RETRIES + 1):
            try:
                async with _semaphore():
                    return await call(_client(key_index))
            except Exception as exc:  # classified below, re-raised if not retryable
                if not _is_retryable(exc):
                    raise
                last = exc
                if attempt < config.MODEL_RETRIES:
                    delay = min(2.0**attempt, BACKOFF_CAP_S) + random.uniform(0, 0.5)
                    log.warning("%s: %s, retry %d in %.1fs", label, type(exc).__name__, attempt + 1, delay)
                    await asyncio.sleep(delay)
        assert last is not None
        if _status_code(last) == 429 and key_index == 0 and _has_fallback_key():
            if _active_key == 0:
                log.warning("%s: still rate limited after retries, switching to the second API key", label)
                _active_key = 1
            continue
        raise last


# --- usage accounting ---------------------------------------------------------------------------

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("glint_run_id", default=None)
_usage: dict[str, Usage] = {}


def bind_run(run_id: str) -> None:
    """Attribute every model call made from this context to run_id."""
    _run_id.set(run_id)
    _usage.setdefault(run_id, Usage())


def usage_snapshot(run_id: str) -> Usage:
    """Tokens and cost accumulated for a run so far."""
    return _usage.get(run_id, Usage()).model_copy()


def _record(model: str, input_tokens: int, output_tokens: int, latency_s: float) -> None:
    price_in, price_out = config.PRICE.get(model, (0.0, 0.0))
    cost = (input_tokens * price_in + output_tokens * price_out) / 1_000_000
    run_id = _run_id.get()
    if run_id is None:
        return
    total = _usage.setdefault(run_id, Usage())
    total.calls += 1
    total.input_tokens += input_tokens
    total.output_tokens += output_tokens
    total.cost_usd += cost
    line = {
        "ts": datetime.now(UTC).isoformat(),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 6),
        "latency_s": round(latency_s, 3),
    }
    try:
        run_dir = Path(config.RUNS_DIR) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "usage.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError as exc:
        log.warning("could not write usage.jsonl: %s", type(exc).__name__)


# --- video upload -------------------------------------------------------------------------------


async def _upload(client: genai.Client, path: Path) -> VideoRef:
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    uploaded = await client.aio.files.upload(file=str(path), config=types.UploadFileConfig(mime_type=mime))
    deadline = time.monotonic() + UPLOAD_TIMEOUT_S
    while True:
        state = getattr(uploaded.state, "name", str(uploaded.state))
        if state == "ACTIVE":
            return VideoRef(name=uploaded.name or "", uri=uploaded.uri or "", mime_type=uploaded.mime_type or mime)
        if state == "FAILED":
            raise RuntimeError(f"video processing failed for {path.name}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"video {path.name} not ACTIVE after {UPLOAD_TIMEOUT_S} s")
        await asyncio.sleep(UPLOAD_POLL_S)
        uploaded = await client.aio.files.get(name=uploaded.name or "")


async def upload_video(path: Path) -> VideoRef:
    """Upload a video through the Files API and wait until it is ACTIVE (120 s timeout)."""
    ref = await _with_retries("upload_video", lambda client: _upload(client, path))
    _local_videos[ref.uri] = path
    return ref


async def _resolve_uri(uri: str, mime_type: str) -> tuple[str, str]:
    """Map a Files URI uploaded under key 1 to its copy under key 2 once the fallback key is active."""
    if _active_key == 0 or uri not in _local_videos:
        return uri, mime_type
    if uri not in _fallback_refs:
        _fallback_refs[uri] = await _upload(_client(1), _local_videos[uri])
    alt = _fallback_refs[uri]
    return alt.uri, alt.mime_type


# --- generation (Neon, Hydrogen) ----------------------------------------------------------------


async def _to_sdk_parts(parts: list) -> list[Any]:
    out: list[Any] = []
    for part in parts:
        if isinstance(part, str):
            out.append(types.Part.from_text(text=part))
        elif isinstance(part, VideoRef):
            uri, mime = await _resolve_uri(part.uri, part.mime_type)
            out.append(types.Part.from_uri(file_uri=uri, mime_type=mime))
        elif isinstance(part, Path):
            out.append(types.Part.from_bytes(data=part.read_bytes(), mime_type="image/png"))
        else:
            raise TypeError(f"unsupported part type: {type(part).__name__}")
    return out


async def _generate[T: BaseModel](
    model: str, parts: list, schema: type[T] | None, thinking: str, temperature: float | None
) -> T | str:
    gen_config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking.upper())),
        temperature=temperature,
    )
    if schema is not None:
        gen_config.response_mime_type = "application/json"
        gen_config.response_schema = schema

    prompt_parts = list(parts)
    for schema_attempt in range(config.SCHEMA_RETRIES + 1):

        async def call(client: genai.Client, current: list = prompt_parts) -> Any:
            contents = await _to_sdk_parts(current)
            return await client.aio.models.generate_content(model=model, contents=contents, config=gen_config)

        started = time.monotonic()
        response = await _with_retries(model, call)
        meta = response.usage_metadata
        input_tokens = (meta.prompt_token_count or 0) if meta else 0
        output_tokens = ((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)) if meta else 0
        _record(model, input_tokens, output_tokens, time.monotonic() - started)

        text = response.text or ""
        if schema is None:
            return text
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            if schema_attempt >= config.SCHEMA_RETRIES:
                raise
            log.warning("%s: output failed %s validation, retrying", model, schema.__name__)
            prompt_parts = [
                *parts,
                (
                    f"Your previous answer did not match the required JSON schema. Validation error:\n{exc}\n"
                    "Answer again with JSON that matches the schema exactly."
                ),
            ]
    raise RuntimeError("unreachable")


async def neon[T: BaseModel](
    parts: list, schema: type[T] | None = None, thinking: str = config.NEON_THINKING
) -> T | str:
    """Gemini 4 Neon. parts: str, VideoRef, or Path to a PNG. Returns the schema instance or raw text."""
    return await _generate(config.NEON, parts, schema, thinking, None)


async def hydrogen[T: BaseModel](
    parts: list,
    schema: type[T] | None = None,
    thinking: str = config.HYDROGEN_THINKING,
    temperature: float | None = None,
) -> T | str:
    """Gemini 4 Hydrogen. parts: str, VideoRef, or Path to a PNG. Returns the schema instance or raw text."""
    return await _generate(config.HYDROGEN, parts, schema, thinking, temperature)


# --- decisions (Decision 1) ---------------------------------------------------------------------


def image_block(png_path: Path) -> dict:
    """Decision API image block with inline base64 PNG data."""
    data = base64.b64encode(png_path.read_bytes()).decode("ascii")
    return {"type": "image", "data": data, "mime_type": "image/png"}


def video_block(ref: VideoRef, resolution: str = "low") -> dict:
    """Decision API video block pointing at a Files API URI. Low resolution keeps long videos in budget."""
    return {"type": "video", "uri": ref.uri, "mime_type": ref.mime_type, "resolution": resolution}


async def decide(input_blocks: list[dict], questions: dict[str, str]) -> dict[str, float]:
    """Ask yes/no questions about shared input. Returns question key -> calibrated p_yes."""
    payload = {
        key: {"type": config.DECISION_BOOL_TYPE, "instructions": text} for key, text in questions.items()
    }

    async def call(client: genai.Client) -> Any:
        blocks = []
        for block in input_blocks:
            if block.get("type") == "video" and "uri" in block:
                uri, mime = await _resolve_uri(block["uri"], block.get("mime_type", "video/mp4"))
                block = {**block, "uri": uri, "mime_type": mime}
            blocks.append(block)
        return await client.aio.decisions.create(model=config.DECISION, input=blocks, questions=payload)

    started = time.monotonic()
    result = await _with_retries(config.DECISION, call)
    usage = result.usage
    input_tokens = (usage.total_input_tokens or 0) if usage else 0
    output_tokens = (usage.total_output_tokens or 0) if usage else 0
    _record(config.DECISION, input_tokens, output_tokens, time.monotonic() - started)

    answers = result.answers or {}
    out: dict[str, float] = {}
    for key in questions:
        if key not in answers:
            raise RuntimeError(f"Decisions API returned no answer for question '{key}'")
        out[key] = float(getattr(answers[key], config.DECISION_BOOL_TYPE))
    return out
