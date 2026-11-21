# Glint: CONTRACTS (frozen interfaces)

This file is the single source of truth for everything that connects Person 1's work to Person 2's work.
Every agent reads this file before writing code. Nothing in here changes without the change protocol in section 12.

If code and this file disagree, this file wins and the code is wrong.

---

## 1. Repo layout and ownership

`P1` = Person 1 (system lane). `P2` = Person 2 (proof and demo lane). `LEAD` = Person 1, the only person who edits frozen files.

```
glint/                              repo root
├── AGENTS.md                       LEAD   rules every agent follows
├── README.md                       P2     written last (16:20)
├── pyproject.toml                  LEAD   frozen after P0
├── .env.example                    LEAD
├── .gitignore                      LEAD   includes runs/, .env, eval/results/, __pycache__/
├── docs/
│   ├── PLAN.md                     both read
│   ├── CONTRACTS.md                LEAD   this file
│   ├── PERSON_1_SYSTEM.md          P1
│   └── PERSON_2_PROOF_AND_DEMO.md  P2
├── glint/                          the product. MUST NEVER import from eval/
│   ├── __init__.py                 LEAD
│   ├── config.py                   LEAD   frozen after P0 (thresholds editable only during 14:00 to 14:35 tuning)
│   ├── schemas.py                  LEAD   frozen after P0
│   ├── gemini.py                   P1     model wrappers (P2 imports it for baselines)
│   ├── media.py                    P1     ffmpeg normalize, probe, frame grab
│   ├── workspace.py                P1     temp copy, apply patch, serve, diff (P2 imports it for scoring)
│   ├── perceive.py                 P1     G0 + Neon ReproSpec
│   ├── repro.py                    P1     Hydrogen repro test + G1 + G1b
│   ├── patch.py                    P1     Hydrogen candidates + Neon repair
│   ├── gates.py                    P1     G2, G3, G4
│   ├── pipeline.py                 P1     run_pipeline(), the only entry point
│   └── server.py                   P2     FastAPI app, routes in section 6
├── web/                            P2     index.html, styles.css, app.js, vendor/, fixtures/
├── apps/
│   └── todomvc/                    P2     vendored third-party target app with its LICENSE
├── eval/                           P2     may import glint/, never the other way
│   ├── mutate.py  fuzz.py  record.py  garbage.py  baselines.py  run_eval.py  report.py
│   ├── corpus/                     P2     frozen at 14:35
│   ├── results/                    P2     gitignored except summary.json
│   ├── PREDICTIONS.md              P2     frozen at 14:35
│   └── FROZEN.md                   P2     corpus hash, written at 14:35
├── scripts/
│   ├── smoke.py                    P1     calls all three models once
│   ├── e2e_smoke.sh                LEAD   the merge gate (section 11)
│   └── make_live_bug.py            P1     demo: seed -> fresh mutant served on port 8100
├── tests/                          each owner writes tests for their own modules
├── live/                           gitignored. Demo mutant written by make_live_bug.py (live/app, live/meta.json)
└── runs/                           gitignored. Evidence bundles written by the pipeline
```

**Import rules (hard):**
- `glint/*` never imports `eval/*` or `scripts/*`. The product must not be able to see the oracle.
- `eval/*` may import `glint.gemini`, `glint.workspace`, `glint.pipeline`, `glint.schemas`, `glint.config`.
- `web/` talks to the backend only through the HTTP routes in section 6.

---

## 2. Stack and dependencies (frozen)

Python 3.12. Async everywhere in `glint/`.

```
google-genai>=3.2
pydantic>=2.7
fastapi>=0.115
uvicorn[standard]>=0.30
python-multipart>=0.0.9
playwright>=1.47
pytest>=8
pytest-playwright>=0.5
pytest-asyncio>=0.23
httpx>=0.27
ruff>=0.6          # dev only
```

System tools: `ffmpeg` and `ffprobe` on PATH, Chromium via `playwright install chromium`.
No other dependency without the change protocol. No LangChain, no LangGraph, no databases, no Streamlit.

