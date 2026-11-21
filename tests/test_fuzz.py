"""Tests for eval.fuzz."""
from __future__ import annotations

import shutil
from pathlib import Path

from eval.fuzz import differential, run_sequence

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
