"""Isolated app workspaces, patch validation/application, local HTTP serving, and diffs."""
from __future__ import annotations

import asyncio
import difflib
import logging
import shutil
import socket
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from glint import config
from glint.schemas import Patch

logger = logging.getLogger(__name__)

_PORT_START = 8200
_PORT_END = 8999


class PatchRejected(Exception):
    """Raised when a patch violates safety, scope, or size rules."""


def make_workspace(app_dir: Path) -> Path:
    """Copy app_dir to a fresh temporary directory."""
    ws = Path(tempfile.mkdtemp(prefix="glint_ws_"))
    shutil.copytree(app_dir, ws, dirs_exist_ok=True)
    return ws


def _count_changed_lines(old_text: str, new_text: str) -> int:
    """Count added + removed lines between old_text and new_text using difflib."""
    diff = difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm="")
    changed = 0
    for line in diff:
        if (line.startswith("+") and not line.startswith("+++")) or (
            line.startswith("-") and not line.startswith("---")
        ):
            changed += 1
    return changed


def apply_patch(ws: Path, patch: Patch) -> tuple[list[str], int]:
    """Validate and apply a Patch to ws. Returns (files_changed, lines_changed) or raises PatchRejected."""
    if not patch.files:
        raise PatchRejected("Patch contains no files.")
    if len(patch.files) > config.PATCH_MAX_FILES:
        raise PatchRejected(
            f"Patch modifies {len(patch.files)} files, max is {config.PATCH_MAX_FILES}."
        )

    ws_resolved = ws.resolve()
    pending_writes: list[tuple[Path, str, str]] = []
    total_changed = 0

    for rel_path, new_content in patch.files.items():
        if not rel_path or rel_path.startswith(("/", "\\")):
            raise PatchRejected(f"Invalid path: {rel_path!r}")
        p = Path(rel_path)
        if p.is_absolute() or ".." in p.parts:
            raise PatchRejected(f"Path traversal or absolute path not allowed: {rel_path!r}")
        if "test" in rel_path.lower():
            raise PatchRejected(f"Patches cannot touch test files: {rel_path!r}")
        if p.suffix.lower() not in config.PATCH_ALLOWED_EXT:
            raise PatchRejected(f"Disallowed file extension for {rel_path!r}")

        target = (ws_resolved / p).resolve()
        if not target.is_relative_to(ws_resolved):
            raise PatchRejected(f"Path escapes workspace: {rel_path!r}")
        if not target.is_file():
            raise PatchRejected(f"Target file does not exist: {rel_path!r}")

        old_content = target.read_text(encoding="utf-8")
        total_changed += _count_changed_lines(old_content, new_content)
        pending_writes.append((target, rel_path, new_content))

    if total_changed > config.PATCH_MAX_CHANGED_LINES:
        raise PatchRejected(
            f"Patch changes {total_changed} lines, max is {config.PATCH_MAX_CHANGED_LINES}."
        )

    files_changed: list[str] = []
    for target, rel_path, new_content in pending_writes:
        target.write_text(new_content, encoding="utf-8")
        files_changed.append(rel_path)

    return files_changed, total_changed


def _find_free_port() -> int:
    """Find an available TCP port in 8200..8999 on 127.0.0.1."""
    for port in range(_PORT_START, _PORT_END + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free port found in {_PORT_START}..{_PORT_END}")


@asynccontextmanager
async def serve(ws: Path) -> AsyncIterator[str]:
    """Serve ws over HTTP on a free port in 8200..8999, yielding its base URL."""
    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "http.server",
        str(port),
        "--bind",
        "127.0.0.1",
        "--directory",
        str(ws),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        async with httpx.AsyncClient() as client:
            while True:
                if proc.returncode is not None:
                    raise RuntimeError(f"http.server exited early with code {proc.returncode}")
                try:
                    resp = await client.get(url, timeout=0.5)
                    if resp.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(f"http.server did not respond 200 within 5s at {url}")
                await asyncio.sleep(0.05)
        yield url
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()


def unified_diff(app_dir: Path, ws: Path) -> str:
    """Return unified diff over all changed files between app_dir and ws with app-relative paths."""
    app_files = {p.relative_to(app_dir).as_posix() for p in app_dir.rglob("*") if p.is_file()}
    ws_files = {p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file()}
    all_paths = sorted(app_files | ws_files)

    chunks: list[str] = []
    for rel in all_paths:
        old_path = app_dir / rel
        new_path = ws / rel
        old_lines = (
            old_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if old_path.is_file()
            else []
        )
        new_lines = (
            new_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if new_path.is_file()
            else []
        )
        if old_lines == new_lines:
            continue
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        chunks.append("".join(diff))
    return "".join(chunks)
