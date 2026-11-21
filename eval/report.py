"""Eval report: reads result records, writes RESULTS.md and eval/results/summary.json."""
from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
from pathlib import Path
from typing import Any

from glint import config

log = logging.getLogger("eval.report")

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_ROOT = REPO_ROOT / "eval" / "results"
RESULTS_MD = REPO_ROOT / "RESULTS.md"
SUMMARY_JSON = RESULTS_ROOT / "summary.json"
FROZEN_MD = REPO_ROOT / "eval" / "FROZEN.md"
PREDICTIONS_MD = REPO_ROOT / "eval" / "PREDICTIONS.md"

SYSTEM_NAMES = {"glint": "Glint", "baseline": "Single Neon call"}
SPLITS = ("test", "dev")
TAGS = ("v2", "v1", None)  # newest first; None is an untagged run

TRUE_FIX = "TRUE_FIX"
WRONG_FIX = "WRONG_FIX"

Record = dict[str, Any]


# --- metric math (pure, unit-tested) ------------------------------------------------------------


def rate(numerator: int, denominator: int) -> float | None:
    """numerator / denominator, or None when there is nothing to divide by."""
    return numerator / denominator if denominator else None


def bug_runs(records: list[Record]) -> list[Record]:
    return [r for r in records if r.get("kind", "bug") == "bug"]


def clean_runs(records: list[Record]) -> list[Record]:
    return [r for r in records if r.get("kind", "bug") == "clean"]


def system_metrics(records: list[Record]) -> dict[str, Any]:
    """The main-table numbers for one system's records.

    true-fix rate = TRUE_FIX / all bug runs. precision when claiming = TRUE_FIX / runs with claimed_fix.
    wrong-fix rate = WRONG_FIX / all bug runs. abstain rate = runs without a claimed fix / all bug runs.
    A clean-video false fix is any claimed fix on a video that shows no bug.
    """
    bugs = bug_runs(records)
    cleans = clean_runs(records)
    n = len(bugs)
    n_claimed = sum(1 for r in bugs if r.get("claimed_fix"))
    n_true = sum(1 for r in bugs if r.get("oracle") == TRUE_FIX)
    n_wrong = sum(1 for r in bugs if r.get("oracle") == WRONG_FIX)
    latencies = [float(r.get("latency_s", 0.0)) for r in bugs]
    costs = [float(r.get("cost_usd", 0.0)) for r in bugs]
    return {
        "n_bug_runs": n,
        "n_claimed": n_claimed,
        "n_true_fix": n_true,
        "n_wrong_fix": n_wrong,
        "true_fix_rate": rate(n_true, n),
        "precision_when_claiming": rate(n_true, n_claimed),
        "wrong_fix_rate": rate(n_wrong, n),
        "abstain_rate": rate(n - n_claimed, n),
        "n_clean_runs": len(cleans),
        "clean_video_false_fixes": sum(1 for r in cleans if r.get("claimed_fix")),
        "clean_video_behavior_changes": sum(1 for r in cleans if r.get("oracle") == WRONG_FIX),
        "median_latency_s": statistics.median(latencies) if latencies else None,
        "mean_cost_usd": statistics.fmean(costs) if costs else None,
    }


def by_rung(records: list[Record]) -> list[dict[str, Any]]:
    """Per rung, for one system's bug runs: repro valid rate and true-fix rate."""
    bugs = bug_runs(records)
    rows: list[dict[str, Any]] = []
    for rung in sorted({str(r.get("rung", "")) for r in bugs}):
        runs = [r for r in bugs if r.get("rung") == rung]
        rows.append(
            {
                "rung": rung.upper(),
                "n": len(runs),
                "repro_valid_rate": rate(sum(1 for r in runs if r.get("repro_valid") is True), len(runs)),
                "true_fix_rate": rate(sum(1 for r in runs if r.get("oracle") == TRUE_FIX), len(runs)),
            }
        )
    return rows


