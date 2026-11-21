"""Eval runner: runs Glint and the baseline over the corpus and scores every patch with the hidden oracle.

This is the only module that calls the oracle (eval.fuzz.differential against the unmutated app).
Answer-key hygiene: for a mutant the systems under test receive nothing but the bug video and the mutant's
own app directory. From meta.json the "split" field is read for case discovery, and the "file" field is
read only in phase 2, after every model run has finished, to build the oracle's answer-key control.

Two phases:
  1. All model and pipeline runs. Each record saves the claimed patch, with oracle "PENDING".
  2. Oracle scoring alone, after phase 1 has fully finished, at most ORACLE_PARALLEL browser jobs at once.
     It starts with controls that need no model call; if a control fails nothing is scored.
A divergence counts only if it reproduces: the diverging sequence is replayed ORACLE_REPLAYS more times and
must diverge at the same step every time. Otherwise it is logged as flaky and the next sequence is tried.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, async_playwright

from eval import baselines
from eval.fuzz import differential
from glint import config, gemini
from glint.pipeline import new_run_id, run_pipeline
from glint.repro import run_test
from glint.schemas import Patch, RunResult
from glint.workspace import PatchRejected, apply_patch, make_workspace, serve

log = logging.getLogger("eval.run_eval")

APP_ID = "todomvc"
REPO_ROOT = Path(__file__).resolve().parent.parent
ORIGINAL_APP = REPO_ROOT / "apps" / APP_ID
RESULTS_ROOT = REPO_ROOT / "eval" / "results"
DEFAULT_CORPUS = REPO_ROOT / "eval" / "corpus"

SYSTEMS = ("glint", "baseline")
RUNGS = ("r0", "r3")
ORACLE_N_SEQ = 200
ORACLE_SEED = 10_000
ORACLE_PARALLEL = 4
ORACLE_ATTEMPTS = 2
ORACLE_REPLAYS = 2  # extra replays of a diverging sequence; all must diverge at the same step
SEQUENCE_LENGTH = 12  # eval.fuzz.differential default, part of its snapshot cache key
REPRO_NAME = "repro_test.py"

NO_PATCH = "NO_PATCH"
PENDING = "PENDING"  # a patch was claimed in phase 1 and is waiting for the oracle in phase 2
TRUE_FIX = "TRUE_FIX"
WRONG_FIX = "WRONG_FIX"
HARMLESS_PATCH = "HARMLESS_PATCH"  # clean video only: a patch was claimed but behavior did not change


@dataclass(frozen=True)
class Case:
    """One video to run: a mutant (kind "bug") or a recording of the unmutated app (kind "clean")."""

    case_id: str
    kind: str
    rung: str
    video: Path
    app_dir: Path


@dataclass
class OracleCtx:
    """Shared state for oracle calls: one browser and a cache of the original app's snapshots."""

    browser: Browser | None
    original: Path = ORIGINAL_APP
    n_seq: int = ORACLE_N_SEQ
    seed: int = ORACLE_SEED
    cache: dict[tuple[str, int, int], list[dict[str, Any]]] = field(default_factory=dict)
    slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(ORACLE_PARALLEL))
    flaky: list[dict[str, Any]] = field(default_factory=list)
    verdicts: dict[str, asyncio.Task[dict[str, Any] | None]] = field(default_factory=dict)
    cache_hits: int = 0


# --- corpus discovery ---------------------------------------------------------------------------


def read_split(meta_path: Path) -> str | None:
    """The split of a mutant. This is the only thing ever read from meta.json; nothing else leaves here."""
    try:
        value = json.loads(meta_path.read_text(encoding="utf-8")).get("split")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def find_video(folder: Path, rung: str) -> Path | None:
    """The recording for one rung in a corpus folder (video_<rung>.<ext>), or None."""
    matches = sorted(
        p for p in folder.glob(f"video_{rung}.*") if p.is_file() and p.suffix.lower() in config.ACCEPTED_EXT
    )
    return matches[0] if matches else None


