"""Isolated app workspaces, patch validation/application, local HTTP serving, and diffs."""
from __future__ import annotations

import asyncio
import difflib
import functools
import logging
import shutil
import tempfile
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from glint import config
from glint.schemas import Patch

logger = logging.getLogger(__name__)



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


class _QuietHandler(SimpleHTTPRequestHandler):
    """Static file handler that logs nothing and tells the browser not to cache."""

    def log_message(self, format: str, *args: object) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class _WorkspaceServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128  # the default backlog of 5 resets connections when several browsers load at once

    def handle_error(self, request: object, client_address: object) -> None:
        logger.debug("workspace server: connection from %s dropped", client_address)


@asynccontextmanager
async def serve(ws: Path) -> AsyncIterator[str]:
    """Serve ws over HTTP from a thread in this process, yielding its base URL. Stops on exit.

    Binds 127.0.0.1 port 0, so the OS picks a free port atomically and parallel calls never collide.
    """
    handler = functools.partial(_QuietHandler, directory=str(ws))
    server = _WorkspaceServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, name="glint-workspace-server", daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()


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
