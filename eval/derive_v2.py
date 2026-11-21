"""Glint v2 numbers derived from the saved v1 runs. No model is called.

Each saved v1 Glint run is recounted under the v2 selection rule (glint.pipeline.select_winner) using the
run's saved candidates and gate results. A fix that v2 claims and v1 did not is scored by the same hidden
oracle, through eval.run_eval, after the same controls. Results go to eval/results/<split>_v2_derived/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from eval import run_eval
from glint import config
from glint.pipeline import select_winner
from glint.schemas import Patch, RunResult

log = logging.getLogger("eval.derive_v2")


def recount(record: dict[str, Any], result: RunResult) -> dict[str, Any]:
    """The v1 record as it would read under the v2 selection rule. Oracle is PENDING for a new claim."""
    new = dict(record)
    new["derived_from"] = "v1"
    winner, basis = select_winner(result.candidates)
    if winner is None or winner.patch is None:
        new.update(claimed_fix=False, patch=None, oracle=run_eval.NO_PATCH, oracle_detail="", fixed_in_repair=False)
        if record.get("status") == "VERIFIED_FIX":
            new["status"] = "NO_VERIFIED_FIX"
        return new
    patch = winner.patch.model_dump()
    same_claim = bool(record.get("claimed_fix")) and record.get("patch") == patch
    lines = "1 line" if winner.lines_changed == 1 else f"{winner.lines_changed} lines"
    files = ", ".join(winner.files_changed) or "the app"
    new.update(
        status="VERIFIED_FIX",
        claimed_fix=True,
        patch=patch,
        fixed_in_repair=winner.source == "repair",
        failed_gate=None,
        reason=f"Verified fix {winner.id} changed {lines} in {files} ({basis}).",
        v2_basis=basis,
    )
    if not same_claim:
        new.update(oracle=run_eval.PENDING, oracle_detail="")
    return new


async def derive(split: str, corpus: Path) -> int:
    cases = run_eval.discover_cases(corpus, split, list(run_eval.RUNGS), include_clean=True)
    src = run_eval.results_dir(split, "v1")
    dst = run_eval.RESULTS_ROOT / f"{split}_v2_derived"
    records: list[tuple[run_eval.Case, dict[str, Any]]] = []
    for case in cases:
        path = run_eval.record_path(src, "glint", case)
        if not path.is_file():
            print(f"missing v1 record for {case.case_id} {case.rung}; run the v1 eval first")
            return 1
        record = json.loads(path.read_text(encoding="utf-8"))
        result_path = Path(config.RUNS_DIR) / record["run_id"] / "result.json"
        result = RunResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        records.append((case, recount(record, result)))

    new_claims = [(case, rec) for case, rec in records if rec["oracle"] == run_eval.PENDING]
    print(f"{len(records)} v1 Glint runs recounted; {len(new_claims)} newly claimed fix(es) to score")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        oracle = run_eval.OracleCtx(browser=browser)
        try:
            controls = await run_eval.run_controls(cases, corpus, oracle)
            ok = all(row["ok"] for row in controls)
            dst.mkdir(parents=True, exist_ok=True)
            (dst / "controls.json").write_text(
                json.dumps({"ok": ok, "rows": controls, "flaky": oracle.flaky, "n_seq": run_eval.ORACLE_N_SEQ}, indent=2),
                "utf-8",
            )
            print(f"controls: {sum(r['ok'] for r in controls)} of {len(controls)} passed")
            if not ok:
                print("A control failed, so oracle results would be invalid. Nothing was scored.")
                return 2

            async def score(case: run_eval.Case, rec: dict[str, Any]) -> None:
                label, detail = await run_eval.score_patch(Patch.model_validate(rec["patch"]), case, oracle)
                rec.update(oracle=label, oracle_detail=detail)

            await asyncio.gather(*(score(case, rec) for case, rec in new_claims))
        finally:
            await browser.close()

    for case, rec in records:
        path = run_eval.record_path(dst, "glint", case)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    for case, rec in new_claims:
        print(f"new claim {case.case_id} {case.rung}: oracle={rec['oracle']} basis={rec.get('v2_basis')} {rec['oracle_detail']}")
    print(f"wrote {len(records)} records to {dst} ({len(oracle.flaky)} flaky sequence(s) not counted)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Recount saved v1 Glint runs under the v2 selection rule. No model calls.")
    parser.add_argument("--split", default="test", choices=["dev", "test"], help="which split's v1 results to recount")
    parser.add_argument("--corpus", type=Path, default=run_eval.DEFAULT_CORPUS, help="corpus root")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(derive(args.split, args.corpus.resolve())))


if __name__ == "__main__":
    main()
