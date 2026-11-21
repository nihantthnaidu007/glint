# Glint: Person 1 (System lane)

You own the product: everything under `glint/` except `server.py`, plus `scripts/smoke.py`, `scripts/e2e_smoke.sh`, `scripts/make_live_bug.py`. You are also LEAD: the only person who edits `docs/CONTRACTS.md`, `glint/schemas.py`, `glint/config.py`, `pyproject.toml`.

Your output is one function other people rely on: `run_pipeline()` (CONTRACTS.md section 7), which never raises and always returns a `RunResult`.

**Start every agent session with:**
> Read AGENTS.md, docs/CONTRACTS.md, and the task brief for task <ID> in docs/PERSON_1_SYSTEM.md. Follow the HARD RULE. Work only in the files listed for this task. Finish with the reporting format from AGENTS.md.

---

## Task map

| ID | Time | Mode | Owns files | Depends on | Done when |
|---|---|---|---|---|---|
| P0 | 11:20 to 11:40 | single agent | pyproject.toml, .env.example, .gitignore, AGENTS.md (copy), glint/__init__.py, glint/config.py, glint/schemas.py, glint/gemini.py, scripts/smoke.py, scripts/e2e_smoke.sh, tests/test_schemas.py | nothing | smoke prints 3 model responses |
| A1 | 11:40 to 12:50 | parallel subagent | glint/media.py, glint/workspace.py, tests/test_media.py, tests/test_workspace.py | P0 | verify passes |
| A2 | 11:40 to 12:50 | parallel subagent | glint/perceive.py, glint/repro.py, tests/test_repro_classify.py | P0 (uses media + workspace by contract, integrate at CP1) | CP1 |
| B1 | 12:50 to 13:25 | parallel subagent | glint/patch.py, glint/gates.py, tests/test_gates_rules.py | A1, A2 merged | verify passes |
| B2 | 13:25 to 14:00 | single agent | glint/pipeline.py, tests/test_pipeline_never_raises.py | B1 merged | CP2 |
| C1 | 14:00 to 14:35 | single agent | any glint/ file except server.py (robustness fixes only) | B2, P2's garbage suite | garbage suite clean |
| D1 | 14:35 to 15:15 | single agent | scripts/make_live_bug.py | P2's eval/mutate.py | demo flow rehearsed |
| E1 | 15:15 to 15:45 | single agent | whichever glint/ file the chosen change needs | failure table | re-run shows v2 |

**Branches:** phases are stacked (CONTRACTS.md section 13). Phase 0: P0, V0. Phase 1: A tasks. Phase 2: B tasks. Phase 3: C tasks and freeze. Phase 4: D tasks. Phase 5: E and F tasks. Cut every task branch from the current phase branch, merge it back into that phase branch, run the smoke. As LEAD you do the fast-forward of `main` and create the next phase branch at each checkpoint.

You run at most 3 agents at once. A1 and A2 run in parallel. B1 runs alone while you review A1 and A2 merges.

---

## P0: Scaffold, contracts as code, model smoke test (11:20, single agent)

**Prompt:**
```
Task P0. Create the Glint scaffold exactly as described in docs/CONTRACTS.md.
1. pyproject.toml with the frozen dependency list from CONTRACTS.md section 2, package "glint", python >=3.12, ruff config line-length 110.
2. .env.example with the variables in section 3. .gitignore with runs/, .env, eval/results/* except eval/results/summary.json, __pycache__/, .venv/, live/.
3. glint/config.py and glint/schemas.py EXACTLY as written in CONTRACTS.md sections 4 and 5. Do not change a single name.
4. glint/gemini.py implementing every signature in CONTRACTS.md section 7 under "glint/gemini.py":
   - One google-genai client with http_options api_version "v1beta", key from GEMINI_API_KEY.
   - upload_video: client.aio.files.upload, then poll files.get until state is ACTIVE, 120 s timeout, raise on FAILED.
   - neon and hydrogen: generate_content on config.NEON / config.HYDROGEN. parts may be str, VideoRef, or Path to a PNG.
     When schema is given use response_mime_type application/json and response_schema, validate into the pydantic model,
     retry once on validation failure with the validation error appended to the prompt. Thinking level from the argument.
   - decide: client.decisions.create with model config.DECISION, questions of type config.DECISION_BOOL_TYPE,
     returns {key: p_yes}. If the SDK has no async variant, wrap it in asyncio.to_thread.
   - All calls: asyncio.Semaphore(config.MAX_CONCURRENCY), retries config.MODEL_RETRIES with exponential backoff and jitter
     on 429, 5xx, and timeouts. Record input and output tokens per call into a per-run accumulator (contextvar set by bind_run)
     and append a line to RUNS_DIR/<run_id>/usage.jsonl. Cost from config.PRICE.
5. scripts/smoke.py: --quick flag. Calls neon and hydrogen once each with a tiny generated PNG plus text, uploads a 3 second
   ffmpeg-generated test video and asks neon one question about it, and makes one decide() call on the image.
   Prints model id, latency, and the raw answer for each. Exit 1 on any failure.
6. scripts/e2e_smoke.sh exactly as in CONTRACTS.md section 11, skipping the pipeline line if the corpus file does not exist
   or glint/pipeline.py does not exist yet.
7. AGENTS.md is already in the repo root. Do not modify it.
8. tests/test_schemas.py: construct every schema with sample values and round-trip through JSON.
Verify: ruff check glint scripts && pytest -q tests && python scripts/smoke.py
```