Frontend: plain HTML, CSS, JS. No framework, no build step. Any library goes into `web/vendor/` as a file. No CDN links (venue Wi-Fi).

---

## 3. Environment variables and ports

| Name | Default | Used by |
|---|---|---|
| `GEMINI_API_KEY` | none, required | glint/gemini.py |
| `GLINT_RUNS_DIR` | `runs` | pipeline, server |
| `GLINT_MAX_CONCURRENCY` | `4` | gemini.py semaphore |
| `GLINT_LOG_LEVEL` | `INFO` | all |

| Port | Purpose |
|---|---|
| 8000 | Glint server (API + web UI) |
| 8100 | Live demo bug app (scripts/make_live_bug.py) |
| any free port | Workspaces served during gates. `workspace.serve()` binds `127.0.0.1` port 0 in-process, so the OS picks the port |

---

## 4. config.py (write exactly this at P0)

```python
"""Glint configuration. Frozen after P0. Thresholds editable only during the 14:00 to 14:35 tuning window."""
from __future__ import annotations
import os

# Models
NEON = "gemini-4-neon"            # perceiver: video + audio + whole app -> ReproSpec; repair round
HYDROGEN = "gemini-4-hydrogen"    # generator: repro test, fix candidates (parallel)
DECISION = "gemini-decision-1"    # gate: calibrated yes/no on video and screenshots
DECISION_BOOL_TYPE = "noul"       # VERIFY at P0 against /docs/decisions-api. Fix here only.
DECISION_VIDEO_RESOLUTION = "high"  # video resolution for the Decision gates G0, G1B, G4

NEON_THINKING = "medium"
HYDROGEN_THINKING = "low"
REPAIR_THINKING = "low"           # Neon in the repair round: "medium" took 54 s, over TIMEOUT_S["repair"]

# Concurrency and budgets
MAX_CONCURRENCY = int(os.getenv("GLINT_MAX_CONCURRENCY", "4"))
N_CANDIDATES = 4
RUN_BUDGET_S = 150
TIMEOUT_S = {
    "normalize": 20, "g0": 15, "perceive": 45, "repro_attempt": 40,
    "candidate": 40, "gates_per_candidate": 45, "repair": 40,
}
MODEL_RETRIES = 3                 # retries on 429/5xx/timeouts, exponential backoff with jitter
SCHEMA_RETRIES = 1                # retries when output fails schema validation

# Input limits
MAX_UPLOAD_MB = 200
MIN_VIDEO_S = 3.0
MAX_VIDEO_S = 90.0                # longer videos keep the LAST 90 seconds
NORMALIZED_HEIGHT = 720
ACCEPTED_EXT = {".webm", ".mp4", ".mov", ".m4v", ".mkv"}

# Repro and patch rules
REPRO_MAX_ATTEMPTS = 3            # 1 try + 2 retries with error feedback
REPRO_FAIL_RUNS = 3               # repro must fail 3 of 3 times on the buggy app
CANDIDATE_PASS_RUNS = 2           # candidate must pass the repro 2 of 2 times
PATCH_MAX_FILES = 2
PATCH_MAX_CHANGED_LINES = 40
PATCH_ALLOWED_EXT = {".js", ".html"}
MONKEY_ACTIONS = 30               # G3 random actions on the patched app

# Gate thresholds (tuned on dev split only, then frozen at 14:35)
TH_G0_IS_DEFECT = 0.30            # abstain if p_yes < this
TH_G1B_SYMPTOM_MATCH = 0.60       # pass if p_yes >= this
TH_G4_DEFECT_GONE = 0.70          # pass if p_yes >= this

# Decision questions (wording frozen with thresholds)
Q_G0 = "Does this screen recording show a software application behaving incorrectly or not as a user would expect?"
Q_G1B = "The first input is a screen recording of a bug. The second input is a screenshot from an automated test. Does the screenshot show the same incorrect behavior as the recording?"
Q_G4 = "The first input is a screen recording of a bug. The second input is a screenshot of the same app after a fix, taken after the same user steps. Is the incorrect behavior from the recording absent in the screenshot, with the app otherwise working normally?"

# Cost estimates (USD per 1M tokens; Decision price unknown, set 0 and say so)
PRICE = {
    NEON: (3.00, 15.00),
    HYDROGEN: (1.50, 7.50),
    DECISION: (0.0, 0.0),
}

RUNS_DIR = os.getenv("GLINT_RUNS_DIR", "runs")
```

