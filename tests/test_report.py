"""Tests for eval.report: metric math on hand-made result records. No network."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval import report


def rec(
    mutant: str,
    system: str = "glint",
    oracle: str = "NO_PATCH",
    *,
    claimed: bool | None = None,
    kind: str = "bug",
    rung: str = "r0",
    repro_valid: bool | None = None,
    repair: bool = False,
    latency: float = 10.0,
    cost: float = 0.1,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "mutant_id": mutant,
        "kind": kind,
        "rung": rung,
        "system": system,
        "status": "VERIFIED_FIX" if oracle != "NO_PATCH" else "NO_VERIFIED_FIX",
        "claimed_fix": (oracle != "NO_PATCH") if claimed is None else claimed,
        "oracle": oracle,
        "repro_valid": repro_valid,
        "fixed_in_repair": repair,
        "latency_s": latency,
        "cost_usd": cost,
        **extra,
    }


# 10 bug runs: 5 TRUE_FIX, 1 WRONG_FIX, 4 without a claim. 2 clean runs, one with an invented fix.
GLINT = [
    *[rec(f"m{i}", oracle="TRUE_FIX", repro_valid=True, latency=float(i)) for i in range(1, 5)],
    rec("m5", oracle="TRUE_FIX", repro_valid=True, repair=True, latency=5.0),
    rec("m6", oracle="WRONG_FIX", repro_valid=False, latency=6.0),
    *[rec(f"m{i}", repro_valid=None, latency=float(i), cost=0.3) for i in range(7, 11)],
    rec("c01", kind="clean", oracle="NO_PATCH", latency=500.0, cost=9.0),
    rec("c02", kind="clean", oracle="HARMLESS_PATCH", claimed=True, latency=500.0, cost=9.0),
]


def test_rate_handles_zero_denominator() -> None:
    assert report.rate(1, 4) == 0.25
    assert report.rate(0, 0) is None
    assert report.rate(3, 0) is None


def test_system_metrics_definitions() -> None:
    m = report.system_metrics(GLINT)
    assert (m["n_bug_runs"], m["n_claimed"], m["n_true_fix"], m["n_wrong_fix"]) == (10, 6, 5, 1)
    assert m["true_fix_rate"] == pytest.approx(5 / 10)
    assert m["precision_when_claiming"] == pytest.approx(5 / 6)
    assert m["wrong_fix_rate"] == pytest.approx(1 / 10)
    assert m["abstain_rate"] == pytest.approx(4 / 10)
    assert (m["n_clean_runs"], m["clean_video_false_fixes"], m["clean_video_behavior_changes"]) == (2, 1, 0)
    # latency and cost are over bug runs only: latencies 1..10, costs six at 0.1 and four at 0.3
    assert m["median_latency_s"] == pytest.approx(5.5)
    assert m["mean_cost_usd"] == pytest.approx((6 * 0.1 + 4 * 0.3) / 10)


def test_system_metrics_no_runs_and_no_claims() -> None:
    empty = report.system_metrics([])
    assert empty["true_fix_rate"] is None
    assert empty["precision_when_claiming"] is None
    assert empty["abstain_rate"] is None
    assert empty["median_latency_s"] is None and empty["mean_cost_usd"] is None

    silent = report.system_metrics([rec("m1"), rec("m2")])
    assert silent["true_fix_rate"] == 0.0
    assert silent["precision_when_claiming"] is None
    assert silent["wrong_fix_rate"] == 0.0
    assert silent["abstain_rate"] == 1.0


def test_claimed_fix_is_the_precision_denominator_not_status() -> None:
    # a rejected patch is a claimed fix that the oracle calls WRONG_FIX
    m = report.system_metrics([rec("m1", "baseline", "TRUE_FIX"), rec("m2", "baseline", "WRONG_FIX")])
    assert m["precision_when_claiming"] == 0.5
    assert m["abstain_rate"] == 0.0


def test_by_rung() -> None:
    records = [
        rec("m1", oracle="TRUE_FIX", repro_valid=True),
        rec("m2", repro_valid=True),
        rec("m3", repro_valid=False),
        rec("m4", repro_valid=None),
        rec("m1", rung="r3", repro_valid=True, oracle="TRUE_FIX"),
        rec("m2", rung="r3", repro_valid=None),
        rec("c01", kind="clean", rung="r0"),
    ]
    rows = report.by_rung(records)
    assert [r["rung"] for r in rows] == ["R0", "R3"]
    assert rows[0]["n"] == 4 and rows[0]["repro_valid_rate"] == 0.5 and rows[0]["true_fix_rate"] == 0.25
    assert rows[1]["n"] == 2 and rows[1]["repro_valid_rate"] == 0.5 and rows[1]["true_fix_rate"] == 0.5
    assert report.by_rung([]) == []


def test_repair_share_and_without_repair() -> None:
    assert report.repair_share_of_fixes(GLINT) == pytest.approx(1 / 5)
    assert report.repair_share_of_fixes([rec("m1")]) is None

    derived = report.system_metrics(report.without_repair(GLINT))
    assert derived["n_true_fix"] == 4 and derived["n_claimed"] == 5
    assert derived["true_fix_rate"] == pytest.approx(4 / 10)
    assert derived["precision_when_claiming"] == pytest.approx(4 / 5)
    assert derived["abstain_rate"] == pytest.approx(5 / 10)
    assert report.system_metrics(GLINT)["n_true_fix"] == 5  # the input records were not modified

    wrong_repair = [rec("m1", oracle="WRONG_FIX", repair=True)]
    assert report.system_metrics(report.without_repair(wrong_repair))["wrong_fix_rate"] == 0.0


def test_failures_and_categories() -> None:
    records = [
        rec("m1", oracle="TRUE_FIX"),
        rec("m2", oracle="WRONG_FIX", stage_reached="done", failed_gate=None),
        rec("m3", status="ABSTAIN", stage_reached="g0", failed_gate="G0"),
        rec("m4", stage_reached="repro", failed_gate="G1"),
        rec("m5", stage_reached="done", failed_gate="G2"),
        rec("m6", "baseline", "WRONG_FIX", oracle_detail="patch rejected: too many files"),
        rec("m7", "baseline"),
        rec("c01", kind="clean", oracle="WRONG_FIX"),
    ]
    rows = {f["mutant"]: f for f in report.failures(records)}
    assert set(rows) == {"m2", "m3", "m4", "m5", "m6", "m7"}
    assert rows["m2"]["category"] == "wrong fix claimed"
    assert rows["m3"]["category"] == "abstained before a repro" and rows["m3"]["where"] == "G0"
    assert rows["m4"]["category"] == "no repro established"
    assert rows["m5"]["category"] == "no candidate passed the gates" and rows["m5"]["where"] == "G2"
    assert rows["m6"]["category"] == "claimed fix was not an applicable patch"
    assert rows["m7"]["category"] == "no patch proposed" and rows["m7"]["system"] == "Single Neon call"


BASELINE = [
    rec("m1", "baseline", "TRUE_FIX"),
    *[rec(f"m{i}", "baseline", "WRONG_FIX") for i in range(2, 6)],
    *[rec(f"m{i}", "baseline") for i in range(6, 11)],
    rec("c01", "baseline", "WRONG_FIX", kind="clean"),
    rec("c02", "baseline", "HARMLESS_PATCH", kind="clean", claimed=True),
]


def test_score_predictions_hit_miss_and_not_evaluable() -> None:
    text = """
    - Glint precision when claiming fixed will be at least 80%.
    - Glint true-fix rate will beat the single call by at least 50 points.
    - The single call's wrong-fix rate will be at least 40%.
    - The single call will invent a fix on at least 2 of 5 clean videos. Glint on at most 0.
    - The repair round will produce fewer than 25% of Glint's fixes.
    - Repro validity will drop by at least 10 points from R0 to R3.
    """
    glint, baseline = report.system_metrics(GLINT), report.system_metrics(BASELINE)
    extras = {"repair_share": report.repair_share_of_fixes(GLINT), "by_rung": report.by_rung(GLINT)}
    verdicts = [p["verdict"] for p in report.score_predictions(text, glint, baseline, extras)]
    # precision 83.3% >= 80 HIT; gap 50-10 = 40 points < 50 MISS; baseline wrong 40% HIT; baseline clean 2 HIT;
    # glint clean 1 > 0 MISS; repair 20% < 25 HIT; no R3 runs so the drop cannot be evaluated
    assert verdicts == ["HIT", "MISS", "HIT", "HIT", "MISS", "HIT", "NOT EVALUABLE"]
    assert report.score_predictions("free text without the plan's wording", glint, baseline, extras) == []


def test_build_summary_matches_contract_shape() -> None:
    summary = report.build_summary([*GLINT, *BASELINE], "abc1234")
    assert list(summary) == ["available", "frozen_hash", "n_test_mutants", "rows", "by_rung", "repair_share_of_fixes"]
    assert summary["available"] is True and summary["frozen_hash"] == "abc1234" and summary["n_test_mutants"] == 10
    assert [row["system"] for row in summary["rows"]] == ["Glint", "Single Neon call"]
    assert list(summary["rows"][0]) == [
        "system", "true_fix_rate", "precision_when_claiming", "wrong_fix_rate", "abstain_rate",
        "clean_video_false_fixes", "median_latency_s", "mean_cost_usd",
    ]  # fmt: skip
    assert summary["rows"][0]["precision_when_claiming"] == 0.8333
    assert summary["rows"][1]["clean_video_false_fixes"] == 2
    assert summary["by_rung"] == [{"rung": "R0", "repro_valid_rate": 0.5, "true_fix_rate": 0.5}]
    assert summary["repair_share_of_fixes"] == 0.2

    empty = report.build_summary([], "not frozen")
    assert empty["available"] is False and empty["n_test_mutants"] == 0 and empty["by_rung"] == []
    assert all(row["precision_when_claiming"] == 0.0 for row in empty["rows"])
    json.dumps(empty)


def test_render_with_no_claims_and_with_both_tags() -> None:
    silent = [rec("m1"), rec("m1", "baseline")]
    text = report.render(silent, "dev", None, {}, "not frozen", None, {})
    assert "n/a (0/0)" in text and "PREDICTIONS.md` does not exist" in text and "v1 vs v2" not in text
    assert "**dev** split numbers" in text

    both = report.render([*GLINT, *BASELINE], "test", "v2", {"limit": 2}, "abc1234", "nothing", {"v1": GLINT, "v2": GLINT})
    assert "## v1 vs v2" in both and "Glint (v1)" in both and "abc1234" in both
    assert "limited to the first 2 mutants" in both and "50.0% (5/10)" in both


def test_load_records_and_choose_set(tmp_path: Path) -> None:
    assert report.choose_set(tmp_path, None, None) is None
    for name in ("dev", "test_v1", "test_v2"):
        folder = tmp_path / name / "glint"
        folder.mkdir(parents=True)
        (folder / "m1_r0.json").write_text(json.dumps(rec("m1")), encoding="utf-8")
    (tmp_path / "dev" / "run_config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "dev" / "glint" / "broken.json").write_text("{not json", encoding="utf-8")
    assert len(report.load_records(tmp_path / "dev")) == 1
    assert report.choose_set(tmp_path, None, None) == ("test", "v2")
    assert report.choose_set(tmp_path, None, "v1") == ("test", "v1")
    assert report.choose_set(tmp_path, "dev", None) == ("dev", None)


def test_frozen_hash(tmp_path: Path) -> None:
    assert report.frozen_hash(tmp_path / "missing.md") == "not frozen"
    frozen = tmp_path / "FROZEN.md"
    frozen.write_text("Frozen at 14:35, commit 9ae7e1d, 28 mutants\n", encoding="utf-8")
    assert report.frozen_hash(frozen) == "9ae7e1d"