**Verify by hand after the agent finishes:** the smoke output shows real answers from all three models. If the Decisions API rejects `DECISION_BOOL_TYPE`, look up the correct string in `/docs/decisions-api`, fix `config.py` in a `contract:` commit, rerun.

**Git:**
```
git checkout -b phase/0-scaffold main
git add -A
git commit -m "P0: scaffold, contracts as code, gemini wrappers, smoke test"
git push -u origin phase/0-scaffold
# open PR phase/0-scaffold -> main, then at the 11:40 checkpoint (after Person 2's V0 commit is on this branch):
git checkout main
git merge --ff-only phase/0-scaffold
git push origin main
git checkout -b phase/1-foundations phase/0-scaffold
git push -u origin phase/1-foundations
```

---

## A1: Media and workspace (11:40, parallel)

**Prompt:**
```
Task A1. Implement glint/media.py and glint/workspace.py to the exact signatures in CONTRACTS.md section 7.
media.normalize(src, dst):
  - ffprobe the input. Raise InputRejected with codes: UNREADABLE (ffprobe fails), NO_VIDEO_STREAM, TOO_SHORT (< MIN_VIDEO_S).
  - If longer than MAX_VIDEO_S keep the LAST MAX_VIDEO_S seconds and set trimmed=True.
  - Re-encode to mp4 h264, height NORMALIZED_HEIGHT keeping aspect (even width), 30 fps max, aac audio if present, yuv420p,
    -movflags +faststart. Timeout TIMEOUT_S["normalize"]. Return VideoInfo from a second ffprobe of the output.
  - Use asyncio.create_subprocess_exec, never shell=True.
media.grab_frame(video, t_s, out_png): single frame via ffmpeg -ss, clamp t to [0, duration].
workspace.make_workspace: shutil.copytree to a new tempfile.mkdtemp(prefix="glint_ws_").
workspace.apply_patch: enforce every rejection rule listed in CONTRACTS.md section 7 under workspace.py, using limits from
  config.py. Count changed lines with difflib (added + removed). Raise PatchRejected with a short reason. Write files only
  after all checks pass.
workspace.serve: find a free port in 8200 to 8999, run python -m http.server <port> --bind 127.0.0.1 --directory <ws>
  as a subprocess, wait until it answers HTTP 200 (max 5 s), yield the URL, kill the process on exit even on exceptions.
workspace.unified_diff: difflib unified diff over all changed files, app-relative paths.
Tests (no network): normalize rejects a 2 s clip, an audio-only file, random bytes; trims a 100 s clip to 90 s;
apply_patch rejects ../ paths, new files, test paths, 3 files, 41 changed lines; serve returns a URL that responds 200.
Generate test media with ffmpeg lavfi inside the tests (tmp_path). Do not commit media files.
Verify: pytest -q tests/test_media.py tests/test_workspace.py
```

---

## A2: Perceive and repro (11:40, parallel)