def discover_cases(
    corpus: Path, split: str, rungs: list[str], include_clean: bool, limit: int | None = None
) -> list[Case]:
    """Every (mutant, rung) of the split that has a video, plus the clean videos when asked.

    limit keeps the first N mutants of the split, sorted by id. Clean videos are not limited.
    """
    app_root = corpus / APP_ID
    mutant_dirs = sorted(
        d
        for d in app_root.iterdir()
        if d.is_dir() and d.name != "clean" and (d / "app").is_dir() and read_split(d / "meta.json") == split
    )
    if limit is not None:
        mutant_dirs = mutant_dirs[:limit]
    cases: list[Case] = []
    for mutant_dir in mutant_dirs:
        for rung in rungs:
            video = find_video(mutant_dir, rung)
            if video is not None:
                cases.append(Case(mutant_dir.name, "bug", rung, video, mutant_dir / "app"))
    clean_root = app_root / "clean"
    if include_clean and clean_root.is_dir():
        for clean_dir in sorted(d for d in clean_root.iterdir() if d.is_dir()):
            if (clean_dir / "meta.json").is_file() and read_split(clean_dir / "meta.json") is None:
                continue  # excluded before the scored run, see eval/corpus/EXCLUSIONS.md
            for rung in rungs:
                video = find_video(clean_dir, rung)
                if video is not None:
                    cases.append(Case(clean_dir.name, "clean", rung, video, ORIGINAL_APP))
    return cases


def results_dir(split: str, tag: str | None) -> Path:
    """eval/results/<split> or eval/results/<split>_<tag>."""
    return RESULTS_ROOT / (f"{split}_{tag}" if tag else split)


def record_path(out_dir: Path, system: str, case: Case) -> Path:
    """eval/results/<set>/<system>/<mutant>_<rung>.json."""
    return out_dir / system / f"{case.case_id}_{case.rung}.json"


# --- the hidden oracle --------------------------------------------------------------------------


async def reproducible_divergence(oracle: OracleCtx, candidate: Path, label: str = "") -> dict[str, Any] | None:
    """First divergence between candidate and the unmutated app that reproduces, or None.

    Sequences are checked one at a time. When one diverges it is replayed ORACLE_REPLAYS more times with no
    cached snapshots. It counts only if every replay diverges at the same step; otherwise it is recorded in
    oracle.flaky and the next sequence is tried.
    """
    cache_prefix = str(oracle.original.resolve())
    for offset in range(oracle.n_seq):
        seq_seed = oracle.seed + offset
        first = await differential(
            oracle.original, candidate, 1, seq_seed, browser=oracle.browser, orig_cache=oracle.cache
        )
        if first is None:
            continue
        steps: list[int | None] = [first["step"]]
        for _ in range(ORACLE_REPLAYS):
            again = await differential(oracle.original, candidate, 1, seq_seed, browser=oracle.browser)
            steps.append(None if again is None else again["step"])
        if all(step == steps[0] for step in steps):
            return {**first, "sequence_seed": seq_seed}
        oracle.flaky.append({"candidate": label or str(candidate), "sequence_seed": seq_seed, "steps": steps})
        oracle.cache.pop((cache_prefix, seq_seed, SEQUENCE_LENGTH), None)
        log.warning("flaky divergence for %s at sequence seed %d (steps %s), not counted", label, seq_seed, steps)
    return None


