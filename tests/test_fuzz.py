"""Tests for eval.fuzz."""
from __future__ import annotations

import shutil
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from eval.fuzz import _serve_dir, differential, run_sequence

TODOMVC = Path("apps/todomvc")


async def test_fuzz_determinism() -> None:
    s1 = await run_sequence(TODOMVC, seed=11, length=5)
    s2 = await run_sequence(TODOMVC, seed=11, length=5)
    assert s1 == s2
    assert len(s1) == 6


async def test_differential_identical_and_mutant(tmp_path: Path) -> None:
    assert await differential(TODOMVC, TODOMVC, n_seq=2, seed=0, length=5) is None

    mutant = tmp_path / "todomvc_mutant"
    shutil.copytree(TODOMVC, mutant)
    model_js = mutant / "model.js"
    text = model_js.read_text(encoding="utf-8")
    assert "completed: false" in text
    model_js.write_text(text.replace("completed: false", "completed: true", 1), encoding="utf-8")

    diff = await differential(TODOMVC, mutant, n_seq=5, seed=7, length=6)
    assert diff is not None
    assert "sequence" in diff
    assert diff["original"] != diff["candidate"]


def test_serve_dir_in_process_handles_burst_and_shuts_down(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<p>hello</p>", encoding="utf-8")
    threads_before = threading.active_count()

    def fetch(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            assert resp.status == 200
            return resp.read()

    with _serve_dir(tmp_path) as url:
        assert url.startswith("http://127.0.0.1:")
        # 64 simultaneous loads: far beyond the backlog of 5 that the old subprocess server had.
        with ThreadPoolExecutor(max_workers=64) as pool:
            bodies = list(pool.map(fetch, [f"{url}/index.html"] * 64))
        assert bodies == [b"<p>hello</p>"] * 64

    with pytest.raises((urllib.error.URLError, OSError)):
        fetch(f"{url}/index.html")
    assert threading.active_count() <= threads_before
