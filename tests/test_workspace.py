"""Tests for glint.workspace."""
from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from glint.schemas import Patch
from glint.workspace import PatchRejected, apply_patch, make_workspace, serve, unified_diff


def _make_sample_app(root: Path) -> Path:
    app = root / "app"
    (app / "js").mkdir(parents=True)
    (app / "index.html").write_text("<html><body><h1>App</h1></body></html>\n", encoding="utf-8")
    (app / "js" / "app.js").write_text(
        "function add(a, b) {\n  return a - b;\n}\n", encoding="utf-8"
    )
    (app / "js" / "other.js").write_text("const x = 1;\n", encoding="utf-8")
    (app / "js" / "third.js").write_text("const y = 2;\n", encoding="utf-8")
    return app


def test_make_workspace_and_unified_diff(tmp_path: Path) -> None:
    app = _make_sample_app(tmp_path)
    ws = make_workspace(app)
    try:
        assert ws != app
        assert (ws / "index.html").is_file()
        assert unified_diff(app, ws) == ""

        patch = Patch(files={"js/app.js": "function add(a, b) {\n  return a + b;\n}\n"})
        files_changed, lines_changed = apply_patch(ws, patch)
        assert files_changed == ["js/app.js"]
        assert lines_changed == 2

        diff = unified_diff(app, ws)
        assert "--- a/js/app.js" in diff
        assert "+++ b/js/app.js" in diff
        assert "-  return a - b;" in diff
        assert "+  return a + b;" in diff
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_apply_patch_rejections(tmp_path: Path) -> None:
    app = _make_sample_app(tmp_path)
    ws = make_workspace(app)
    try:
        # ../ path traversal
        with pytest.raises(PatchRejected):
            apply_patch(ws, Patch(files={"../escape.js": "x"}))

        # Absolute path
        with pytest.raises(PatchRejected):
            apply_patch(ws, Patch(files={"/etc/passwd": "x"}))

        # New file that does not exist
        with pytest.raises(PatchRejected):
            apply_patch(ws, Patch(files={"js/brand_new.js": "x"}))

        # Test path
        (ws / "js" / "test_helper.js").write_text("// test\n", encoding="utf-8")
        with pytest.raises(PatchRejected):
            apply_patch(ws, Patch(files={"js/test_helper.js": "// modified\n"}))

        # Disallowed extension
        (ws / "style.css").write_text("body {}\n", encoding="utf-8")
        with pytest.raises(PatchRejected):
            apply_patch(ws, Patch(files={"style.css": "body { color: red; }\n"}))

        # More than 2 files (3 files)
        with pytest.raises(PatchRejected):
            apply_patch(
                ws,
                Patch(
                    files={
                        "js/app.js": "a\n",
                        "js/other.js": "b\n",
                        "js/third.js": "c\n",
                    }
                ),
            )

        # More than 40 changed lines (41 changed lines)
        big_content = "".join(f"const line{i} = {i};\n" for i in range(41))
        with pytest.raises(PatchRejected):
            apply_patch(ws, Patch(files={"js/other.js": big_content}))

        # Verify no partial writes occurred on rejected patches
        assert (ws / "js" / "other.js").read_text(encoding="utf-8") == "const x = 1;\n"
    finally:
        shutil.rmtree(ws, ignore_errors=True)


async def test_serve_returns_200(tmp_path: Path) -> None:
    app = _make_sample_app(tmp_path)
    ws = make_workspace(app)
    try:
        async with serve(ws) as url:
            assert url.startswith("http://127.0.0.1:")
            async with httpx.AsyncClient() as client:
                resp = await client.get(url)
                assert resp.status_code == 200
                assert "<h1>App</h1>" in resp.text
    finally:
        shutil.rmtree(ws, ignore_errors=True)