def content_hash(app_dir: Path) -> str:
    """SHA-256 over every file of an app directory: relative path and bytes, in sorted order."""
    digest = hashlib.sha256()
    for path in sorted(p for p in app_dir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(app_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


async def _oracle_verdict(oracle: OracleCtx, candidate: Path, label: str) -> dict[str, Any] | None:
    """Oracle verdict for an app directory, scored once per distinct content.

    Byte-identical candidates (for example an answer-key control and the original app, or the same fix
    proposed by two systems) share one scoring. The first request starts it; later ones wait for it.
    """
    key = content_hash(candidate)
    task = oracle.verdicts.get(key)
    if task is None:
        task = asyncio.ensure_future(_score_uncached(oracle, candidate, label))
        oracle.verdicts[key] = task
    else:
        oracle.cache_hits += 1
    return await task


async def _score_uncached(oracle: OracleCtx, candidate: Path, label: str) -> dict[str, Any] | None:
    """reproducible_divergence under the oracle's concurrency limit, with one retry on a browser failure."""
    async with oracle.slots:
        for attempt in range(1, ORACLE_ATTEMPTS + 1):
            try:
                return await reproducible_divergence(oracle, candidate, label)
            except Exception as exc:
                if attempt == ORACLE_ATTEMPTS:
                    raise
                log.warning("oracle attempt %d failed (%s), retrying", attempt, type(exc).__name__)
    return None


async def score_patch(patch: Patch | None, case: Case, oracle: OracleCtx) -> tuple[str, str]:
    """Oracle verdict for one run: (label, detail). Compares the patched app with the unmutated app."""
    if patch is None:
        return NO_PATCH, ""
    ws = make_workspace(case.app_dir)
    try:
        try:
            apply_patch(ws, patch)
        except PatchRejected as exc:
            return WRONG_FIX, f"patch rejected: {exc}"
        divergence = await _oracle_verdict(oracle, ws, f"{case.case_id} {case.rung} patch")
        if divergence is None:
            return (TRUE_FIX if case.kind == "bug" else HARMLESS_PATCH), ""
        return WRONG_FIX, (
            f"behavior differs from the unmutated app at step {divergence['step']} of fuzz sequence "
            f"{divergence['sequence_seed']} (reproduced {ORACLE_REPLAYS + 1} of {ORACLE_REPLAYS + 1} times)"
        )
    finally:
        shutil.rmtree(ws, ignore_errors=True)


async def run_controls(cases: list[Case], corpus: Path, oracle: OracleCtx) -> list[dict[str, Any]]:
    """Oracle controls, no model calls. The original app must score TRUE_FIX, every in-split mutant must
    score WRONG_FIX unpatched, and TRUE_FIX once its mutated file is restored from the unmutated app."""
    mutants = sorted({c.case_id: c.app_dir for c in cases if c.kind == "bug"}.items())
    rows: list[dict[str, Any]] = []

    async def check(name: str, candidate: Path, expected: str) -> None:
        divergence = await _oracle_verdict(oracle, candidate, name)
        got = TRUE_FIX if divergence is None else WRONG_FIX
        detail = "" if divergence is None else f"step {divergence['step']} of sequence {divergence['sequence_seed']}"
        rows.append({"control": name, "expected": expected, "got": got, "ok": got == expected, "detail": detail})

    restored: list[Path] = []
    try:
        jobs = [check("original app as candidate", oracle.original, TRUE_FIX)]
        for mutant_id, app_dir in mutants:
            jobs.append(check(f"{mutant_id} unpatched", app_dir, WRONG_FIX))
            meta = json.loads((corpus / APP_ID / mutant_id / "meta.json").read_text(encoding="utf-8"))
            ws = make_workspace(app_dir)
            restored.append(ws)
            shutil.copyfile(oracle.original / meta["file"], ws / meta["file"])
            jobs.append(check(f"{mutant_id} answer key restored", ws, TRUE_FIX))
        await asyncio.gather(*jobs)
    finally:
        for ws in restored:
            shutil.rmtree(ws, ignore_errors=True)
    return sorted(rows, key=lambda r: r["control"])


async def repro_is_valid(bundle: Path, mutant_app: Path, original: Path = ORIGINAL_APP) -> bool | None:
    """True when the frozen repro test fails by assertion on the mutant and passes on the unmutated app.

    None when the run produced no repro test.
    """
    source = bundle / REPRO_NAME
    if not source.is_file():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="glint_eval_repro_"))
    try:
        test = tmp / REPRO_NAME
        shutil.copyfile(source, test)
        try:
            async with serve(mutant_app) as url:
                on_mutant, _, _ = await run_test(test, url, tmp / "out_mutant")
            if on_mutant != "failed_assertion":
                return False
            async with serve(original) as url:
                on_original, _, _ = await run_test(test, url, tmp / "out_original")
        except TimeoutError:
            return False
        return on_original == "passed"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- running one case ---------------------------------------------------------------------------


