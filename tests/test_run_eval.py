"""Tests for eval.run_eval and eval.baselines: discovery, hygiene, record shape, oracle scoring. No network."""
from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eval import baselines, run_eval
from eval.run_eval import Case, OracleCtx
from glint import config
from glint.schemas import Candidate, GateResult, Patch, RunResult

REPO = Path(__file__).resolve().parent.parent
TODOMVC = REPO / "apps" / "todomvc"
SECRET = "ANSWER_KEY_SNIPPET"


def make_corpus(root: Path) -> Path:
    """A tiny corpus: m001 dev (r0), m002 test (r0 + r3), m003 dev (r0 + r3), m004 dev (no video), two clean."""
    app_root = root / "todomvc"
    layout = {"m003": ("dev", ["video_r0.webm", "video_r3.mp4"]), "m001": ("dev", ["video_r0.webm"]),
              "m002": ("test", ["video_r0.webm", "video_r3.mp4"]), "m004": ("dev", [])}  # fmt: skip
    for mutant, (split, videos) in layout.items():
        folder = app_root / mutant
        (folder / "app").mkdir(parents=True)
        (folder / "app" / "app.js").write_text("var x = 1;\n", encoding="utf-8")
        meta = {"mutant_id": mutant, "split": split, "file": "app.js", "original_snippet": SECRET}
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        for video in videos:
            (folder / video).write_bytes(b"video")
    for clean in ("c02", "c01"):
        (app_root / "clean" / clean).mkdir(parents=True)
        (app_root / "clean" / clean / "video_r0.webm").write_bytes(b"video")
    return root


def test_discover_cases_split_rungs_and_order(tmp_path: Path) -> None:
    corpus = make_corpus(tmp_path)
    cases = run_eval.discover_cases(corpus, "dev", ["r0", "r3"], include_clean=False)
    assert [(c.case_id, c.rung) for c in cases] == [("m001", "r0"), ("m003", "r0"), ("m003", "r3")]
    assert all(c.kind == "bug" and c.app_dir == corpus / "todomvc" / c.case_id / "app" for c in cases)
    assert cases[2].video.name == "video_r3.mp4"

    test_cases = run_eval.discover_cases(corpus, "test", ["r0"], include_clean=False)
    assert [(c.case_id, c.rung) for c in test_cases] == [("m002", "r0")]


def test_discover_cases_limit_and_clean(tmp_path: Path) -> None:
    corpus = make_corpus(tmp_path)
    cases = run_eval.discover_cases(corpus, "dev", ["r0"], include_clean=True, limit=1)
    assert [(c.case_id, c.kind) for c in cases] == [("m001", "bug"), ("c01", "clean"), ("c02", "clean")]
    assert all(c.app_dir == run_eval.ORIGINAL_APP for c in cases if c.kind == "clean")


def test_mutant_cases_never_carry_the_answer_key(tmp_path: Path) -> None:
    corpus = make_corpus(tmp_path)
    for case in run_eval.discover_cases(corpus, "dev", ["r0", "r3"], include_clean=False):
        assert case.app_dir.resolve() != run_eval.ORIGINAL_APP.resolve()
        assert not (case.app_dir / "meta.json").exists()
        assert SECRET not in repr(case)
        record = run_eval.base_record(case, "glint", "dev", "r_x")
        assert SECRET not in json.dumps(record)
    assert run_eval.read_split(corpus / "todomvc" / "m002" / "meta.json") == "test"
    assert run_eval.read_split(corpus / "todomvc" / "missing" / "meta.json") is None


def test_only_run_eval_touches_the_oracle_and_glint_never_imports_eval() -> None:
    importing = re.compile(r"^\s*(from|import)\s+(eval|scripts)\b", re.MULTILINE)
    for path in (REPO / "glint").glob("*.py"):
        assert not importing.search(path.read_text(encoding="utf-8")), path
    for name in ("baselines.py", "report.py"):
        source = (REPO / "eval" / name).read_text(encoding="utf-8")
        assert "eval.fuzz" not in source and "import fuzz" not in source, name
    assert "meta" not in (REPO / "eval" / "baselines.py").read_text(encoding="utf-8").lower()


def test_results_paths_and_record_fields(tmp_path: Path) -> None:
    case = Case("m007", "bug", "r3", tmp_path / "video_r3.mp4", tmp_path / "app")
    assert run_eval.results_dir("test", None) == run_eval.RESULTS_ROOT / "test"
    assert run_eval.results_dir("test", "v2") == run_eval.RESULTS_ROOT / "test_v2"
    assert run_eval.record_path(tmp_path, "baseline", case) == tmp_path / "baseline" / "m007_r3.json"
    record = run_eval.base_record(case, "baseline", "test", "r_1")
    required = {"mutant_id", "rung", "system", "status", "claimed_fix", "oracle", "repro_valid", "fixed_in_repair",
                "latency_s", "cost_usd", "tokens", "run_id"}  # fmt: skip
    assert required <= set(record)
    assert "status=" in run_eval.summary_line(record) and "oracle=NO_PATCH" in run_eval.summary_line(record)