---

## 5. schemas.py (write exactly this at P0)

```python
"""Glint shared data contracts. Frozen after P0. Only LEAD edits, via the change protocol."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field

Status = Literal["VERIFIED_FIX", "NO_VERIFIED_FIX", "ABSTAIN", "REJECTED_INPUT"]
Stage = Literal["input", "g0", "perceive", "repro", "candidates", "repair", "select", "done"]
GateName = Literal["G0", "G1", "G1B", "G2", "G3", "G4"]


class VideoInfo(BaseModel):
    path: str                      # normalized mp4 path
    duration_s: float
    width: int
    height: int
    has_audio: bool
    trimmed: bool                  # True if the original was longer than MAX_VIDEO_S


class ReproSpec(BaseModel):
    summary: str                   # one line: what is wrong
    steps: list[str]               # concrete user actions in order
    expected: str
    observed: str
    evidence_timestamps_s: list[float] = Field(default_factory=list)
    evidence_notes: list[str] = Field(default_factory=list)   # same length as timestamps, shown as timeline markers
    suspect_files: list[str] = Field(default_factory=list)    # app-relative paths that exist
    confidence: float = Field(ge=0.0, le=1.0)


class GateResult(BaseModel):
    gate: GateName
    passed: bool
    p_yes: float | None = None     # Decision gates only
    detail: str = ""
    candidate_id: str | None = None


class Patch(BaseModel):
    files: dict[str, str]          # app-relative path -> FULL new file content
    rationale: str = ""


class Candidate(BaseModel):
    id: str                        # "c1".."c4", repair candidates "r1".."r2"
    source: Literal["initial", "repair"]
    patch: Patch | None = None
    lines_changed: int = 0
    files_changed: list[str] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)
    survived: bool = False
    reject_reason: str = ""


class Usage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class RunResult(BaseModel):
    run_id: str
    app_id: str
    status: Status
    reason: str                    # one human-readable sentence, always set
    stage_reached: Stage
    video: VideoInfo | None = None
    spec: ReproSpec | None = None
    repro_test: str | None = None  # bundle-relative path, e.g. "repro_test.py"
    candidates: list[Candidate] = Field(default_factory=list)
    winner_id: str | None = None
    gates: list[GateResult] = Field(default_factory=list)   # run-level gates G0, G1, G1B
    usage: Usage = Field(default_factory=Usage)
    latency_s: float = 0.0
    internal_error: bool = False   # True if an exception was caught and converted to a status
    created_at: datetime


EventType = Literal[
    "run.started", "input.normalized", "gate.result", "spec.ready",
    "repro.attempt", "repro.ready", "candidate.created", "candidate.final",
    "repair.started", "verdict", "run.finished", "log",
]


class Event(BaseModel):
    run_id: str
    seq: int                       # starts at 1, strictly increasing per run
    ts: datetime
    type: EventType
    stage: Stage
    data: dict[str, Any] = Field(default_factory=dict)
    replay: bool = False
```

---

## 6. HTTP API (server.py, P2 implements, web/ consumes)

Base URL `http://localhost:8000`. All JSON. Errors always use this shape:

```json
{ "error": { "code": "VIDEO_TOO_LARGE", "message": "Upload is 312 MB, limit is 200 MB." } }
```