def fresh_run_id(suffix: str = "") -> str:
    """A run id whose bundle directory does not exist yet."""
    while True:
        run_id = f"{new_run_id()}{suffix}"
        if not (Path(config.RUNS_DIR) / run_id).exists():
            return run_id


def glint_failed_gate(result: RunResult) -> str | None:
    """The first failing run-level gate, else the gate that rejected most candidates, else None."""
    for gate in result.gates:
        if not gate.passed:
            return gate.gate
    if result.winner_id is not None:
        return None
    first_failures = Counter(
        next(g.gate for g in c.gates if not g.passed) for c in result.candidates if any(not g.passed for g in c.gates)
    )
    return first_failures.most_common(1)[0][0] if first_failures else None


def base_record(case: Case, system: str, split: str, run_id: str) -> dict[str, Any]:
    """Fields every result record has, with their defaults."""
    return {
        "mutant_id": case.case_id,
        "kind": case.kind,
        "split": split,
        "rung": case.rung,
        "system": system,
        "status": "",
        "claimed_fix": False,
        "oracle": NO_PATCH,
        "oracle_detail": "",
        "patch": None,
        "repro_valid": None,
        "fixed_in_repair": False,
        "latency_s": 0.0,
        "cost_usd": 0.0,
        "tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "run_id": run_id,
        "stage_reached": None,
        "failed_gate": None,
        "reason": "",
        "internal_error": False,
        "finished_at": "",
    }


async def run_glint(case: Case, split: str) -> dict[str, Any]:
    """Phase 1: run the Glint pipeline on one case and save its claimed fix for the oracle."""
    run_id = fresh_run_id()
    record = base_record(case, "glint", split, run_id)
    result = await run_pipeline(case.video, case.app_dir, APP_ID, run_id, None)
    winner = next((c for c in result.candidates if c.id == result.winner_id), None)
    patch = winner.patch if result.status == "VERIFIED_FIX" and winner is not None else None
    record.update(
        status=result.status,
        claimed_fix=patch is not None,
        oracle=PENDING if patch is not None else NO_PATCH,
        patch=patch.model_dump() if patch is not None else None,
        fixed_in_repair=patch is not None and winner is not None and winner.source == "repair",
        latency_s=result.latency_s,
        cost_usd=round(result.usage.cost_usd, 6),
        tokens=result.usage.input_tokens + result.usage.output_tokens,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        stage_reached=result.stage_reached,
        failed_gate=glint_failed_gate(result),
        reason=result.reason,
        internal_error=result.internal_error,
    )
    return record


async def run_baseline(case: Case, split: str) -> dict[str, Any]:
    """Phase 1: run the single-call baseline on one case and save its patch. Raises if the model call fails."""
    run_id = fresh_run_id("_baseline")
    record = base_record(case, "baseline", split, run_id)
    gemini.bind_run(run_id)
    started = time.monotonic()
    patch = await baselines.single_call(case.video, case.app_dir)
    latency_s = round(time.monotonic() - started, 2)
    usage = gemini.usage_snapshot(run_id)
    record.update(
        status="PATCH" if patch is not None else NO_PATCH,
        claimed_fix=patch is not None,
        oracle=PENDING if patch is not None else NO_PATCH,
        patch=patch.model_dump() if patch is not None else None,
        latency_s=latency_s,
        cost_usd=round(usage.cost_usd, 6),
        tokens=usage.input_tokens + usage.output_tokens,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        reason=patch.rationale if patch is not None else "The model proposed no file changes.",
    )
    return record


RUNNERS = {"glint": run_glint, "baseline": run_baseline}