def repair_share_of_fixes(records: list[Record]) -> float | None:
    """Share of the system's TRUE_FIX bug runs whose winning patch came from the repair round."""
    true_fixes = [r for r in bug_runs(records) if r.get("oracle") == TRUE_FIX]
    return rate(sum(1 for r in true_fixes if r.get("fixed_in_repair")), len(true_fixes))


def without_repair(records: list[Record]) -> list[Record]:
    """The same runs as if the repair round did not exist: every repair-round win becomes no claimed fix."""
    out: list[Record] = []
    for r in records:
        if r.get("fixed_in_repair"):
            r = {**r, "claimed_fix": False, "oracle": "NO_PATCH", "status": "NO_VERIFIED_FIX", "fixed_in_repair": False}
        out.append(r)
    return out


def failure_category(record: Record) -> str:
    """A short label for why a bug run did not end in a true fix."""
    if record.get("oracle") == WRONG_FIX:
        if str(record.get("oracle_detail", "")).startswith("patch rejected"):
            return "claimed fix was not an applicable patch"
        return "wrong fix claimed"
    if record.get("system") != "glint":
        return "no patch proposed"
    if record.get("internal_error"):
        return "internal error"
    status = record.get("status")
    if status == "REJECTED_INPUT":
        return "input rejected"
    if status == "ABSTAIN":
        return "abstained before a repro"
    if record.get("stage_reached") == "repro":
        return "no repro established"
    return "no candidate passed the gates"


def failures(records: list[Record]) -> list[dict[str, str]]:
    """Failure-table rows: every bug run that is not a TRUE_FIX."""
    rows: list[dict[str, str]] = []
    for r in sorted(bug_runs(records), key=lambda r: (r.get("mutant_id", ""), r.get("rung", ""), r.get("system", ""))):
        if r.get("oracle") == TRUE_FIX:
            continue
        where = r.get("failed_gate") or r.get("stage_reached") or "single call"
        rows.append(
            {
                "mutant": str(r.get("mutant_id", "")),
                "rung": str(r.get("rung", "")).upper(),
                "system": SYSTEM_NAMES.get(str(r.get("system")), str(r.get("system"))),
                "where": str(where),
                "category": failure_category(r),
            }
        )
    return rows


# --- predictions --------------------------------------------------------------------------------

NUM = r"(\d+(?:\.\d+)?)"


def _verdict(ok: bool | None) -> str:
    return "NOT EVALUABLE" if ok is None else ("HIT" if ok else "MISS")


