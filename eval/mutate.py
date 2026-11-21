"""Deterministic JS single-line mutator for generating bug corpora."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from eval.fuzz import check_loads_cleanly, differential

logger = logging.getLogger(__name__)

MUTATABLE_FILES = ("controller.js", "model.js", "view.js", "store.js", "template.js")

_RE_CMP = re.compile(r"(===|!==|<=|>=|<|>)")
_RE_LOGIC = re.compile(r"(&&|\|\|)")
_RE_BOOL = re.compile(r"\b(true|false)\b")
_RE_NUM = re.compile(r"(?<![.\w$])(\d+)(?![.\w$])")
_RE_STR = re.compile(r'(["\'])([a-zA-Z][a-zA-Z0-9_-]{1,20})\1')
_RE_CALL_TWO_ARGS = re.compile(
    r"(\b[A-Za-z_$][\w$.]*\s*\(\s*)([A-Za-z_$][\w$.]*)(\s*,\s*)([A-Za-z_$][\w$.]*)(\s*\))"
)
_RE_IF_COND = re.compile(r"^(\s*if\s*\()(!?)([^()]+)(\)\s*(?:\{|$))")


@dataclass(frozen=True)
class MutationSite:
    file: str
    line: int
    operator: str
    original_snippet: str
    mutated_snippet: str


def _is_comment_or_blank(line: str, in_block_comment: bool) -> tuple[bool, bool]:
    stripped = line.strip()
    if in_block_comment:
        if "*/" in stripped:
            return True, False
        return True, True
    if stripped.startswith("/*"):
        if "*/" in stripped[2:]:
            return True, False
        return True, True
    if not stripped or stripped.startswith(("//", "*")):
        return True, False
    return False, False


def _line_mutations(rel_file: str, line_no: int, line: str) -> list[MutationSite]:
    sites: list[MutationSite] = []
    stripped = line.strip()

    # 1. Comparison flip
    cmp_map = {"===": "!==", "!==": "===", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}
    for m in _RE_CMP.finditer(line):
        op = m.group(1)
        # Skip arrow functions =>
        if op == ">" and m.start() > 0 and line[m.start() - 1] == "=":
            continue
        mutated = line[: m.start()] + cmp_map[op] + line[m.end() :]
        sites.append(MutationSite(rel_file, line_no, "comparison_flip", line, mutated))

    # 2. Logical && <-> ||
    for m in _RE_LOGIC.finditer(line):
        op = m.group(1)
        rep = "||" if op == "&&" else "&&"
        mutated = line[: m.start()] + rep + line[m.end() :]
        sites.append(MutationSite(rel_file, line_no, "logical_swap", line, mutated))

    # 3. Boolean literal true <-> false
    for m in _RE_BOOL.finditer(line):
        val = m.group(1)
        rep = "false" if val == "true" else "true"
        mutated = line[: m.start()] + rep + line[m.end() :]
        sites.append(MutationSite(rel_file, line_no, "bool_literal", line, mutated))

    # 4. Boolean negation on if (...)
    m_if = _RE_IF_COND.match(line)
    if m_if:
        prefix, bang, cond, suffix = m_if.groups()
        new_cond = cond if bang else f"!({cond})"
        mutated = f"{prefix}{new_cond}{suffix}"
        if line.endswith("\n") and not mutated.endswith("\n"):
            mutated += "\n"
        sites.append(MutationSite(rel_file, line_no, "bool_negation", line, mutated))

    # 5. Off-by-one on numeric literal
    for m in _RE_NUM.finditer(line):
        num = int(m.group(1))
        if num > 1000:
            continue
        for delta in (1, -1):
            new_num = num + delta
            if new_num < 0:
                continue
            mutated = line[: m.start()] + str(new_num) + line[m.end() :]
            sites.append(MutationSite(rel_file, line_no, "off_by_one", line, mutated))
            break

    # 6. Swap first two call arguments
    m_call = _RE_CALL_TWO_ARGS.search(line)
    if m_call and not stripped.startswith("function "):
        pre, a1, mid, a2, post = m_call.groups()
        if a1 != a2:
            mutated = line[: m_call.start()] + f"{pre}{a2}{mid}{a1}{post}" + line[m_call.end() :]
            sites.append(MutationSite(rel_file, line_no, "swap_args", line, mutated))

    # 7. String literal in comparison or object/property/method call
    if any(tok in line for tok in ("===", "!==", ":", "className", "render", "replace")):
        for m in _RE_STR.finditer(line):
            quote, val = m.group(1), m.group(2)
            mutated = line[: m.start()] + f"{quote}{val}_x{quote}" + line[m.end() :]
            sites.append(MutationSite(rel_file, line_no, "string_literal", line, mutated))

    # 8. Delete one single-line statement
    if (
        stripped.endswith(";")
        and not stripped.startswith(("var ", "let ", "const ", "return", "\"use strict\"", "'use strict'"))
        and "{" not in stripped
        and "}" not in stripped
        and "(" in stripped
    ):
        indent = line[: len(line) - len(line.lstrip())]
        mutated = f"{indent}/* deleted */\n"
        sites.append(MutationSite(rel_file, line_no, "delete_stmt", line, mutated))

    return sites


def collect_mutation_sites(app_dir: Path, seed: int) -> list[MutationSite]:
    """Collect and deterministically shuffle all single-line mutation sites."""
    sites: list[MutationSite] = []
    for fname in MUTATABLE_FILES:
        fpath = app_dir / fname
        if not fpath.is_file():
            continue
        lines = fpath.read_text(encoding="utf-8").splitlines(keepends=True)
        in_block = False
        for idx, line in enumerate(lines, start=1):
            skip, in_block = _is_comment_or_blank(line, in_block)
            if skip:
                continue
            sites.extend(_line_mutations(fname, idx, line))

    # Deduplicate while preserving deterministic order, then shuffle with seed
    seen: set[tuple[str, int, str]] = set()
    unique: list[MutationSite] = []
    for s in sites:
        key = (s.file, s.line, s.mutated_snippet)
        if key not in seen:
            seen.add(key)
            unique.append(s)

    rng = random.Random(seed)
    rng.shuffle(unique)
    return unique


def _apply_site(app_dir: Path, dst_dir: Path, site: MutationSite) -> None:
    shutil.copytree(app_dir, dst_dir, dirs_exist_ok=True)
    target = dst_dir / site.file
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[site.line - 1] = site.mutated_snippet
    target.write_text("".join(lines), encoding="utf-8")


async def _generate_mutants_async(
    app_dir: Path,
    seed: int,
    n: int,
    out_root: Path,
    *,
    max_tries: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    sites = collect_mutation_sites(app_dir, seed)
    if max_tries is not None:
        sites = sites[:max_tries]

    kept: list[dict[str, Any]] = []
    attempted = 0
    orig_cache: dict[tuple[str, int, int], list[dict[str, Any]]] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for site in sites:
                if len(kept) >= n:
                    break
                attempted += 1
                with tempfile.TemporaryDirectory(prefix="glint_mut_") as tmp:
                    cand_dir = Path(tmp) / "app"
                    _apply_site(app_dir, cand_dir, site)
                    if not await check_loads_cleanly(cand_dir):
                        continue
                    diff = await differential(
                        app_dir,
                        cand_dir,
                        n_seq=50,
                        seed=seed,
                        browser=browser,
                        orig_cache=orig_cache,
                    )
                    if diff is None:
                        continue

                    mutant_id = f"m{len(kept) + 1:03d}"
                    m_dir = out_root / mutant_id
                    m_app = m_dir / "app"
                    if m_app.exists():
                        shutil.rmtree(m_app)
                    shutil.copytree(cand_dir, m_app)

                    meta: dict[str, Any] = {
                        "mutant_id": mutant_id,
                        "app_id": app_dir.name,
                        "file": site.file,
                        "line": site.line,
                        "operator": site.operator,
                        "original_snippet": site.original_snippet.strip(),
                        "mutated_snippet": site.mutated_snippet.strip(),
                        "seed": seed,
                        "divergent_sequence": diff["sequence"],
                        "split": None,
                    }
                    (m_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
                    kept.append(meta)
        finally:
            await browser.close()

    return kept, attempted


def generate_mutants(app_dir: Path, seed: int, n: int, out_root: Path) -> list[dict[str, Any]]:
    """Generate up to n kept mutants under out_root/<mutant_id>/app and meta.json."""
    out_root.mkdir(parents=True, exist_ok=True)
    kept, _ = asyncio.run(_generate_mutants_async(app_dir, seed, n, out_root))
    return kept


def make_one(app_dir: Path, seed: int, out_dir: Path, max_tries: int = 50) -> dict[str, Any] | None:
    """Write the first visible mutant reachable from seed to out_dir/app + out_dir/meta.json."""
    with tempfile.TemporaryDirectory(prefix="glint_one_") as tmp:
        kept, _ = asyncio.run(
            _generate_mutants_async(app_dir, seed, 1, Path(tmp), max_tries=max_tries)
        )
        if not kept:
            return None
        meta = kept[0]
        src_m = Path(tmp) / meta["mutant_id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out_app = out_dir / "app"
        if out_app.exists():
            shutil.rmtree(out_app)
        shutil.copytree(src_m / "app", out_app)
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic JS mutants for Glint eval.")
    parser.add_argument("--app", type=Path, required=True, help="Path to target app directory.")
    parser.add_argument("--seed", type=int, default=7, help="Deterministic RNG seed.")
    parser.add_argument("--n", type=int, default=28, help="Number of kept mutants to generate.")
    parser.add_argument("--out", type=Path, required=True, help="Output corpus directory.")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    kept, attempted = asyncio.run(_generate_mutants_async(args.app, args.seed, args.n, args.out))

    # Assign splits: first 8 -> dev, next 20 -> test
    for idx, meta in enumerate(kept):
        if idx < 8:
            meta["split"] = "dev"
        elif idx < 28:
            meta["split"] = "test"
        else:
            meta["split"] = None
        meta_path = args.out / meta["mutant_id"] / "meta.json"
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    op_counts = Counter(m["operator"] for m in kept)
    print(f"Attempted: {attempted}")
    print(f"Kept: {len(kept)}")
    print("Per-operator counts:")
    for op, count in sorted(op_counts.items()):
        print(f"  {op}: {count}")


if __name__ == "__main__":
    main()