def test_cli_parser() -> None:
    parser = run_eval.build_parser()
    args = parser.parse_args(["--split", "dev", "--systems", "glint,baseline", "--rungs", "r0", "--limit", "2"])
    assert (args.systems, args.rungs, args.limit, args.concurrency) == (["glint", "baseline"], ["r0"], 2, 8)
    assert args.corpus == run_eval.DEFAULT_CORPUS and args.force is False and args.tag is None
    for bad in (["--split", "dev", "--rungs", "r1"], ["--split", "dev", "--systems", "gpt"], ["--split", "all"]):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)


def _result(**fields: object) -> RunResult:
    base = {"run_id": "r", "app_id": "todomvc", "status": "NO_VERIFIED_FIX", "reason": "x", "stage_reached": "done",
            "created_at": datetime.now(UTC)}  # fmt: skip
    return RunResult(**{**base, **fields})


def test_glint_failed_gate() -> None:
    g0 = GateResult(gate="G0", passed=False)
    assert run_eval.glint_failed_gate(_result(status="ABSTAIN", gates=[g0])) == "G0"

    def cand(cid: str, failed: str | None) -> Candidate:
        gates = [GateResult(gate="G2", passed=failed != "G2")]
        if failed == "G4":
            gates.append(GateResult(gate="G4", passed=False))
        return Candidate(id=cid, source="initial", gates=gates)

    passed_run_gates = [GateResult(gate="G0", passed=True), GateResult(gate="G1", passed=True)]
    lost = _result(gates=passed_run_gates, candidates=[cand("c1", "G4"), cand("c2", "G4"), cand("c3", "G2")])
    assert run_eval.glint_failed_gate(lost) == "G4"
    won = _result(status="VERIFIED_FIX", gates=passed_run_gates, candidates=[cand("c1", None)], winner_id="c1")
    assert run_eval.glint_failed_gate(won) is None
    assert run_eval.glint_failed_gate(_result(gates=passed_run_gates)) is None


def test_baseline_prompt_and_model_check() -> None:
    prompt = baselines.baseline_prompt()
    assert f"at most {config.PATCH_MAX_FILES} files" in prompt
    assert f"at most {config.PATCH_MAX_CHANGED_LINES} lines" in prompt
    assert "{" not in prompt


async def test_single_call_rejects_unknown_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown baseline model"):
        await baselines.single_call(tmp_path / "v.webm", tmp_path, model="gpt")


def _mutant(tmp_path: Path) -> tuple[Path, str, str]:
    mutant = tmp_path / "mutant"
    shutil.copytree(TODOMVC, mutant)
    original = (TODOMVC / "model.js").read_text(encoding="utf-8")
    assert "completed: false" in original
    mutated = original.replace("completed: false", "completed: true", 1)
    (mutant / "model.js").write_text(mutated, encoding="utf-8")
    return mutant, original, mutated


async def test_score_patch_without_a_browser(tmp_path: Path) -> None:
    mutant, _, _ = _mutant(tmp_path)
    case = Case("m900", "bug", "r0", tmp_path / "video_r0.webm", mutant)
    oracle = OracleCtx(browser=None, n_seq=1)
    assert await run_eval.score_patch(None, case, oracle) == ("NO_PATCH", "")
    label, detail = await run_eval.score_patch(Patch(files={"../outside.js": "x"}), case, oracle)
    assert label == "WRONG_FIX" and detail.startswith("patch rejected")
    label, detail = await run_eval.score_patch(Patch(files={"new_test.js": "x"}), case, oracle)
    assert label == "WRONG_FIX" and detail.startswith("patch rejected")


async def test_score_patch_with_the_real_oracle(tmp_path: Path) -> None:
    mutant, original, mutated = _mutant(tmp_path)
    case = Case("m900", "bug", "r0", tmp_path / "video_r0.webm", mutant)
    oracle = OracleCtx(browser=None, n_seq=5, seed=7)

    assert await run_eval.score_patch(Patch(files={"model.js": original}), case, oracle) == ("TRUE_FIX", "")

    still_broken = mutated.replace("completed: true", "completed: !false", 1)
    label, detail = await run_eval.score_patch(Patch(files={"model.js": still_broken}), case, oracle)
    assert label == "WRONG_FIX" and "behavior differs" in detail
    assert (mutant / "model.js").read_text(encoding="utf-8") == mutated  # the corpus app itself is never edited

    clean = Case("c900", "clean", "r0", tmp_path / "video_r0.webm", TODOMVC)
    harmless = original.replace("completed: false", "completed: !true", 1)
    assert await run_eval.score_patch(Patch(files={"model.js": harmless}), clean, oracle) == ("HARMLESS_PATCH", "")
    label, _ = await run_eval.score_patch(Patch(files={"model.js": mutated}), clean, oracle)
    assert label == "WRONG_FIX"


async def test_repro_is_valid_none_without_a_repro(tmp_path: Path) -> None:
    assert await run_eval.repro_is_valid(tmp_path, TODOMVC) is None