| Method | Path | Request | Success | Errors |
|---|---|---|---|---|
| GET | `/api/health` | none | `200 {"status":"ok","models":{"neon":"gemini-4-neon","hydrogen":"gemini-4-hydrogen","decision":"gemini-decision-1"}}` | none |
| GET | `/api/apps` | none | `200 [{"id":"live","name":"Live demo bug","url":"http://localhost:8100"},{"id":"todomvc","name":"TodoMVC (original)","url":null}]`. Registry: `live` -> `live/app` (only if it exists), `todomvc` -> `apps/todomvc` | none |
| POST | `/api/runs` | multipart: `video` (file), `app_id` (string), `source` ("record" or "upload") | `202 {"run_id":"r_20261121_143012_a3f9"}` | `400 UNKNOWN_APP`, `400 BAD_EXTENSION`, `413 VIDEO_TOO_LARGE`, `429 RUN_IN_PROGRESS`, `503 PIPELINE_NOT_READY` |
| GET | `/api/runs/{run_id}` | none | `200 RunResult` (status may be absent while running: then `{"run_id":...,"running":true}`) | `404 RUN_NOT_FOUND` |
| GET | `/api/runs/{run_id}/events` | none | `200 text/event-stream`, one SSE message per Event | `404 RUN_NOT_FOUND` |
| GET | `/api/runs/{run_id}/files/{path}` | none | the file from the bundle (video, png, py, diff) | `404 FILE_NOT_FOUND`, `400 BAD_PATH` (any `..`) |
| POST | `/api/runs/{run_id}/replay` | none | `202 {"run_id":"<new id>"}`, re-streams the cached events with `replay:true` at original pacing | `404 RUN_NOT_FOUND` |
| GET | `/api/results` | none | `200` contents of `eval/results/summary.json` (section 9) or `{"available":false}` | none |
| GET | `/` | none | `web/index.html`, static mount for `/web/*` | none |

Server rules:
- One live run at a time (demo laptop). A second POST while one is running gets `429 RUN_IN_PROGRESS`.
- `POST /api/runs` saves the upload to `runs/<run_id>/input<ext>`, then starts `run_pipeline` as a background task and returns immediately.
- SSE format: `id: <seq>\nevent: <type>\ndata: <Event JSON>\n\n`. Send a `: ping` comment every 10 s. The stream closes after `run.finished`.
- A client connecting late first receives all past events from `runs/<run_id>/events.jsonl`, then live ones. No event is ever lost.
- The server never returns a 500 with a stack trace. Unhandled exceptions return `500 {"error":{"code":"INTERNAL","message":"..."}}` and are logged.

---

## 7. Python interfaces (function signatures are frozen)

### glint/pipeline.py (P1). The only entry point to the product.

```python
EmitFn = Callable[[Event], Awaitable[None]]

async def run_pipeline(
    video_path: Path,           # raw upload or corpus video
    app_dir: Path,              # the buggy app as the user has it
    app_id: str,
    run_id: str,
    emit: EmitFn | None = None, # server passes one; eval passes None
    *,
    allow_repair: bool = True,
) -> RunResult:
    """Never raises. Every failure becomes a RunResult with a status and a reason.
    Writes the evidence bundle to RUNS_DIR/<run_id>/ (section 8) and events.jsonl as it goes."""

def new_run_id() -> str:        # "r_YYYYMMDD_HHMMSS_<4 hex>"
```

### glint/gemini.py (P1). P2 imports it for baselines.

```python
class VideoRef(BaseModel): name: str; uri: str; mime_type: str

async def upload_video(path: Path) -> VideoRef            # waits until ACTIVE, timeout 120 s
async def neon(parts: list, schema: type[T] | None = None, thinking: str = NEON_THINKING) -> T | str
async def hydrogen(parts: list, schema: type[T] | None = None, thinking: str = HYDROGEN_THINKING, temperature: float | None = None) -> T | str
async def decide(input_blocks: list[dict], questions: dict[str, str]) -> dict[str, float]   # key -> p_yes
def image_block(png_path: Path) -> dict                     # Decision API image block (base64)
def video_block(ref: VideoRef, resolution: str = "low") -> dict   # Decision API video block (Files URI)
def usage_snapshot(run_id: str) -> Usage                    # tokens and cost accumulated for a run
def bind_run(run_id: str) -> None                           # contextvar so every call is attributed to a run
```

`parts` is a list of `str`, `VideoRef`, or `Path` to a PNG. gemini.py converts them to SDK parts. Every call: semaphore, retries per config, usage logged to `runs/<run_id>/usage.jsonl`.

### glint/workspace.py (P1). P2 imports it for scoring.

