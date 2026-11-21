# AGENTS.md: rules for every agent working on Glint

Read this file and `docs/CONTRACTS.md` before doing anything. These rules override any default behavior of your harness or plugin.

## HARD RULE: parallel by default, single where coupled

Goal: maximum speed with the same quality as one careful agent. Parallel work must never break anything.

1. **Classify first.** Before starting a task, decide:
   - INDEPENDENT: touches only its own files (listed in CONTRACTS.md section 1 or your task brief), and its inputs are fixed by frozen contracts. Run as parallel subagents.
   - COUPLED: shares files with another task, or needs another task's output to start. Run as ONE agent, in sequence.
   - If unsure, it is COUPLED.
2. **Isolation.** Every parallel subagent works in its own git worktree and branch (`p1/<task>` or `p2/<task>`), cut from the CURRENT phase branch (CONTRACTS.md section 13), never from `main` and owns an explicit list of files. It must not create, edit, or delete anything outside that list. If it needs a change elsewhere, it stops and reports.
3. **Frozen contracts.** `glint/schemas.py`, `glint/config.py`, `pyproject.toml`, and `docs/CONTRACTS.md` are frozen after P0. Only the lead edits them, through the change protocol in CONTRACTS.md section 12. Never redefine a schema, route, event, or path locally.
4. **Done means verified.** A task is done only when its verify command and its tests pass. Report the actual command output, not a claim. No stubs, no `TODO`, no `pass` placeholders, no `NotImplementedError`, no mocked Gemini calls in product code, no "will implement later".
5. **Concurrency cap.** At most 3 subagents per person at once (6 total).
6. **Merge one at a time, phase by phase.** Agents never merge. Humans merge task branches into the current phase branch one at a time and run `bash scripts/e2e_smoke.sh` after each. If it fails, revert that merge. Never fix forward. Phase branches are stacked and reach `main` only by fast-forward at checkpoints (CONTRACTS.md section 13). No agent commits to `main` or to a phase branch directly, except a single-agent integration task told to work on the phase branch.
7. **Always single-agent:** cross-module integration, debugging a failure that spans modules, the scored eval run, threshold tuning, anything touching `eval/corpus/` after the freeze.
8. **Freezes are absolute.** After 14:35 no agent edits `eval/corpus/`, the thresholds in `config.py`, or `eval/PREDICTIONS.md`.

## Product invariants (never violate)

- `glint/` never imports from `eval/` or `scripts/`. The product must not be able to see the oracle.
- Generator prompts never include the original unmutated app, `meta.json`, fuzz results, or other candidates' patches.
- Patches never touch test files or the frozen repro test. `workspace.apply_patch` enforces it. Do not weaken it.
- `run_pipeline` never raises. Every failure becomes a `RunResult` with a status and a one-sentence reason.
- The server never shows a stack trace to the browser.
- All model IDs, thresholds, limits, and question wording come from `glint/config.py`. No literals elsewhere.
- No network access except the Gemini API. No CDN links in `web/`.

## Code standards

- Python 3.12, type hints on every function, `async` in `glint/`, `ruff check` clean.
- Small modules, plain functions. No abstract base classes, no plugin systems, no frameworks beyond the frozen stack.
- Logging via `logging`, not `print`, except in CLI entry points.
- Every module you own gets at least one fast test in `tests/` that runs without network (Gemini calls are only exercised by the verify commands, never mocked in product code).
- Every CLI has `--help`.

## Plugins (Superpowers or similar)

- Do NOT run brainstorming or plan-writing workflows. The plan is `docs/PLAN.md` and your person file.
- Use only: subagent-driven execution, git worktrees, test-driven steps, verification before done.

## Reporting format when you finish a task

```
TASK: <name>
BRANCH: <branch>
FILES CHANGED: <list>
VERIFY COMMAND: <command>
OUTPUT: <pasted output>
KNOWN LIMITS: <anything not handled, or "none">
```