**Prompt:**
```
Task A2. Implement glint/perceive.py and glint/repro.py. Use glint.gemini, glint.media, glint.workspace only through the
signatures in CONTRACTS.md section 7 (media and workspace are being written in parallel; if they are not merged yet,
write your code against the signatures and test with the CLI once they land).

perceive.py:
  async def is_defect(video_ref) -> GateResult        # G0 via decide(video_block, {"g0": Q_G0}), threshold TH_G0_IS_DEFECT
  async def perceive(video_ref, app_dir) -> ReproSpec
    - Parts: the video, then every file in app_dir with extension .js .html .css as "=== path ===\n<content>", then the prompt.
    - Prompt asks Neon to watch the recording (and listen to any narration), and return a ReproSpec: concrete steps a test
      can perform (exact visible labels and texts typed), expected vs observed, evidence timestamps where the wrong behavior
      is visible with a short note each, suspect files (paths from the list given), confidence.
    - Drop suspect_files that do not exist. Clamp timestamps to the video duration.
repro.py:
  async def write_repro(spec, app_dir, feedback: str | None) -> str      # hydrogen, returns test file source
    - Prompt includes the CONTRACTS.md "Repro test file contract" rules verbatim, the spec, the app source, and feedback.
    - Strip markdown fences from the output. Reject output that does not define test_repro(page).
  async def run_test(test_path, base_url, out_dir) -> tuple[str, str, Path | None]   # (outcome, detail, screenshot)
    - Run the exact command from the contract. Before running, write a conftest.py next to the test that fixes the
      viewport at 1280x720 and injects CSS disabling animations and transitions.
    - Parse junit.xml into failed_assertion / failed_other / passed. detail = first 400 chars of the failure message.
    - Find the screenshot png under out_dir.
  async def establish_repro(video_ref, spec, app_dir, bundle_dir, emit) -> tuple[Path | None, list[GateResult]]
    - Up to REPRO_MAX_ATTEMPTS: write, serve app_dir via workspace.serve, run REPRO_FAIL_RUNS times; G1 passes only if all
      runs are failed_assertion. Otherwise feed back the outcome and detail and retry.
    - On G1 pass: G1B decide(video_block + image_block(repro_fail.png), {"g1b": Q_G1B}). If it fails, count it as a failed
      attempt with feedback "the failure does not show the same defect as the video".
    - Save the winning test to bundle_dir/repro_test.py and the screenshot to bundle_dir/shots/repro_fail.png.
    - Emit repro.attempt events and gate.result events per CONTRACTS.md section 9 when emit is given.
  CLI: python -m glint.repro --video <path> --app <dir> prints G0, the spec, each attempt, G1B, and the test path.
Tests (no network): junit parsing for the three outcomes using small hand-written junit.xml strings.
Verify (CP1): python -m glint.repro --video eval/corpus/todomvc/m001/video_r0.webm --app eval/corpus/todomvc/m001/app
```

If P2's corpus is not ready, use the hand-broken app and video from the 11:20 go/no-go test.

**CP1 at 12:50:** on one mutant the CLI prints G0 pass, a sensible spec, and a repro test that fails 3 of 3 with G1B pass. If not by 13:15, trigger the kill criterion in PLAN.md section 5.

---

## B1: Fix candidates and gates (12:50, parallel subagent while you review merges)

**Prompt:**
```
Task B1. Implement glint/patch.py and glint/gates.py.
patch.py:
  async def candidates(spec, app_dir, n) -> list[Candidate]
    - n hydrogen calls in parallel with temperatures spread over [0.2, 0.9], schema Patch, ids c1..cn, source "initial".
    - Prompt: the spec, the full app source with paths, rules: edit at most PATCH_MAX_FILES existing files, return FULL file
      contents, smallest change that makes the expected behavior true, never touch tests. Never include the repro test.
    - A call that fails or times out (TIMEOUT_S["candidate"]) yields a Candidate with patch None and reject_reason set.
  async def repair(spec, app_dir, failures: list[Candidate]) -> list[Candidate]
    - One neon call returning up to 2 patches (ids r1, r2, source "repair"). Feedback per failed candidate is ONLY the gate
      name and the assertion message. No diffs from other candidates, no original app.
gates.py:
  async def check_candidate(c, app_dir, repro_test, video_ref, bundle_dir) -> Candidate
    - make_workspace, apply_patch (PatchRejected -> reject), then in order, stopping at the first failure:
      G2: run the frozen repro test CANDIDATE_PASS_RUNS times, all must be passed. Save the last screenshot as shots/<id>_after.png.
      G3: open the patched app in Playwright, collect page errors and console errors during load and MONKEY_ACTIONS seeded
          random actions (click, type short text + Enter, toggle checkboxes, double click) on visible interactive elements.
          Any error -> fail. This gate must not import anything from eval/.
      G4: decide(video_block + image_block(<id>_after.png), {"g4": Q_G4}) >= TH_G4_DEFECT_GONE.
    - Write diffs/<id>.diff. Fill gates, survived, reject_reason, files_changed, lines_changed.
    - Whole check under TIMEOUT_S["gates_per_candidate"]; timeout -> reject with reason "timeout".
Tests (no network): G3 passes on the unmodified app and fails on a copy with a thrown error injected into the JS.
Verify: pytest -q tests/test_gates_rules.py
```