```python
class PatchRejected(Exception): ...

def make_workspace(app_dir: Path) -> Path                          # copy to a fresh temp dir
def apply_patch(ws: Path, patch: Patch) -> tuple[list[str], int]   # (files_changed, lines_changed); raises PatchRejected
@asynccontextmanager
async def serve(ws: Path) -> AsyncIterator[str]                    # yields "http://127.0.0.1:<port>", stops on exit
def unified_diff(app_dir: Path, ws: Path) -> str
```

`apply_patch` rejects: paths outside the app, absolute paths, `..`, files that do not already exist, extensions not in `PATCH_ALLOWED_EXT`, any path containing `test`, more than `PATCH_MAX_FILES` files, more than `PATCH_MAX_CHANGED_LINES` changed lines.

### glint/media.py (P1)

```python
class InputRejected(Exception): code: str; message: str
async def normalize(src: Path, dst: Path) -> VideoInfo    # mp4 h264 720p, aac if audio, last 90 s; raises InputRejected
async def grab_frame(video: Path, t_s: float, out_png: Path) -> Path
```

### eval/mutate.py and eval/fuzz.py (P2). P1's make_live_bug.py imports these.

```python
def generate_mutants(app_dir: Path, seed: int, n: int, out_root: Path) -> list[dict]
    # writes out_root/<mutant_id>/app/ and meta.json for each KEPT (visible) mutant, returns their meta dicts
def make_one(app_dir: Path, seed: int, out_dir: Path, max_tries: int = 50) -> dict | None
    # first visible mutant reachable from seed, written to out_dir/app/ + out_dir/meta.json

async def differential(original: Path, candidate: Path, n_seq: int, seed: int) -> dict | None
    # None if all n_seq sequences match; else {"sequence": [...actions], "step": int, "original": snap, "candidate": snap}
```

`meta.json` fields: `mutant_id, app_id, file, line, operator, original_snippet, mutated_snippet, seed, divergent_sequence, split` (`split` is "dev", "test", or null). meta.json is answer-key data: it is never passed to `run_pipeline` or to any model.

Corpus layout:

```
eval/corpus/todomvc/<mutant_id>/{app/, meta.json, video_r0.webm, video_r1.mp4, video_r3.mp4 (5 test mutants only)}
eval/corpus/todomvc/clean/<clean_id>/{video_r0.webm}      # original app, normal use, no bug
eval/corpus/garbage/<case_id>.<ext> + garbage/expected.json  # robustness inputs (section 10)
```

### Repro test file contract (generated by repro.py, run by repro.py, gates.py, eval)

- One file, `repro_test.py`, one function `def test_repro(page):` using pytest-playwright's sync `page` fixture.
- First line of the body: `page.goto(os.environ["BASE_URL"])`.
- Uses `expect(...)` assertions only. No `time.sleep`, no `wait_for_timeout` over 500 ms.
- Viewport is set by the runner, not the test.
- Run command (the only allowed one):
  `BASE_URL=<url> pytest -q -p no:cacheprovider <file> --browser chromium --screenshot on --output <dir> --junitxml <dir>/junit.xml`
- Classification from junit.xml: `failed_assertion` (AssertionError), `failed_other` (TimeoutError, locator error, syntax error, anything else), `passed`.

---

## 8. Evidence bundle layout (written by pipeline, read by server and eval)

```
runs/<run_id>/
├── input.<ext>                 original upload
├── video.mp4                   normalized
├── events.jsonl                one Event per line, in seq order
├── result.json                 RunResult, written last
├── spec.json                   ReproSpec
├── repro_test.py               frozen repro test
├── shots/repro_fail.png        screenshot from the failing repro on the buggy app
├── shots/<cid>_after.png       screenshot from the repro on candidate <cid>
├── diffs/<cid>.diff            unified diff per candidate
├── frames/t_<seconds>.png      frames at evidence timestamps (for UI markers)
└── usage.jsonl                 one line per model call
```

All paths inside `RunResult` and events are bundle-relative. The UI fetches them via `/api/runs/{id}/files/{path}`.

---

