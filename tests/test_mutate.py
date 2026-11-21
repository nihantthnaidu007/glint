"""Tests for eval.mutate."""
from __future__ import annotations

from pathlib import Path

from eval.mutate import collect_mutation_sites, make_one

TODOMVC = Path("apps/todomvc")


def test_collect_mutation_sites_deterministic() -> None:
    s1 = collect_mutation_sites(TODOMVC, seed=7)
    s2 = collect_mutation_sites(TODOMVC, seed=7)
    assert s1 == s2
    assert len(s1) > 20
    assert all(s.file in {"controller.js", "model.js", "view.js", "store.js", "template.js"} for s in s1)


def test_make_one(tmp_path: Path) -> None:
    out_dir = tmp_path / "one_mutant"
    meta = make_one(TODOMVC, seed=7, out_dir=out_dir, max_tries=20)
    assert meta is not None
    assert (out_dir / "app" / "index.html").is_file()
    assert (out_dir / "meta.json").is_file()
    assert meta["file"] in {"controller.js", "model.js", "view.js", "store.js", "template.js"}
    assert isinstance(meta["divergent_sequence"], list)