def _points(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else (a - b) * 100


def score_predictions(text: str, glint: dict[str, Any], baseline: dict[str, Any], extras: dict[str, Any]) -> list[dict[str, str]]:
    """HIT or MISS for each prediction sentence of eval/PREDICTIONS.md that follows the plan's wording."""
    rows: list[dict[str, str]] = []

    def add(match: re.Match[str], ok: bool | None, actual: str) -> None:
        rows.append({"prediction": " ".join(match.group(0).split()), "verdict": _verdict(ok), "actual": actual})

    def cmp(value: float | None, test: Any) -> bool | None:
        return None if value is None else bool(test(value))

    flags = re.IGNORECASE
    if m := re.search(rf"Glint precision when claiming fixed will be at least {NUM}\s*%", text, flags):
        p = glint["precision_when_claiming"]
        add(m, cmp(p, lambda v: v * 100 >= float(m.group(1))), pct(p))
    if m := re.search(rf"Glint true-fix rate will beat the single call by at least {NUM} points", text, flags):
        gap = _points(glint["true_fix_rate"], baseline["true_fix_rate"])
        add(m, cmp(gap, lambda v: v >= float(m.group(1))), "n/a" if gap is None else f"{gap:+.1f} points")
    if m := re.search(rf"single call's wrong-fix rate will be at least {NUM}\s*%", text, flags):
        w = baseline["wrong_fix_rate"]
        add(m, cmp(w, lambda v: v * 100 >= float(m.group(1))), pct(w))
    if m := re.search(rf"single call will invent a fix on at least {NUM} of {NUM} clean videos", text, flags):
        n = baseline["clean_video_false_fixes"] if baseline["n_clean_runs"] else None
        add(m, cmp(n, lambda v: v >= float(m.group(1))), "n/a" if n is None else f"{n} of {baseline['n_clean_runs']}")
    if m := re.search(rf"Glint on at most {NUM}", text, flags):
        n = glint["clean_video_false_fixes"] if glint["n_clean_runs"] else None
        add(m, cmp(n, lambda v: v <= float(m.group(1))), "n/a" if n is None else f"{n} of {glint['n_clean_runs']}")
    if m := re.search(rf"repair round will produce fewer than {NUM}\s*% of Glint's fixes", text, flags):
        share = extras.get("repair_share")
        add(m, cmp(share, lambda v: v * 100 < float(m.group(1))), pct(share))
    if m := re.search(rf"Repro validity will drop by at least {NUM} points from R0 to R3", text, flags):
        rungs = {row["rung"]: row["repro_valid_rate"] for row in extras.get("by_rung", [])}
        drop = _points(rungs.get("R0"), rungs.get("R3"))
        add(m, cmp(drop, lambda v: v >= float(m.group(1))), "n/a" if drop is None else f"{drop:.1f} points")
    # The six predictions written at the freeze (eval/PREDICTIONS.md, Nov 21 2026).
    rungs_tf = {row["rung"]: row["true_fix_rate"] for row in extras.get("by_rung", [])}
    if m := re.search(rf"Glint precision when it claims a fix: at least {NUM}\s*%", text, flags):
        p = glint["precision_when_claiming"]
        add(m, cmp(p, lambda v: v * 100 >= float(m.group(1))), pct(p))
    if m := re.search(rf"Glint true-fix rate on test bugs \(clean recordings\): at least {NUM}\s*%", text, flags):
        r0 = rungs_tf.get("R0")
        add(m, cmp(r0, lambda v: v * 100 >= float(m.group(1))), f"{pct(r0)} on R0")
    if m := re.search(rf"Single Neon call true-fix rate: at least {NUM}\s*%", text, flags):
        t = baseline["true_fix_rate"]
        add(m, cmp(t, lambda v: v * 100 >= float(m.group(1))), f"{pct(t)} over all bug runs")
    if m := re.search(rf"Single Neon call wrong-fix rate: at least {NUM}\s*%", text, flags):
        w = baseline["wrong_fix_rate"]
        add(m, cmp(w, lambda v: v * 100 >= float(m.group(1))), f"{pct(w)} over all bug runs")
    if m := re.search(
        rf"On clean videos: Glint claims a fix on at most {NUM}, the single call changes code on all of them",
        text,
        flags,
    ):
        n_clean = baseline["n_clean_runs"]
        if n_clean and glint["n_clean_runs"]:
            g, b = glint["clean_video_false_fixes"], baseline["clean_video_false_fixes"]
            add(m, g <= float(m.group(1)) and b == n_clean, f"Glint {g} of {glint['n_clean_runs']}, single call {b} of {n_clean}")
        else:
            add(m, None, "n/a")
    if m := re.search(
        rf"Glint true-fix rate on phone videos is at least {NUM} points lower than on clean recordings", text, flags
    ):
        gap = _points(rungs_tf.get("R0"), rungs_tf.get("R3"))
        actual = "n/a" if gap is None else f"R0 {pct(rungs_tf.get('R0'))}, R3 {pct(rungs_tf.get('R3'))}, gap {gap:.1f} points"
        add(m, cmp(gap, lambda v: v >= float(m.group(1))), actual)
    return rows


# --- loading ------------------------------------------------------------------------------------


def set_name(split: str, tag: str | None) -> str:
    return f"{split}_{tag}" if tag else split


def load_records(set_dir: Path) -> list[Record]:
    """Every result record of one result set (eval/results/<set>/<system>/<mutant>_<rung>.json)."""
    records: list[Record] = []
    for path in sorted(set_dir.glob("*/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("skipping unreadable record %s", path)
            continue
        if isinstance(record, dict) and "system" in record and "oracle" in record:
            records.append(record)
    return records


def choose_set(root: Path, split: str | None, tag: str | None) -> tuple[str, str | None] | None:
    """The result set to report: the given split and tag, else test before dev and the newest tag."""
    splits = (split,) if split else SPLITS
    tags = (tag,) if tag else TAGS
    for s in splits:
        for t in tags:
            if load_records(root / set_name(s, t)):
                return s, t
    return None


def frozen_hash(path: Path = FROZEN_MD) -> str:
    """The corpus freeze commit hash from eval/FROZEN.md, or a plain statement that there is none."""
    if not path.is_file():
        return "not frozen"
    match = re.search(r"\b[0-9a-f]{7,40}\b", path.read_text(encoding="utf-8"))
    return match.group(0) if match else "not frozen"


def records_of(records: list[Record], system: str) -> list[Record]:
    return [r for r in records if r.get("system") == system]


# --- rendering ----------------------------------------------------------------------------------


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def num(value: float | None, fmt: str) -> str:
    return "n/a" if value is None else format(value, fmt)


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def main_row(name: str, m: dict[str, Any]) -> list[str]:
    clean = f"{m['clean_video_false_fixes']} of {m['n_clean_runs']}" if m["n_clean_runs"] else "not run"
    return [
        name,
        str(m["n_bug_runs"]),
        f"{pct(m['true_fix_rate'])} ({m['n_true_fix']}/{m['n_bug_runs']})",
        f"{pct(m['precision_when_claiming'])} ({m['n_true_fix']}/{m['n_claimed']})",
        f"{pct(m['wrong_fix_rate'])} ({m['n_wrong_fix']}/{m['n_bug_runs']})",
        pct(m["abstain_rate"]),
        clean,
        num(m["median_latency_s"], ".1f"),
        num(m["mean_cost_usd"], ".4f"),
    ]


DERIVED_NAME = "Glint v2 (derived from v1 runs)"
DERIVED_NOTE = (
    "\"Derived\" means the saved v1 Glint runs were recounted under the v2 selection rule from their saved "
    "candidates and gate results, with no model called again; newly claimed fixes were scored by the same oracle. "
    "The v2 rule was chosen after seeing these v1 results (see eval/CHANGES_AFTER_V1.md). v1 is the scored run."
)

MAIN_HEADER = [
    "System", "Bug runs", "True-fix rate", "Precision when claiming fixed", "Wrong-fix rate", "Abstain rate",
    "False fixes on clean videos", "Median latency (s)", "Mean cost (USD)",
]  # fmt: skip


def build_summary(records: list[Record], frozen: str, derived_glint: list[Record] | None = None) -> dict[str, Any]:
    """eval/results/summary.json in the shape of CONTRACTS.md section 9. Undefined rates are written as 0.0.

    With derived v2 records the rows are "Glint v1", "Glint v2 (derived from v1 runs)", "Single Neon call".
    """
    sources: list[tuple[str, list[Record]]] = [(name, records_of(records, system)) for system, name in SYSTEM_NAMES.items()]
    if derived_glint:
        sources = [("Glint v1", sources[0][1]), (DERIVED_NAME, derived_glint), sources[1]]
    rows = []
    for name, system_records in sources:
        m = system_metrics(system_records)
        rows.append(
            {
                "system": name,
                "true_fix_rate": round(m["true_fix_rate"] or 0.0, 4),
                "precision_when_claiming": round(m["precision_when_claiming"] or 0.0, 4),
                "wrong_fix_rate": round(m["wrong_fix_rate"] or 0.0, 4),
                "abstain_rate": round(m["abstain_rate"] or 0.0, 4),
                "clean_video_false_fixes": m["clean_video_false_fixes"],
                "median_latency_s": round(m["median_latency_s"] or 0.0, 2),
                "mean_cost_usd": round(m["mean_cost_usd"] or 0.0, 4),
            }
        )
    glint = records_of(records, "glint")
    return {
        "available": bool(records),
        "frozen_hash": frozen,
        "n_test_mutants": len({r.get("mutant_id") for r in bug_runs(records)}),
        "rows": rows,
        "by_rung": [
            {
                "rung": row["rung"],
                "repro_valid_rate": round(row["repro_valid_rate"] or 0.0, 4),
                "true_fix_rate": round(row["true_fix_rate"] or 0.0, 4),
            }
            for row in by_rung(glint)
        ],
        "repair_share_of_fixes": round(repair_share_of_fixes(glint) or 0.0, 4),
    }


def _repo_relative(path: str) -> str:
    """A path inside this repository shown relative to its root, so no local directory name is published."""
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return Path(path).name


def config_lines(run_cfg: dict[str, Any], records: list[Record], split: str, tag: str | None, frozen: str) -> list[str]:
    """The 'frozen corpus hash and config' section. Values come from the run's own run_config.json when present."""
    models = run_cfg.get("models") or {"neon": config.NEON, "hydrogen": config.HYDROGEN, "decision": config.DECISION}
    thresholds = run_cfg.get("thresholds") or {
        "TH_G0_IS_DEFECT": config.TH_G0_IS_DEFECT,
        "TH_G1B_SYMPTOM_MATCH": config.TH_G1B_SYMPTOM_MATCH,
        "TH_G4_DEFECT_GONE": config.TH_G4_DEFECT_GONE,
    }
    oracle = run_cfg.get("oracle") or {}
    bugs = bug_runs(records)
    rungs = ", ".join(sorted({str(r.get("rung", "")).upper() for r in records}))
    lines = [
        f"- Result set: `eval/results/{set_name(split, tag)}/` (split **{split}**" + (f", tag {tag})" if tag else ")"),
        f"- Frozen corpus hash: {frozen}",
        f"- Corpus: `{_repo_relative(str(run_cfg.get('corpus', 'unknown')))}`",
        (f"- N: {len({r.get('mutant_id') for r in bugs})} mutants, {len(bugs)} bug runs, "
        f"{len(clean_runs(records))} clean-video runs, rungs {rungs}"),
        (f"- Models: Neon `{models.get('neon')}`, Hydrogen `{models.get('hydrogen')}`, Decision `{models.get('decision')}`; "
        f"the baseline is one `{run_cfg.get('baseline_model', config.NEON)}` call"),
        "- Thresholds: " + ", ".join(f"{k} = {v}" for k, v in thresholds.items()),
        f"- Glint candidates per run: {run_cfg.get('n_candidates', config.N_CANDIDATES)}",
    ]
    if oracle:
        lines.append(
            f"- Oracle: differential fuzz against the unmutated app, {oracle.get('n_seq')} sequences from seed "
            f"{oracle.get('seed')}. Called only by `eval/run_eval.py`."
        )
    if run_cfg.get("limit"):
        lines.append(f"- This run was limited to the first {run_cfg['limit']} mutants of the split.")
    return lines


def control_lines(controls: dict[str, Any] | None, records: list[Record]) -> list[str]:
    """The oracle's own checks, run before any patch was scored. No model is involved in them."""
    if not controls:
        return ["Oracle controls were not run for this result set, so the oracle verdicts below are unchecked."]
    rows = controls.get("rows", [])
    passed = sum(1 for row in rows if row.get("ok"))
    lines = [
        f"Run at the start of oracle scoring, after every model run had finished: **{passed} of {len(rows)} passed**"
        + ("." if controls.get("ok") else ". A CONTROL FAILED, SO THE ORACLE VERDICTS IN THIS FILE ARE INVALID."),
        "",
        *table(
            ["Control", "Expected", "Got", "Result"],
            [[row["control"], row["expected"], row["got"], "ok" if row["ok"] else "FAILED"] for row in rows],
        ),
        "",
        (
            "A divergence counts only if the same sequence diverges at the same step 3 times out of 3. "
            f"Flaky sequences seen and not counted: {len(controls.get('flaky', []))}."
        ),
    ]
    pending = sum(1 for r in records if r.get("oracle") == "PENDING")
    if pending:
        lines += ["", f"{pending} claimed fix(es) are still waiting for the oracle and are not in the rates below."]
    return lines


def limitation_lines(records: list[Record], split: str) -> list[str]:
    n_mutants = len({r.get("mutant_id") for r in bug_runs(records)})
    lines = [
        (f"- {n_mutants} mutants of one app (TodoMVC). Every bug is a single synthetic mutation, so these numbers say "
        "nothing about other apps or about bugs that span several files."),
        ("- TRUE_FIX means the differential fuzzer found no behavior difference from the unmutated app. That is strong "
        "evidence, not a proof of equivalence."),
        ("- A clean-video false fix counts every claimed fix on a video without a bug, including patches that do not "
        "change behavior."),
        "- Latency is wall time measured while several runs share one machine and one API quota.",
    ]
    if split != "test":
        lines.append(f"- These are **{split}** split numbers. Thresholds are tuned on dev, so they are not the scored result.")
    if n_mutants < 20:
        lines.append(f"- With {n_mutants} mutants each run moves a rate by {100 / max(n_mutants, 1):.0f} points or more.")
    if config.PRICE.get(config.DECISION) == (0.0, 0.0):
        lines.append("- Decision model calls are priced at $0 because the price is unknown, so Glint's cost is a lower bound.")
    if not any(r.get("kind") == "clean" for r in records):
        lines.append("- Clean videos were not run in this result set.")
    return lines


def render(
    records: list[Record],
    split: str,
    tag: str | None,
    run_cfg: dict[str, Any],
    frozen: str,
    predictions_text: str | None,
    other_tags: dict[str, list[Record]],
    controls: dict[str, Any] | None = None,
    derived_glint: list[Record] | None = None,
) -> str:
    """RESULTS.md as one string."""
    glint_records = records_of(records, "glint")
    metrics = {system: system_metrics(records_of(records, system)) for system in SYSTEM_NAMES}
    rung_rows = by_rung(glint_records)
    share = repair_share_of_fixes(glint_records)

    out = ["# Glint eval results", "", "Generated by `python -m eval.report`. Do not edit by hand.", ""]
    out += ["## Frozen corpus hash and config", "", *config_lines(run_cfg, records, split, tag, frozen), ""]

    out += ["## Oracle controls", "", *control_lines(controls, records), ""]

    out += ["## Main table", ""]
    if derived_glint:
        out += table(
            MAIN_HEADER,
            [
                main_row("Glint v1", metrics["glint"]),
                main_row(DERIVED_NAME, system_metrics(derived_glint)),
                main_row(SYSTEM_NAMES["baseline"], metrics["baseline"]),
            ],
        )
        out += ["", DERIVED_NOTE]
    else:
        out += table(MAIN_HEADER, [main_row(SYSTEM_NAMES[s], metrics[s]) for s in SYSTEM_NAMES if metrics[s]["n_bug_runs"] or metrics[s]["n_clean_runs"]])
    out += [
        "",
        ("True-fix rate = TRUE_FIX / all bug runs. Precision when claiming fixed = TRUE_FIX / runs with a claimed fix. "
        "Wrong-fix rate = WRONG_FIX / all bug runs. Abstain rate = runs without a claimed fix / all bug runs. "
        "Latency and cost are over bug runs. \"n/a\" means the denominator is zero."),
        "",
    ]

    out += ["## Glint by rung", ""]
    out += table(
        ["Rung", "Bug runs", "Repro valid rate", "True-fix rate"],
        [[r["rung"], str(r["n"]), pct(r["repro_valid_rate"]), pct(r["true_fix_rate"])] for r in rung_rows],
    )
    out += ["", "Repro valid = the frozen repro test fails by assertion on the mutant and passes on the unmutated app. Both rates are over all bug runs of the rung, so a run without a repro test counts as not valid.", ""]

    no_repair = system_metrics(without_repair(glint_records))
    out += ["## Repair round", ""]
    out += [f"- Repair share of Glint's true fixes: {pct(share)}", ""]
    out += table(MAIN_HEADER, [main_row("Glint", metrics["glint"]), main_row("Glint without repair (derived)", no_repair)])
    out += ["", "\"Without repair\" recounts the same runs with every repair-round win turned into no claimed fix.", ""]

    out += ["## Failures", ""]
    failure_rows = failures(records)
    if failure_rows:
        out += table(
            ["Mutant", "Rung", "System", "First failing gate or stage", "Category"],
            [[f["mutant"], f["rung"], f["system"], f["where"], f["category"]] for f in failure_rows],
        )
    else:
        out += ["No bug run failed."]
    out += [""]

    out += ["## Predictions", ""]
    if predictions_text is None:
        out += ["`eval/PREDICTIONS.md` does not exist, so there is nothing to score."]
    else:
        scored = score_predictions(predictions_text, metrics["glint"], metrics["baseline"], {"repair_share": share, "by_rung": rung_rows})
        if scored:
            out += table(["Prediction", "Actual", "Verdict"], [[p["prediction"], p["actual"], p["verdict"]] for p in scored])
        else:
            out += ["`eval/PREDICTIONS.md` has no sentence in the plan's wording, so nothing was scored."]
    out += [""]

    if "v1" in other_tags and "v2" in other_tags:
        out += ["## v1 vs v2", ""]
        rows = []
        for version in ("v1", "v2"):
            for system, name in SYSTEM_NAMES.items():
                rows.append(main_row(f"{name} ({version})", system_metrics(records_of(other_tags[version], system))))
        out += [*table(MAIN_HEADER, rows), ""]

    out += ["## Limitations", "", *limitation_lines(records, split), ""]
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write RESULTS.md and eval/results/summary.json from eval result records.")
    parser.add_argument("--split", choices=SPLITS, default=None, help="default: test if it has results, else dev")
    parser.add_argument("--tag", choices=("v1", "v2"), default=None, help="default: the newest tag that has results")
    parser.add_argument("--results", type=Path, default=RESULTS_ROOT, help="results directory (default eval/results)")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    chosen = choose_set(args.results, args.split, args.tag)
    if chosen is None:
        raise SystemExit(f"no result records under {args.results}. Run python -m eval.run_eval first.")
    split, tag = chosen
    set_dir = args.results / set_name(split, tag)
    records = load_records(set_dir)
    cfg_path = set_dir / "run_config.json"
    run_cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    other_tags = {t: load_records(args.results / set_name(split, t)) for t in ("v1", "v2")}
    other_tags = {t: rs for t, rs in other_tags.items() if rs}
    frozen = frozen_hash()
    predictions = PREDICTIONS_MD.read_text(encoding="utf-8") if PREDICTIONS_MD.is_file() else None

    controls_path = set_dir / "controls.json"
    controls = json.loads(controls_path.read_text(encoding="utf-8")) if controls_path.is_file() else None
    derived_dir = args.results / f"{split}_v2_derived"
    derived_glint = records_of(load_records(derived_dir), "glint") if tag == "v1" and derived_dir.is_dir() else None
    RESULTS_MD.write_text(
        render(records, split, tag, run_cfg, frozen, predictions, other_tags, controls, derived_glint or None),
        encoding="utf-8",
    )
    SUMMARY_JSON.parent.mkdir(parents=True, exist_ok=True)
    summary = build_summary(records, frozen, derived_glint or None)
    if derived_glint:
        summary["note"] = DERIVED_NOTE
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"{len(records)} records from {set_dir} -> {RESULTS_MD} and {SUMMARY_JSON}")


if __name__ == "__main__":
    main()