## 9. Event payloads (what each `data` contains)

| type | stage | data |
|---|---|---|
| `run.started` | input | `{"app_id": str}` |
| `input.normalized` | input | `VideoInfo` fields + `"video": "video.mp4"` |
| `gate.result` | any | `GateResult` fields |
| `spec.ready` | perceive | `ReproSpec` fields + `"frames": ["frames/t_4.2.png", ...]` |
| `repro.attempt` | repro | `{"attempt": int, "outcome": "failed_assertion"|"failed_other"|"passed", "detail": str}` |
| `repro.ready` | repro | `{"test_code": str, "screenshot": "shots/repro_fail.png"}` |
| `candidate.created` | candidates or repair | `{"candidate_id": str, "source": str, "files_changed": [str], "lines_changed": int}` |
| `candidate.final` | candidates or repair | `{"candidate_id": str, "survived": bool, "reject_reason": str, "diff": "diffs/c1.diff", "screenshot": "shots/c1_after.png" or null}` |
| `repair.started` | repair | `{"feedback_gates": [str]}` |
| `verdict` | select | `{"status": Status, "winner_id": str or null, "reason": str}` |
| `run.finished` | done | `{"status": Status, "latency_s": float, "cost_usd": float}` |
| `log` | any | `{"message": str}` (optional, UI may ignore) |

Gate events for candidates carry `candidate_id`. Run-level gates (G0, G1, G1B) have `candidate_id: null`.

**`eval/results/summary.json`** (P2 writes, `/api/results` serves, UI shows):

```json
{
  "available": true,
  "frozen_hash": "abc1234",
  "n_test_mutants": 20,
  "rows": [
    {"system": "Glint", "true_fix_rate": 0.0, "precision_when_claiming": 0.0, "wrong_fix_rate": 0.0, "abstain_rate": 0.0, "clean_video_false_fixes": 0, "median_latency_s": 0.0, "mean_cost_usd": 0.0},
    {"system": "Single Neon call", "true_fix_rate": 0.0, "precision_when_claiming": 0.0, "wrong_fix_rate": 0.0, "abstain_rate": 0.0, "clean_video_false_fixes": 0, "median_latency_s": 0.0, "mean_cost_usd": 0.0}
  ],
  "by_rung": [{"rung": "R0", "repro_valid_rate": 0.0, "true_fix_rate": 0.0}],
  "repair_share_of_fixes": 0.0
}
```

---

## 10. Pipeline behavior (P1 implements, everyone relies on it)

```
input:      normalize -> REJECTED_INPUT on InputRejected
g0:         decide(video at DECISION_VIDEO_RESOLUTION, Q_G0) -> ABSTAIN if p_yes < TH_G0_IS_DEFECT
perceive:   neon(video + all app source with paths) -> ReproSpec; drop suspect_files that do not exist
            grab frames at evidence timestamps
repro:      hydrogen -> repro_test.py; run; need failed_assertion REPRO_FAIL_RUNS times in a row
            failed_other or passed -> feed error back, retry up to REPRO_MAX_ATTEMPTS
            G1B decide(video + repro_fail.png + symptom text, Q_G1B) -> must pass
            after REPRO_MAX_ATTEMPTS without G1 and G1B -> ABSTAIN ("could not reproduce")
            repro_test.py is now FROZEN (hash recorded)
candidates: N_CANDIDATES hydrogen calls in parallel (varied temperature), each a Patch
            per candidate, own workspace: apply_patch (G-rules) -> G2 -> G3 -> G4, stop at first failure
repair:     only if allow_repair and zero survivors: one neon call with sanitized feedback
            (gate name + assertion message only), up to 2 candidates r1, r2, same gates
select:     survivors sorted by (lines_changed asc, G4 p_yes desc) -> winner
            no survivors -> NO_VERIFIED_FIX, repro_test.py still returned
budget:     RUN_BUDGET_S across the whole run; exhausted -> stop, NO_VERIFIED_FIX or ABSTAIN with reason "time budget"
errors:     any exception -> caught, internal_error=True, status by stage (before repro ready: ABSTAIN, after: NO_VERIFIED_FIX)
```