---

## B2: Pipeline integration (13:25, single agent, you watch it)

**Prompt:**
```
Task B2. Implement glint/pipeline.py exactly to CONTRACTS.md sections 7, 8, 9, 10.
- run_pipeline wires: media.normalize -> upload_video -> G0 -> perceive -> grab frames at evidence timestamps
  -> establish_repro -> candidates -> check_candidate for all in parallel -> repair if allowed and no survivors -> select.
- Every stage under its TIMEOUT_S and the global RUN_BUDGET_S (asyncio.timeout). Budget exhausted -> status by stage,
  reason "stopped at <stage>: time budget".
- Wrap everything: any exception -> internal_error True, status ABSTAIN before repro ready, NO_VERIFIED_FIX after,
  reason "stopped at <stage>: <ExceptionType>". Log the traceback, never put it in reason.
- Emit events in the exact order and payload shapes of section 9, seq from 1, also appended to events.jsonl.
- Write spec.json, result.json (last), and the bundle layout of section 8.
- reason is always one plain sentence a judge can read, e.g. "Verified fix c2 changed 3 lines in js/app.js".
- CLI: python -m glint.pipeline --video <p> --app <dir> --app-id <id> [--no-repair] [--expect-any]; prints status, reason,
  winner, latency, cost; exit 0 when --expect-any and a RunResult was produced.
Test (no network): monkeypatch glint.gemini functions to raise, assert run_pipeline returns a RunResult with
internal_error True and never raises.
Verify: python -m glint.pipeline --video eval/corpus/todomvc/m001/video_r0.webm --app eval/corpus/todomvc/m001/app --app-id todomvc
```

**CP2 at 14:00:** one VERIFIED_FIX from the CLI and from the browser (P2's UI), one clean video ends in ABSTAIN or NO_VERIFIED_FIX.

---

## C1: Robustness (14:00, single agent)

1. Run P2's garbage suite: `python -m eval.garbage --run`. It reports each case against CONTRACTS.md section 10.
2. Fix every failing case inside `glint/` only. No threshold changes except during the joint tuning slot.
3. Run it again. All cases must match. Then run `bash scripts/e2e_smoke.sh`.
4. Joint tuning with P2 on dev split only: adjust the three `TH_*` values in `config.py` to maximize precision when claiming fixed while keeping at least half of dev bugs fixed. One `contract: tune thresholds` commit.

---

## D1: Live demo bug script (14:35, single agent)

**Prompt:**
```
Task D1. scripts/make_live_bug.py --seed <int>: calls eval.mutate.make_one(apps/todomvc, seed, live/) (signature in
CONTRACTS.md section 7), serves live/app on port 8100 with python -m http.server until Ctrl-C, prints only
"Bug ready at http://localhost:8100 (seed <n>)". Writes live/meta.json but never prints the bug.
--reveal prints live/meta.json (used after the demo). If no visible mutant is found in 50 tries, try seed+1.
Verify: python scripts/make_live_bug.py --seed 42 then open http://localhost:8100.
```

Rehearse the full live flow at least twice: seed, record in Glint, verdict, reveal.

---

## E1: The one change (15:15, single agent)

P2 produces the failure table. Together pick the single change targeting the largest failure category. Implement it, then P2 re-runs Glint on the test split. Both v1 and v2 numbers are reported. No second change.

---

## Your rules recap

- `run_pipeline` never raises, never hangs past budget, never shows a stack trace.
- `glint/` never imports `eval/`.
- Thresholds only change in the 14:00 to 14:35 window, together with P2.
- After 14:35, no pipeline changes except E1.
