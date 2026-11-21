# Glint: Person 2 (Proof and demo lane)

You own the proof and the face of the product: `eval/`, `web/`, `apps/`, `glint/server.py`, `README.md`, the submission video.

Your two outputs: a results table nobody can argue with, and a demo page that makes a working system look clear and calm.

**Start every agent session with:**
> Read AGENTS.md, docs/CONTRACTS.md, and the task brief for task <ID> in docs/PERSON_2_PROOF_AND_DEMO.md. Follow the HARD RULE. Work only in the files listed for this task. Finish with the reporting format from AGENTS.md.

---

## Task map

| ID | Time | Mode | Owns files | Depends on | Done when |
|---|---|---|---|---|---|
| V0 | 11:20 to 11:40 | you, by hand | apps/todomvc/ | nothing | go/no-go decided, app vendored |
| A4 | 11:40 to 12:50 | parallel subagent | eval/mutate.py, eval/fuzz.py, tests/test_fuzz.py, tests/test_mutate.py | apps/todomvc | 28+ kept mutants |
| A5 | 12:15 to 12:50 | parallel subagent (starts when A4's fuzz lands) | eval/record.py, eval/garbage.py | A4 | videos for every mutant, garbage inputs exist |
| A6 | 11:40 to 12:50 | parallel subagent | web/index.html, web/styles.css, web/app.js, web/vendor/, web/fixtures/ | CONTRACTS.md only | UI plays the fixture run |
| B3 | 12:50 to 13:25 | parallel subagent | glint/server.py, tests/test_server.py | P0 (schemas), pipeline by signature | routes pass tests |
| B4 | 12:50 to 14:00 | parallel subagent | eval/baselines.py, eval/run_eval.py, eval/report.py | P1's gemini.py, workspace.py | dev run completes |
| C2 | 14:00 to 14:35 | you + single agent | eval/PREDICTIONS.md, R3 phone videos, splits | B4 | dev numbers, predictions written |
| FZ | 14:35 | together | eval/FROZEN.md | everything | hash on main |
| D2 | 14:35 to 15:15 | single agent | eval/results/ | freeze | scored run done |
| E2 | 15:15 to 15:45 | single agent | RESULTS.md (generated), eval/results/summary.json | D2 | failure table, v1 and v2 |
| F1 | 15:45 to 16:45 | you + one agent | web/ polish, README.md, video | all | submitted |

---

**Branches:** phases are stacked (CONTRACTS.md section 13). Phase 0: P0, V0. Phase 1: A tasks. Phase 2: B tasks. Phase 3: C tasks and freeze. Phase 4: D tasks. Phase 5: E and F tasks. Cut every task branch from the current phase branch, merge it back into that phase branch, run the smoke. Person 1 fast-forwards `main` and creates the next phase branch at each checkpoint. Pull it before cutting new task branches. V0 is committed directly on `phase/0-scaffold`.

---

## V0: Go/no-go and vendoring (11:20, by hand)

1. Run the go/no-go test in PLAN.md section 6. Tell Person 1 the result at 11:40.
2. Put the TodoMVC vanilla JavaScript example into `apps/todomvc/` with its LICENSE file. It must open from `python -m http.server` with no build step and no network requests. If it loads anything from a CDN, download those files into the folder and fix the paths.
3. Add `apps/todomvc/GLINT_NOTE.md`: source URL, commit, license, "third-party test subject, not our work".

---

## A4: Bug generator and hidden oracle (11:40, parallel)

**Prompt:**
```
Task A4. Implement eval/mutate.py and eval/fuzz.py to the signatures in CONTRACTS.md section 7. No model calls. Deterministic.

fuzz.py (Playwright async, headless Chromium, viewport 1280x720, fresh context per sequence, inject CSS disabling
animations and transitions):
  generate_sequence(seed, length=12) -> list of actions. Each action is chosen at run time from the currently visible
    interactive elements (button, input, a, label, li, [role=button], [type=checkbox]) by seeded index:
    click, dblclick, fill a seeded short string then press Enter (text inputs), check/uncheck (checkboxes), press Escape.
    The sequence is defined by (seed, step index), so the same seed drives both apps identically.
  snapshot(page) -> dict: whitespace-collapsed body innerText, list of checkbox checked states in DOM order,
    counts of li / input / button elements, sorted localStorage items, document.title.
  run_sequence(app_dir, seed, length) -> list[snapshot] (one per step, plus the initial one). Serve the dir with a local
    http.server on a free port (do not import glint.workspace here; keep fuzz self-contained).
  differential(original, candidate, n_seq, seed) per the contract. Seeds are seed..seed+n_seq-1. Stop at first divergence.
  CLI: python -m eval.fuzz --selftest  -> asserts same seed twice gives identical snapshots on apps/todomvc, and
       differential(apps/todomvc, apps/todomvc, 20, 0) is None.

mutate.py:
  Mutation operators on .js files only, one mutation per mutant, sites chosen by seeded RNG, skipping comment lines:
    comparison flip (=== <-> !==, < <-> >=, > <-> <=), off-by-one on a numeric literal or index (+1 or -1),
    boolean negation (insert or remove !), && <-> ||, delete one single-line statement, swap first two call arguments,
    true <-> false, change a string literal used in a comparison or as an object key.
  A mutant is KEPT only if: the app still loads without a page error, AND differential(original, mutant, 50, seed) finds
    a divergence. Store that divergent sequence in meta.json. Mutant ids m001, m002, ... in generation order.
  generate_mutants and make_one per the contract.
  CLI: python -m eval.mutate --app apps/todomvc --seed 7 --n 28 --out eval/corpus/todomvc
       prints attempted, kept, and counts per operator. Then assign split: first 8 kept -> dev, next 20 -> test (in meta.json).
Tests: fuzz determinism; one hand-made mutant is detected; an identical copy is not.
Verify: python -m eval.fuzz --selftest && python -m eval.mutate --app apps/todomvc --seed 7 --n 28 --out eval/corpus/todomvc
```

Check by hand: open 3 random mutants in a browser. Each must show a wrong behavior a human could notice. If most mutants are invisible to a human (for example only localStorage differs), tell the agent to drop localStorage-only divergences from the KEEP rule.

---

## A5: Videos and garbage inputs (12:15, parallel)

**Prompt:**
```
Task A5. Implement eval/record.py and eval/garbage.py.
record.py:
  For each mutant in eval/corpus/<app>/: replay meta.divergent_sequence on the MUTANT with Playwright video recording,
  1280x720, slow_mo 450 ms, 1 s idle at start and 1.5 s idle at the end, a visible cursor (inject a small dot that follows
  mouse events) so clicks are visible. Save video_r0.webm. Then ffmpeg -> video_r1.mp4 (480p, crf 36, 15 fps).
  Clean videos: 5 recordings of the ORIGINAL app with seeded random sequences -> eval/corpus/<app>/clean/c01..c05/video_r0.webm.
  CLI: python -m eval.record --app todomvc [--only m001]
garbage.py:
  --make: create the inputs for every case in CONTRACTS.md section 10 "Robustness contract" under eval/corpus/garbage/
    using ffmpeg lavfi (black, noise, short), random bytes (corrupt), an m4a renamed (audio_only), a Playwright recording
    of a local static html page (static_page), a 3 minute recording built by looping clean use then a mutant's bug (long),
    a 720x1280 crop/pad of a mutant video (portrait), and a clean video (clean_use). Write expected.json mapping case -> allowed statuses.
  --run: run glint.pipeline.run_pipeline on each case (app = the matching mutant app or apps/todomvc), print a table
    case | status | allowed | internal_error | seconds | PASS/FAIL, exit 1 if any FAIL.
Verify: python -m eval.record --app todomvc --only m001 (open the video, the bug must be visible) ; python -m eval.garbage --make
```

`--run` needs Person 1's pipeline, so it is first run at 14:00.

---

## A6: The UI (11:40, parallel, built against a fixture so it does not wait for the pipeline)

### Design direction

Light, minimal, calm. It should look like a well-typeset document that happens to be alive.

| Token | Value |
|---|---|
| Page background | `#FAFAF7` warm off-white |
| Surface (cards) | `#FFFFFF`, 1 px border `#E7E5E0`, radius 12 px, no heavy shadows |
| Text | `#1F2328` |
| Muted text | `#6B7280` |
| Accent (the "glint") | `#E8A33D` for lines, fills, and the shimmer. Accent text uses `#8A5A0B` for contrast |
| Pass | `#1F8A4C` with a ✓ |
| Fail | `#B4451F` with a ✕ |
| Pending | `#B8BCC4` |
| Font | system stack: `ui-sans-serif, system-ui, -apple-system, "Segoe UI", Inter, sans-serif`. Code: `ui-monospace, "SF Mono", Menlo, monospace` |
| Sizes | body 16 px / 1.6, small 14 px, section titles 20 px semibold, verdict word 32 px. Numbers use `font-variant-numeric: tabular-nums` |
| Layout | single column, max width 1040 px, 24 px gaps, lots of white space |

Rules: no dark theme, no oversized text, no gradients except the one shimmer, no emoji, no charts library. Never rely on color alone: every pass or fail has an icon and a word. For the projector, use browser zoom 125% instead of bigger fonts.

### Page structure, top to bottom

1. **Header.** Wordmark "Glint" with a small amber dot, tagline "Show the bug. Get a proven fix." on the right, a quiet app selector and the health dot.
2. **Capture card.** Two actions: "Record screen" (primary) and "Upload video" (secondary, also drag and drop). While recording: a small pulsing amber dot, elapsed time, "Stop". After stop: a thumbnail and "Analyze".
3. **Stage rail** (sticky under the header once a run starts). Six steps on one thin line: Watch · Understand · Reproduce · Fix · Verify · Verdict. The current step has an amber dot. When a step completes, a thin amber shimmer sweeps along the line once to the next step. This is the one signature animation.
4. **Evidence card.** The video player with a timeline underneath. Neon's evidence timestamps appear as small amber ticks on the timeline. Hover shows the note, click seeks the video. Next to the video: Summary, Steps (numbered), Expected, Observed, in plain readable text. The "is this a bug" probability sits as a quiet line: "Bug detected · 0.94".
5. **Reproduction card.** Left: the failing screenshot. Right: "Fails 3 of 3" with ✕ marks, the symptom-match probability, and a collapsed "View test" disclosure with the code.
6. **Fix lanes card.** One slim row per candidate (c1 to c4, plus r1, r2 if repair runs). Each row: id, files and lines changed, then four checkpoints in a line: Test passes · Nothing breaks · Small change · Bug gone. Each checkpoint is a dot that turns into ✓ or ✕, with the probability next to "Bug gone". Rejected rows fade to muted with the reason in small text. The winner gets a thin amber left border.
7. **Verdict card.** One word in 32 px: "Verified" on a very light green wash, or "No verified fix" on a very light amber wash, or "Not a bug video" / "Could not read this video" in neutral. One sentence under it (`RunResult.reason`). The shimmer sweeps across the card once. Below: a before/after compare slider (failing screenshot vs after screenshot) and the diff with removed lines on light red, added on light green.
8. **Results strip** (bottom, always visible, small). A plain three-row table from `/api/results`: system, right when it says fixed, wrong fixes shipped, abstained. A single muted line: "20 bugs frozen before any scored run · commit abc1234". If results are not available, hide the strip.
9. **REPLAY badge.** If any event has `replay: true`, show a clear "REPLAY" pill in the header for the whole run.

Error states: every API error shows as one calm sentence in the capture card (the `message` from the error body). Never a blank page, never a raw JSON dump.

**Prompt:**
```
Task A6. Build web/index.html, web/styles.css, web/app.js following the "Design direction" and "Page structure" sections of
docs/PERSON_2_PROOF_AND_DEMO.md exactly. Plain HTML, CSS, JS. No framework, no build step, no CDN links.
Data comes only from the HTTP API in CONTRACTS.md section 6 and the event payloads in section 9.
1. First create web/fixtures/sample_run/ with a hand-written events.jsonl covering a full VERIFIED_FIX run (every event type,
   4 candidates, 1 winner, realistic gaps) and a second fixture for NO_VERIFIED_FIX and one for ABSTAIN, plus placeholder
   PNGs and a short generated video. Opening index.html?fixture=sample_run plays the fixture with timing, no server needed.
2. app.js: a single reducer applyEvent(state, event) that every event goes through (live SSE and fixture playback share it),
   and render functions per card. Unknown event types are ignored. Missing fields never throw.
3. Recording: navigator.mediaDevices.getDisplayMedia({video:true}) plus microphone via getUserMedia if granted, combined
   into one MediaStream, MediaRecorder webm, 90 s max with a visible countdown. Upload: file input and drag and drop.
4. POST /api/runs then open EventSource on /api/runs/{id}/events. On EventSource error, fall back to polling
   GET /api/runs/{id} every 2 s and render from the final RunResult.
5. Before/after slider: two stacked images and a draggable divider, pure CSS and 20 lines of JS. Keyboard accessible.
6. Diff view: parse unified diff text, render + and - lines with light backgrounds, monospace, no library.
7. Accessibility: 4.5:1 contrast for all text, focus rings, icons plus words for pass and fail, prefers-reduced-motion
   disables the shimmer.
Verify: python -m http.server 8000 --directory . then open /web/index.html?fixture=sample_run and the two other fixtures.
All three play to a verdict with no console errors.
```

Look at it yourself at 125% zoom from two meters away. If you cannot read the steps and the verdict, fix spacing and contrast, not font size.

---

## B3: Server (12:50, parallel)

**Prompt:**
```
Task B3. Implement glint/server.py: a FastAPI app with EXACTLY the routes, status codes, error shape, and server rules in
CONTRACTS.md section 6. Nothing more.
- POST /api/runs: validate app_id against the apps registry (apps/<id> and, if live/app exists, id "live" pointing to it),
  extension against config.ACCEPTED_EXT, size against MAX_UPLOAD_MB (stream to disk, stop early when over).
  Create the run id with pipeline.new_run_id(), save the upload, start pipeline.run_pipeline(...) as a background task with
  an emit function that pushes to an in-memory asyncio.Queue per run. One live run at a time.
- GET events: SSE. First replay every line already in events.jsonl, then stream live events, ping comment every 10 s,
  close after run.finished. Late subscribers lose nothing.
- GET files: serve only from inside runs/<run_id>/, reject any path with "..".
- POST replay: new run id, re-emit the cached events with replay=true keeping original time gaps (capped at 3 s each).
- GET /api/results: eval/results/summary.json or {"available": false}.
- GET /: web/index.html; mount /web as static.
- A global exception handler returning the error shape with code INTERNAL. No stack traces to clients.
- Import run_pipeline lazily inside the route so the server can start before the pipeline exists; if missing, POST returns
  503 {"error":{"code":"PIPELINE_NOT_READY",...}}.
Tests with httpx.AsyncClient and a monkeypatched fake run_pipeline that emits the fixture events: every route, every
error code, late-subscriber SSE, path traversal rejected.
Verify: pytest -q tests/test_server.py && uvicorn glint.server:app --port 8000 (then open http://localhost:8000)
```

After Person 1's pipeline merges (about 13:45), record a bug in the browser and watch a real run. That is **CP2**.

---

## B4: Baseline, eval runner, report (12:50, parallel)

**Prompt:**
```
Task B4. Implement eval/baselines.py, eval/run_eval.py, eval/report.py.
baselines.py:
  async def single_call(video_path, app_dir, model="neon") -> Patch | None
    One model call (glint.gemini.neon): the video plus the full app source with paths, asked for a fix as full-file
    replacements (schema Patch). No repro test, no gates, no retries beyond the wrapper's own. This is the baseline.
run_eval.py:
  --split dev|test  --systems glint,baseline  --rungs r0,r3  --include-clean  --concurrency 8
  For each mutant in the split and each rung that has a video, and each clean video when --include-clean:
    glint:    result = await run_pipeline(video, mutant_app, "todomvc", run_id, None); patch = winner's patch if VERIFIED_FIX.
    baseline: patch = await single_call(video, mutant_app).
    Score with the HIDDEN oracle, which only this file calls:
      no patch -> oracle "NO_PATCH"
      patch -> make_workspace(mutant_app), apply_patch (rejected -> "WRONG_FIX"), then
               eval.fuzz.differential(apps/todomvc, ws, 200, seed=10_000) -> None means "TRUE_FIX", else "WRONG_FIX".
      clean videos: app is apps/todomvc itself; any applied patch that changes behavior is "WRONG_FIX", no patch is correct.
    For glint also record repro_valid: the frozen repro test fails on the mutant and passes on apps/todomvc (run it here).
    Record: mutant_id, rung, system, status, claimed_fix, oracle, repro_valid, fixed_in_repair (winner source == "repair"),
    latency_s, cost_usd, tokens, run_id. Write eval/results/<split>/<system>/<mutant>_<rung>.json.
    Resumable: skip records that exist unless --force. Never pass meta.json or apps/todomvc to run_pipeline for mutants.
  --tag v1|v2 writes under eval/results/<split>_<tag>/ so the post-change rerun does not overwrite the first run.
report.py:
  Reads results, writes RESULTS.md and eval/results/summary.json (shape in CONTRACTS.md section 9).
  RESULTS.md sections: frozen corpus hash and config (models, thresholds, N); main table (per system: true-fix rate,
  precision when claiming fixed, wrong-fix rate, abstain rate, false fixes on clean videos, median latency, mean cost);
  by-rung table for Glint (repro valid rate, true-fix rate for R0, R1, R3); repair share of fixes and "Glint without repair"
  derived numbers; failure table (mutant, rung, first failing gate or stage, category); predictions HIT or MISS;
  v1 vs v2 if both tags exist; limitations.
  Definitions: true-fix rate = TRUE_FIX / all bug runs. precision when claiming = TRUE_FIX / runs with claimed_fix.
  wrong-fix rate = WRONG_FIX / all bug runs. abstain rate = runs without a claimed fix / all bug runs.
Verify: python -m eval.run_eval --split dev --systems glint,baseline --rungs r0 && python -m eval.report && cat RESULTS.md
```

---

## C2: Dev run, phone videos, predictions (14:00)

1. Run dev: `python -m eval.run_eval --split dev --systems glint,baseline --rungs r0 --include-clean`.
2. With Person 1, tune the three thresholds on dev only (PLAN.md timeline). One commit.
3. Record **R3**: pick 5 test mutants, open each on the laptop, film the screen with your phone while performing the bug by hand, talk through it on 2 of the 5. Save as `video_r3.mp4` in each mutant folder.
4. Run the garbage suite with Person 1: `python -m eval.garbage --run`.
5. Write `eval/PREDICTIONS.md` with your own numbers:
   - Glint precision when claiming fixed will be at least ___%.
   - Glint true-fix rate will beat the single call by at least ___ points.
   - The single call's wrong-fix rate will be at least ___%.
   - The single call will invent a fix on at least ___ of 5 clean videos. Glint on at most ___.
   - The repair round will produce fewer than ___% of Glint's fixes.
   - Repro validity will drop by at least ___ points from R0 to R3.

## FZ: Freeze (14:35, together)

```
git add eval/corpus eval/PREDICTIONS.md glint/config.py
git commit -m "freeze: corpus, thresholds, predictions"
git rev-parse --short HEAD
```
Do this on `phase/3-hardening`. Write that hash, the time, and the mutant counts into `eval/FROZEN.md`, commit, push. From now on nobody edits the corpus, thresholds, or predictions.

## D2: Scored run (14:35, single agent)

```
python -m eval.run_eval --split test --systems glint,baseline --rungs r0,r3 --include-clean --tag v1
python -m eval.report
```
Watch for crashes and rate limits. If rate limited, lower `--concurrency`, do not shrink the test set unless the kill criterion says so.

## E2: Failure analysis and the one change (15:15, single agent)

**Prompt:**
```
Task E2. Read eval/results/test_v1. For every Glint bug run that is not TRUE_FIX, classify the cause from the run bundle:
G0 false abstain, wrong perception, repro never failed for the right reason, G1B false reject, no candidate passed G2,
G3 false reject, G4 false reject, wrong fix shipped, timeout, internal error. Output a table with counts and two example
run ids per category. Propose exactly one change for the largest category. Do not implement anything.
```
Agree on the change with Person 1. They implement it. Then:
```
python -m eval.run_eval --split test --systems glint --rungs r0,r3 --include-clean --tag v2
python -m eval.report
```
RESULTS.md shows v1 and v2 side by side with the change described in one sentence.

## F1: Polish and submit (15:45)

- UI: fix anything that looked wrong in real runs, check all three verdict states, check the results strip with real numbers, check 125% zoom.
- 1-minute video per PLAN.md section 11.
- README per PLAN.md section 11.
- Repo public, submission form, teammate added. **Submit by 16:45.**

---

## Your rules recap

- Only `eval/run_eval.py` ever touches the hidden oracle. Nothing in `glint/` or `web/` knows it exists.
- The test split is scored once before any change (v1) and once after the single agreed change (v2). Nothing else.
- Every number on screen comes from `eval/results/summary.json`. Never type a number by hand into the UI.
- The UI talks only to the routes in CONTRACTS.md section 6.