| Gate | Check | Passes when |
|---|---|---|
| G0 | Decision: is this a bug video | p_yes ≥ TH_G0_IS_DEFECT |
| G1 | Repro fails on the buggy app | failed_assertion 3 of 3 runs |
| G1B | Decision: failure screenshot matches the video symptom (inputs: video, screenshot, symptom text) | p_yes ≥ TH_G1B_SYMPTOM_MATCH |
| G2 | Repro passes on the patched app | passed 2 of 2 runs |
| G3 | Never worse | page loads, zero page errors and console errors during load and MONKEY_ACTIONS random actions, patch within file and line limits |
| G4 | Decision: defect gone in the after screenshot (inputs: video, screenshot, symptom text) | p_yes ≥ TH_G4_DEFECT_GONE |

**Decision gate inputs (contract change "decision gates", 12:45):**
- Every Decision gate passes the video with `video_block(ref, config.DECISION_VIDEO_RESOLUTION)` (`"high"`). `video_block` itself still defaults to `"low"`.
- G1B passes three input blocks in this order: the video, the screenshot (`image_block`), and a text block with the spec's observed behavior, built by `perceive.symptom_block(spec)`: `{"type": "text", "text": "Reported incorrect behavior in the recording: <spec.observed>"}`. The question wording (`Q_G1B`, `Q_G4`) is unchanged.
- Reason, measured on one recording: without the text block a screenshot of the buggy state scored 0.54 and a screenshot of the correct state 0.50. With it: 0.78 against 0.20. G0 scored 0.25 at `"low"` and 0.52 at `"high"`.

- G4 passes the same three blocks, but its text block carries the spec's observed AND expected behavior, built by `gates.outcome_block(spec)`: the symptom line above plus `Expected correct behavior after the same steps: <spec.expected>`. Measured on one recording with the symptom alone: fixed app 0.88, unrelated screenshots 0.81 to 0.89. With observed and expected: fixed 0.91, still buggy 0.19, unrelated 0.17 and 0.46.

**Patch transport (contract change, 13:10):** the Gemini Developer API rejects map-typed fields in a response schema, so models never return `Patch` directly. They return `patch.PatchWire` (`files: list[{path, content}]`, `rationale`), and the repair round returns `patch.RepairWire` (`patches: list[PatchWire]`). `patch.to_patch()` converts each to the shared `Patch` (path -> full content). `Patch` itself is unchanged.

**Candidate gates signature:** `gates.check_candidate(c, app_dir, repro_test, video_ref, bundle_dir, spec)` takes the `ReproSpec` as a sixth argument, because G4 needs its text. It never raises.

**Repair thinking:** the repair round calls Neon with `config.REPAIR_THINKING` (`"low"`). At `"medium"` the call took 54 s, over `TIMEOUT_S["repair"]`; at `"low"` 25 s.

**Stage progress callback:** `repro.establish_repro(video_ref, spec, app_dir, bundle_dir, emit)` takes `emit: Callable[[str, dict], Awaitable[None]] | None`, called as `await emit(event_type, data)` with the type and `data` payload from section 9. `pipeline.py` wraps it into `Event` objects, because only the pipeline owns `run_id` and `seq`. `perceive.perceive(video_ref, app_dir, duration_s=None)` clamps evidence timestamps when `duration_s` is given.

**Robustness contract (garbage suite, `eval/garbage.py` generates, Person 1 makes pass):**

| Case | Input | Required status |
|---|---|---|
| corrupt | random bytes renamed `.mp4` | REJECTED_INPUT |
| too_short | 2 second clip | REJECTED_INPUT |
| audio_only | `.m4a` renamed `.mp4`, no video stream | REJECTED_INPUT |
| black | 15 seconds of black frames | ABSTAIN |
| noise | 15 seconds of random noise | ABSTAIN |
| static_page | recording of a plain local page with text, no interaction | ABSTAIN |
| long | 3 minute recording, bug at the end | any status, within RUN_BUDGET_S + 10 s |
| portrait | 9:16 phone-shaped video of the app | any status, no crash |
| clean_use | original app used normally, no bug | ABSTAIN or NO_VERIFIED_FIX. VERIFIED_FIX here is a FAILURE |

