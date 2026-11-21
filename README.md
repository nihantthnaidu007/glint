# Glint

**Show Glint the bug. It tells you which fixes it can prove.**

**Demo video:** [1-minute walkthrough](docs/demo/glint_submission_1min.mp4)

Glint takes a screen recording of a bug in a web app, plus the app's source code, and works out what went
wrong from the video alone. It writes an automated browser test that fails on the buggy app, writes four
candidate fixes, and returns a fix only if it passes that test and a set of independent checks. If no fix
passes, it says so and hands back the failing test.

## Where and when this was built

Built at the Gemini 4 NYC Hackathon on Nov 21, 2026. All code in this repository was written that day; see
the [commit history](https://github.com/nihantthnaidu007/glint/commits/main).

`apps/todomvc` is third-party code (the TodoMVC plain JavaScript example, MIT license, see
`apps/todomvc/LICENSE` and `apps/todomvc/GLINT_NOTE.md`). It is used only as a test subject. We did not
write it.

## How it uses Gemini 4

| Model | Job in Glint |
|---|---|
| Gemini 4 Neon (`gemini-4-neon`) | Perceives the video: watches the recording, reads the whole app, and writes the bug report (steps, expected, observed, suspect files). |
| Gemini 4 Hydrogen (`gemini-4-hydrogen`) | Writes the failing browser test and 4 candidate fixes in parallel. |
| Gemini Decision 1 (`gemini-decision-1`) | Gives calibrated visual checks: does the failing screenshot match the video, and does the after screenshot show the expected behavior. |

The checks a fix has to pass:

| Check | What it tests |
|---|---|
| G1 | The generated test fails on the buggy app, by assertion, 3 runs out of 3. |
| G1B | Decision 1: the failing screenshot shows the same problem as the recording. |
| G2 | The same test passes on the patched app, 2 runs out of 2. |
| G3 | The patched app loads and survives 30 random actions with no page or console errors, and the patch stays within 2 files and 40 changed lines. |
| G4 | Decision 1: the after screenshot shows the expected behavior. |

## Results

Scored once on a test set that was frozen before the run. This table is copied from `RESULTS.md`.

| System | Bug runs | True-fix rate | Precision when claiming fixed | Wrong-fix rate | Abstain rate | False fixes on clean videos | Median latency (s) | Mean cost (USD) |
|---|---|---|---|---|---|---|---|---|
| Glint v1 | 21 | 66.7% (14/21) | 100.0% (14/14) | 0.0% (0/21) | 33.3% | 0 of 1 | 56.9 | 0.2358 |
| Glint v2 (derived from v1 runs) | 21 | 85.7% (18/21) | 94.7% (18/19) | 4.8% (1/21) | 9.5% | 0 of 1 | 56.9 | 0.2358 |
| Single Neon call | 21 | 95.2% (20/21) | 95.2% (20/21) | 4.8% (1/21) | 0.0% | 0 of 1 | 27.7 | 0.0907 |

"Derived" means the saved v1 Glint runs were recounted under the v2 selection rule from their saved
candidates and gate results, with no model called again; newly claimed fixes were scored by the same oracle.

Per-run records: eval/results/test_v1 and eval/results/test_v2_derived.

What these numbers say:

- **The single Neon call fixed more bugs**: 95.2%, against 66.7% for Glint v1 and 85.7% for Glint v2. The
  baseline is one Neon call that gets the video and the code and returns a fix with no checks.
- **Glint v1 was right on all 14 fixes it claimed.** Its 7 misses were refusals, not wrong fixes. In 5 of
  those 7 every candidate was the correct fix and the visual check (G4) rejected it.
- **v2 was chosen after seeing the v1 results.** It accepts a fix without the visual check when at least 3
  candidates that passed G2 and G3 are byte-identical. It raised the fix rate and introduced one wrong fix
  (m013, where three candidates agreed on the same wrong change). v1 is the scored run; v2 is a disclosed
  change made afterwards. Details are in `eval/CHANGES_AFTER_V1.md`.
- **3 of 6 pre-registered predictions missed.** We predicted the single call would ship a wrong fix at least
  10% of the time (it was 4.8%), that it would change code on every clean video (it changed none, on the one
  clean video scored), and that Glint would do at least 20 points worse on phone recordings (the rate was the
  same, on 3 phone videos). The predictions are in `eval/PREDICTIONS.md` and scored in `RESULTS.md`.
- **Glint costs about 2.6x more and takes about 2x longer** than the single call per bug ($0.24 and 57 s,
  against $0.09 and 28 s).

## How we tested

- **Bugs:** 28 machine-generated bugs. A seeded mutator changes one line of TodoMVC's JavaScript, and a bug
  is kept only if it changes what is on screen. Nobody wrote a bug by hand.
- **Exclusions:** 3 bugs were excluded before scoring because the bug is not visible in the recording. 4 of
  the 5 clean videos were excluded before scoring because they use a control where the unmodified app has a
  real quirk. Reasons are in `eval/corpus/EXCLUSIONS.md`.
- **Splits:** 7 dev bugs, used for tuning. 18 test bugs, scored once: 18 screen recordings and 3 phone
  recordings of a laptop screen.
- **Freeze:** corpus, thresholds, and predictions were committed before the scored run. The commit hash is
  in `eval/FROZEN.md`.
- **Hidden oracle:** a fix counts as a true fix only if 200 random click-and-type sequences behave the same
  on the patched app as on the original app. Glint never sees the original app or this check. A difference
  counts only if it reproduces 3 times out of 3.
- **Oracle controls:** before any fix was scored, the oracle itself was checked with no model involved: the
  original app must pass, every unpatched bug must fail, and every bug with its line restored must pass.
  37 of 37 controls passed.

## Limitations

- 18 mutants of one app (TodoMVC). Every bug is a single synthetic mutation, so these numbers say nothing
  about other apps or about bugs that span several files.
- TRUE_FIX means the differential fuzzer found no behavior difference from the unmutated app. That is strong
  evidence, not a proof of equivalence.
- A clean-video false fix counts every claimed fix on a video without a bug, including patches that do not
  change behavior.
- Latency is wall time measured while several runs share one machine and one API quota.
- With 18 mutants each run moves a rate by 6 points or more.
- Decision model calls are priced at $0 because the price is unknown, so Glint's cost is a lower bound.
- One small app. We did not test larger codebases.
- Single-line bugs only.
- No sandbox: generated tests and patched apps run on the local machine.
- Thresholds were tuned on 7 dev bugs.
- The visual check fails when a fix is not visible in the final screen. That is what caused most of v1's
  misses.
- Only 1 clean video and 3 phone videos were scored, so the clean-video and phone-video results rest on very
  little data.
- The v1 numbers were produced before a bug fix in check G3 (it could click links that leave the app). The
  bug shows once in the saved v1 runs and did not change that run's verdict. See `eval/CHANGES_AFTER_V1.md`.

## How to run

Requirements: Python 3.12 or newer, `ffmpeg` and `ffprobe` on PATH, and a Gemini API key.

Install:

```
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

Set the key:

```
cp .env.example .env
```

Then put your key after `GEMINI_API_KEY=` in `.env`.

Start the web UI and API on http://localhost:8000:

```
uvicorn glint.server:app --port 8000
```

Make a fresh bug to record against (served on http://localhost:8100; use any whole number as the seed):

```
python scripts/make_live_bug.py --seed 42
```

Run the pipeline on one video from the command line:

```
python -m glint.pipeline --video eval/corpus/todomvc/m001/video_r0.webm --app eval/corpus/todomvc/m001/app --app-id todomvc
```

Reproduce the evaluation (the scored run makes about 44 model-backed runs, then the oracle scores them):

```
python -m eval.run_eval --split test --systems glint,baseline --rungs r0,r3 --include-clean --tag v1
python -m eval.derive_v2 --split test
python -m eval.report
```

Run the checks:

```
ruff check glint eval scripts
pytest -q tests
```

## Where things are

| Path | What it holds |
|---|---|
| `glint/` | The product: pipeline, model wrappers, gates, server. It never imports from `eval/`. |
| `eval/` | Bug generator, recorder, hidden oracle, eval runner, report. |
| `web/` | The browser UI, plain HTML, CSS and JavaScript. |
| `RESULTS.md` | The full generated results, including the failure table and the predictions. |
| `docs/` | The build plan and the contracts both of us worked from. |