def summary_line(record: dict[str, Any], note: str = "") -> str:
    """One line per run for the terminal."""
    return (
        f"{record['mutant_id']} {record['rung']} {record['system']:<8} status={record['status']} "
        f"claimed_fix={record['claimed_fix']} oracle={record['oracle']} repro_valid={record['repro_valid']} "
        f"latency={record['latency_s']:.1f}s cost=${record['cost_usd']:.4f}{note}"
    )


def run_config(args: argparse.Namespace, corpus: Path, n_cases: int) -> dict[str, Any]:
    """What this result set was produced with; the report prints it."""
    return {
        "written_at": datetime.now(UTC).isoformat(),
        "split": args.split,
        "tag": args.tag,
        "corpus": str(corpus),
        "systems": args.systems,
        "rungs": args.rungs,
        "include_clean": args.include_clean,
        "limit": args.limit,
        "n_cases": n_cases,
        "models": {"neon": config.NEON, "hydrogen": config.HYDROGEN, "decision": config.DECISION},
        "baseline_model": config.NEON,
        "thresholds": {
            "TH_G0_IS_DEFECT": config.TH_G0_IS_DEFECT,
            "TH_G1B_SYMPTOM_MATCH": config.TH_G1B_SYMPTOM_MATCH,
            "TH_G4_DEFECT_GONE": config.TH_G4_DEFECT_GONE,
        },
        "n_candidates": config.N_CANDIDATES,
        "run_budget_s": config.RUN_BUDGET_S,
        "oracle": {"n_seq": ORACLE_N_SEQ, "seed": ORACLE_SEED, "replays": ORACLE_REPLAYS, "parallel": ORACLE_PARALLEL},
    }


async def phase_one(args: argparse.Namespace, cases: list[Case], out_dir: Path) -> int:
    """All model and pipeline runs. Returns the number of runs that could not be completed."""
    slots = asyncio.Semaphore(args.concurrency)
    errors = 0

    async def one(case: Case, system: str) -> None:
        nonlocal errors
        path = record_path(out_dir, system, case)
        if path.is_file() and not args.force:
            print(summary_line(json.loads(path.read_text(encoding="utf-8")), " (already done, skipped)"))
            return
        async with slots:
            try:
                record = await RUNNERS[system](case, args.split)
            except Exception as exc:
                errors += 1
                log.exception("%s %s %s could not be completed", case.case_id, case.rung, system)
                print(f"{case.case_id} {case.rung} {system:<8} ERROR {type(exc).__name__}: not recorded, rerun")
                return
        record["finished_at"] = datetime.now(UTC).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(summary_line(record))

    await asyncio.gather(*(one(case, system) for case in cases for system in args.systems))
    return errors