Every case: `run_pipeline` returns (never raises), `internal_error` is False, `reason` is a readable sentence, total time ≤ RUN_BUDGET_S + 10 s.

What the generator models NEVER receive: the original unmutated app, eval/ anything, meta.json, the hidden fuzz oracle, other candidates' patches.

---

## 11. Merge gate: scripts/e2e_smoke.sh

Must pass on the current phase branch after every task merge, and on `main` after every phase merge (branch workflow in section 13). If red, revert the merge.

```bash
#!/usr/bin/env bash
set -euo pipefail
ruff check glint eval scripts
pytest -q tests
python scripts/smoke.py --quick
python -m glint.pipeline --video eval/corpus/todomvc/m001/video_r0.webm --app eval/corpus/todomvc/m001/app --app-id todomvc --expect-any
```

Before the corpus exists (before about 12:40), the last line is skipped automatically when the file is missing.
`--expect-any` means: exits 0 if a RunResult with any status is produced and nothing crashed.

---

## 12. Change protocol

1. Whoever needs a change posts in the team chat: `CONTRACT CHANGE: <what> <why>`.
2. Both people agree, out loud or in chat.
3. LEAD edits `docs/CONTRACTS.md` and the matching code (`schemas.py`, `config.py`, `pyproject.toml`) in ONE commit titled `contract: <what>`.
4. Everyone tells their active agents to pull main and re-read CONTRACTS.md.

No agent makes a contract change. An agent that needs one stops and reports.

---

## 13. Branch workflow (stacked phase branches)

Work is organized phase by phase. Each phase has one branch, built from the previous phase's branch, with a pull request pointing to `main`.

| Phase branch | Built from | Time | Tasks | Exit check |
|---|---|---|---|---|
| `phase/0-scaffold` | `main` | 11:20 to 11:40 | P0, V0 | smoke prints 3 model responses |
| `phase/1-foundations` | `phase/0-scaffold` | 11:40 to 12:50 | A1, A2, A4, A5, A6 | CP1 |
| `phase/2-pipeline` | `phase/1-foundations` | 12:50 to 14:00 | B1, B2, B3, B4 | CP2 |
| `phase/3-hardening` | `phase/2-pipeline` | 14:00 to 14:35 | C1, C2, FZ | garbage suite clean, freeze committed |
| `phase/4-proof` | `phase/3-hardening` | 14:35 to 15:15 | D1, D2 | scored run v1 written |
| `phase/5-ship` | `phase/4-proof` | 15:15 to 16:45 | E1, E2, F1 | submitted |

Rules:

1. **Task branches** are cut from the CURRENT phase branch, named `p1/<task>` or `p2/<task>`, one git worktree each. Example: `git worktree add ../glint-a1 -b p1/a1-media-workspace phase/1-foundations`.
2. **Task -> phase:** a human merges finished task branches into the phase branch one at a time (`git merge --no-ff`), then runs `bash scripts/e2e_smoke.sh` on the phase branch. Red means revert that merge. Never fix forward.
3. **Phase exit:** both people confirm the exit check is green. Push the phase branch and open its PR to `main`.
4. **Phase -> main at every checkpoint:** `git checkout main && git merge --ff-only phase/N-name && git push origin main`. Because each phase branch contains all earlier phases, this is always a fast-forward with no conflicts, and GitHub marks the PR merged. `main` therefore always holds the last green phase.
5. **Next phase:** `git checkout -b phase/N+1-name phase/N-name && git push -u origin phase/N+1-name`. Both people pull it before cutting task branches.
6. **Final merge:** `phase/5-ship` contains the complete code. Fast-forward `main` to it before submitting. Then verify on a fresh clone of `main` that `bash scripts/e2e_smoke.sh` passes.
7. Nobody commits directly to `main`. Nobody rebases or force-pushes a phase branch. No task branch lives across two phases: unfinished work is re-cut from the new phase branch or dropped.
8. Contract changes (section 12) are committed by LEAD directly on the current phase branch.