async def phase_two(args: argparse.Namespace, cases: list[Case], corpus: Path, out_dir: Path) -> int:
    """Oracle scoring alone. Returns 0, or 2 when a control failed (then nothing is scored)."""
    todo: list[tuple[Path, dict[str, Any], Case]] = []
    for case in cases:
        for system in args.systems:
            path = record_path(out_dir, system, case)
            if path.is_file():
                record = json.loads(path.read_text(encoding="utf-8"))
                needs_repro = system == "glint" and case.kind == "bug" and not record.get("repro_checked")
                if record.get("oracle") == PENDING or needs_repro:
                    todo.append((path, record, case))
    controls_path = out_dir / "controls.json"
    if not todo and controls_path.is_file():
        print("phase 2: nothing to score")
        return 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        oracle = OracleCtx(browser=browser)
        try:
            print(f"phase 2: oracle controls ({ORACLE_N_SEQ} sequences each, {ORACLE_PARALLEL} at a time)")
            controls = await run_controls(cases, corpus, oracle)
            ok = all(row["ok"] for row in controls)
            controls_path.write_text(
                json.dumps({"ok": ok, "rows": controls, "flaky": oracle.flaky, "n_seq": ORACLE_N_SEQ}, indent=2), "utf-8"
            )
            for row in controls:
                verdict = "ok" if row["ok"] else "CONTROL FAILED"
                print(f"control: {row['control']:<32} expected={row['expected']:<9} got={row['got']:<9} {verdict}")
            if not ok:
                print("A control failed, so oracle results would be invalid. Nothing was scored.")
                return 2

            async def score(path: Path, record: dict[str, Any], case: Case) -> None:
                if record.get("oracle") == PENDING:
                    label, detail = await score_patch(Patch.model_validate(record["patch"]), case, oracle)
                    record.update(oracle=label, oracle_detail=detail)
                path.write_text(json.dumps(record, indent=2), encoding="utf-8")
                print(summary_line(record))

            await asyncio.gather(*(score(*item) for item in todo))
            # The repro check also drives a browser, so it runs after the oracle, one run at a time.
            for path, record, case in todo:
                if record["system"] == "glint" and case.kind == "bug" and not record.get("repro_checked"):
                    bundle = Path(config.RUNS_DIR) / record["run_id"]
                    record["repro_valid"] = await repro_is_valid(bundle, case.app_dir, oracle.original)
                    record["repro_checked"] = True
                    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            data = json.loads(controls_path.read_text(encoding="utf-8"))
            data["flaky"] = oracle.flaky
            controls_path.write_text(json.dumps(data, indent=2), "utf-8")
            data["distinct_apps_scored"] = len(oracle.verdicts)
            data["verdicts_reused"] = oracle.cache_hits
            controls_path.write_text(json.dumps(data, indent=2), "utf-8")
            print(
                f"phase 2 done: {len(oracle.verdicts)} distinct apps scored, {oracle.cache_hits} verdicts reused, "
                f"{len(oracle.flaky)} flaky sequence(s) seen and not counted"
            )
        finally:
            await browser.close()
    return 0


async def run_all(args: argparse.Namespace) -> int:
    """Phase 1 (models), then phase 2 (oracle). Returns a process exit code."""
    corpus = args.corpus.resolve()
    cases = discover_cases(corpus, args.split, args.rungs, args.include_clean, args.limit)
    out_dir = results_dir(args.split, args.tag)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run_config.json").write_text(json.dumps(run_config(args, corpus, len(cases)), indent=2), "utf-8")
    print(f"{len(cases)} videos x {len(args.systems)} systems from {corpus} -> {out_dir}")
    print("phase 1: model and pipeline runs")
    errors = await phase_one(args, cases, out_dir)
    if errors:
        print(f"phase 1 left {errors} run(s) unrecorded. Rerun the same command; the oracle has not run.")
        return 1
    return await phase_two(args, cases, corpus, out_dir)


def _csv_choice(allowed: tuple[str, ...]) -> Any:
    def parse(text: str) -> list[str]:
        items = [item.strip().lower() for item in text.split(",") if item.strip()]
        bad = [item for item in items if item not in allowed]
        if bad or not items:
            raise argparse.ArgumentTypeError(f"expected a comma-separated subset of {','.join(allowed)}")
        return list(dict.fromkeys(items))

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Glint and the single-call baseline over the eval corpus and score them with the oracle."
    )
    parser.add_argument("--split", choices=("dev", "test"), required=True, help="which mutants to run")
    parser.add_argument("--systems", type=_csv_choice(SYSTEMS), default=list(SYSTEMS), help="glint,baseline")
    parser.add_argument("--rungs", type=_csv_choice(RUNGS), default=list(RUNGS), help="r0,r3")
    parser.add_argument("--include-clean", action="store_true", help="also run the clean (no bug) videos")
    parser.add_argument("--concurrency", type=int, default=8, help="runs in flight at once (lower it on 429s)")
    parser.add_argument("--tag", choices=("v1", "v2"), default=None, help="write under eval/results/<split>_<tag>/")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS, help="corpus directory (default eval/corpus)")
    parser.add_argument("--limit", type=int, default=None, help="only the first N mutants of the split, by id")
    parser.add_argument("--force", action="store_true", help="rerun and overwrite records that already exist")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be at least 1")
    if not (args.corpus / APP_ID).is_dir():
        raise SystemExit(f"no corpus at {args.corpus / APP_ID}")
    sys.exit(asyncio.run(run_all(args)))


if __name__ == "__main__":
    main()
