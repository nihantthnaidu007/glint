# Glint: Main Build Plan

**Gemini 4 NYC Hackathon · Saturday, Nov 21, 2026 · team of 2**
Hacking opened 10:30 AM. **This plan starts at 11:20 AM** (late start, timeline already compressed). Submission due 5:00 PM.

Read order for both people: this file, then `docs/CONTRACTS.md`, then your own person file.

---

## 1. What Glint is

Glint turns a screen recording of a bug into a fix it has proven, or honestly says it could not.

1. **Watch.** Neon watches the video (and listens to narration) and works out what the user did, what should have happened, and what went wrong.
2. **Reproduce.** Hydrogen writes an automated browser test of those steps. The test is kept only if it actually fails on the buggy app, three times out of three.
3. **Fix.** Hydrogen writes four candidate fixes in parallel.
4. **Verify.** Each fix must make the test pass, break nothing else, stay small, and Decision 1 must judge from a screenshot that the bug in the video is gone.
5. **Verdict.** A fix that survives every check is returned with its evidence. If none survives, Glint says "no verified fix" and hands over the failing test. It never guesses.

**One line:** "Show Glint the bug. It proves the fix."

**Track:** Problem Statement One, 5 Sigma (reliability, proven against a baseline).

---

## 2. How Glint scores on each judging criterion

| Criterion (Round 1 weight) | What judges want | Glint's answer |
|---|---|---|
| Live Demo (35%) | Works live on unrehearsed input | A judge picks a random number, a fresh bug is generated from it, the judge records the bug themselves, Glint answers in about 90 seconds |
| Proof It Works (25%) | Baseline, honest failures, self-checks | 20 machine-generated bugs frozen before any scored run, plus clean videos with no bug. Glint vs a single Neon call. Headline: how often it is right when it says "fixed", and how often each system ships a wrong fix |
| Creativity (20%) | A problem others avoid, beyond text in a way that matters | Real bug reports arrive as screen recordings and voice notes. A text box cannot carry the timing, the clicks, or the visual state. Glint uses only the video |
| Gemini 4 and Decisions API (20%) | At least two models with clear jobs, plus Decisions API | Neon perceives (video, audio, whole app). Hydrogen generates (fast, parallel). Decision 1 judges (calibrated probabilities, multimodal, zero output tokens) |

Rule check: not a chatbot, not RAG, not Streamlit, not an image analyzer, the UI is not a dashboard, not health, education, or sports. Repo public, all code written today.

Prize focus: main prizes only. Skip Managed Agents and Shopify. Neither fits, both cost time.

---

## 3. Model facts (from AI Studio docs, checked this morning)

| Model | ID | Job | Facts |
|---|---|---|---|
| Gemini 4 Neon | `gemini-4-neon` | Perceiver, and the one repair round | Text, image, video, audio, PDF in. Text out. 1M context. Thinking low to xhigh. Structured output. $3 in / $15 out per 1M tokens |
| Gemini 4 Hydrogen | `gemini-4-hydrogen` | Generator: repro test and fix candidates | Same inputs, 1M context, speed focused, free tier exists. $1.50 / $7.50 |
| Gemini Decision 1 | `gemini-decision-1` | Gates G0, G1B, G4 | Calibrated yes/no, choice, and score answers. Up to 1,000 questions per call, one forward pass. Accepts video and images. Zero output tokens |

Not available on Gemini 4: Live API (no real-time streaming), image or audio generation. So Glint does not do real-time and does not generate media.

To verify in the P0 smoke test (unconfirmed): the exact yes/no type string in the Decisions API (docs summary said `noul`), the thinking-level parameter name, video upload and ACTIVE polling, rate limits on our key.

Cost estimate (unconfirmed): roughly $0.20 to $0.60 per full run. About 250 runs total today is likely under $100. Credits are expected to cover it.

---

## 4. The proof (eval) in one page

- **Target app:** TodoMVC vanilla JavaScript, vendored with its MIT license into `apps/todomvc/`. Stated on stage as a third-party test subject.
- **Bugs:** a seeded mutator changes one line of the app's JavaScript (flip a comparison, off by one, negate a boolean, swap `&&` and `||`, delete a statement, swap arguments, change a string). A bug is kept only if it causes visible wrong behavior. Nobody on the team writes bugs by hand.
- **Hidden oracle:** 200 random click and type sequences replayed on the fixed app and on the original, comparing what is on screen after every step. If they all match, it is a TRUE FIX. Glint never sees the original or this check.
- **Videos:** each bug's wrong behavior is recorded automatically (R0, clean 720p) and re-encoded small and blurry (R1, 480p). Five test bugs are also filmed on a phone pointed at the laptop screen (R3, messy real world). Five videos of the app working normally (clean, no bug) test whether a system invents fixes.
- **Splits:** 8 dev bugs (tuning only), 20 test bugs (scored once). Frozen at 14:35 with the commit hash recorded.
- **Systems:** Glint vs a single Neon call (video plus code in, fix out, no checks). "Glint without the repair round" is computed from Glint's own logs.
- **Metrics:** true-fix rate, precision when claiming fixed (headline), wrong-fix rate, abstain rate, false fixes on clean videos, latency, cost, share of fixes found by the repair round.
- **Predictions:** written in `eval/PREDICTIONS.md` and committed before the scored run. Misses get reported on stage.

**Late-start cut:** R1 videos are generated but not scored unless time allows. Scored rungs are R0 and R3.

Size: about 60 scored runs, roughly 12 to 20 minutes at 8 concurrent.

---

## 5. Timeline

| Time | Person 1 (system) | Person 2 (proof and demo) | Checkpoint |
|---|---|---|---|
| Now | Pre-flight checklist (section 7), 10 minutes max | Pre-flight checklist | Both laptops ready |
| 11:20 to 11:40 | **P0**: scaffold, config, schemas, gemini.py, smoke test, AGENTS.md, e2e_smoke.sh (one agent) | **Go/no-go test** by hand (section 6). Vendor TodoMVC, check license | Smoke prints three model responses |
| 11:40 | **GO or NO-GO decision together** | | |
| 11:40 to 12:50 | **Block A** parallel: A1 media + workspace, A2 perceive + repro | **Block A** parallel: A4 mutate + fuzz, A5 record + garbage, A6 UI shell on fixture events | **CP1 12:50**: one mutant video → spec → repro test failing 3/3. Corpus of 20+ kept bugs with R0 videos |
| 12:50 to 14:00 | **Block B**: B1 patch + gates (agent), then B2 pipeline integration (single agent) | **Block B** parallel: B3 server, B4 baselines + run_eval + report, UI wired to live SSE | **CP2 14:00**: one VERIFIED_FIX end to end from the browser, one clean video ends in ABSTAIN or NO_VERIFIED_FIX |
| 14:00 to 14:35 | **Block C**: robustness guards, garbage suite must pass (single agent). Tune thresholds with P2 on dev | **Block C**: dev eval run, record 5 phone videos, write PREDICTIONS.md | Garbage suite all clean, dev eval runs end to end |
| **14:35** | **FREEZE together**: corpus, thresholds, predictions committed. Hash into FROZEN.md | | FROZEN.md on main |
| 14:35 to 15:15 | No pipeline code changes. Build `make_live_bug.py`, rehearse the live demo flow | Scored test run (single agent), watch for crashes | results written |
| 15:15 to 15:45 | Implement the ONE change chosen from failure analysis | Failure table, pick the one change with P1, re-run Glint on test, report v1 and v2 | RESULTS.md with both versions |
| 15:45 to 16:20 | Record backup full run, rehearse demo twice with timer | UI polish, results panel, 1-minute submission video | Video exported |
| 16:20 to 16:45 | Final e2e smoke on demo laptop | README, repo public, submit, add teammate | **Submitted** |
| 16:45 to 17:00 | Buffer | Buffer | |

### Phases and branches

| Phase | Branch | Built from | Ends at |
|---|---|---|---|
| 0 Scaffold | `phase/0-scaffold` | `main` | 11:40 |
| 1 Foundations | `phase/1-foundations` | phase 0 | 12:50 (CP1) |
| 2 Pipeline | `phase/2-pipeline` | phase 1 | 14:00 (CP2) |
| 3 Hardening | `phase/3-hardening` | phase 2 | 14:35 (freeze) |
| 4 Proof | `phase/4-proof` | phase 3 | 15:15 |
| 5 Ship | `phase/5-ship` | phase 4 | 16:45 (submit) |

Full rules and commands: CONTRACTS.md section 13.

### Kill criteria (decided now, not debated later)

- **NO-GO at 11:40, or CP1 missed by 13:15:** drop fixing. Glint becomes "video in, verified failing repro test out". Same gates, same eval (repro validity vs single-call baseline), same UI with the fix lanes removed.
- **CP2 missed by 14:20:** drop the repair round, drop R1. Keep Glint vs single Neon call on R0 and R3.
- **Rate limits:** N_CANDIDATES 4 → 2, test bugs 20 → 12. Never drop the baseline.
- **Decisions API misbehaves:** G0, G1B, G4 fall back to Neon yes/no with structured output. Say so on stage.
- **UI not ready by 16:00:** demo from the plain stage feed. The pipeline matters more than polish.

---

## 6. Go/no-go test at 11:20 (Person 2, by hand, 15 minutes, no agents)

1. Open `apps/todomvc` in a browser. Change one line by hand, for example flip a `!==` in the toggle-completed logic.
2. Screen-record about 20 seconds of normal use until the bug shows. No narration.
3. In AI Studio, give `gemini-4-neon` the video plus the app's JavaScript files. Ask: the user's steps, expected behavior, observed behavior, suspect file and line.
4. **GO** if it names the right behavior and the right file. **NO-GO** if it is wrong on two attempts.
5. While there, run one Decisions API yes/no call on the video ("Does this show the app behaving incorrectly?") and note the exact request field names.

---

## 7. Pre-flight checklist (both, before starting, 10 minutes max)

- [ ] Gemini API key works. If credits are handed out at kickoff, swap the key into `.env`
- [ ] Python 3.12 venv: `pip install -U "google-genai>=3.2" pydantic fastapi "uvicorn[standard]" python-multipart playwright pytest pytest-playwright pytest-asyncio httpx ruff`
- [ ] `playwright install chromium` at home, not on venue Wi-Fi
- [ ] `ffmpeg -version` and `ffprobe -version` work
- [ ] TodoMVC vanilla JS example downloaded, MIT license confirmed
- [ ] One agent harness chosen for both people (Antigravity or Claude Code, not both)
- [ ] Superpowers installed: Antigravity `agy plugin install https://github.com/obra/superpowers`, or Claude Code `/plugin install superpowers@claude-plugins-official`. Run one tiny parallel subagent task to confirm worktrees work
- [ ] Phone hotspot ready, laptop chargers, phone stand for the R3 recordings
- [ ] Empty public GitHub repo `glint` created, both people have push access
- [ ] zsh pastes multi-line commands as one line: save scripts to files, type env vars one at a time

---

## 8. Team rules (both people, all day)

1. **CONTRACTS.md is law.** Routes, schemas, events, paths, and file ownership come from it. Changes only via its section 12 protocol.
2. **AGENTS.md is in every agent's context.** Start every agent session with: "Read AGENTS.md, docs/CONTRACTS.md, and your task brief in docs/PERSON_X file before anything else."
3. **Stay in your lane.** Person 1 never edits Person 2's files and the other way round. Need something from the other lane? Ask in chat.
4. **Stacked phase branches** (CONTRACTS.md section 13). Each phase has one branch built from the previous phase branch, with a PR to `main`. Task branches merge into the phase branch one at a time, with `bash scripts/e2e_smoke.sh` after each. Red means revert, not fix forward. At every checkpoint `main` is fast-forwarded to the green phase branch, so `main` always works and the last phase branch holds the complete code.
5. **Talk at every checkpoint** (11:40, 12:50, 14:00, 14:35, 15:15, 16:20) for at most 5 minutes: green or red, what is blocked, any contract change.
6. **No new scope after 14:00.** Only fixes, robustness, eval, and polish.
7. **Never present a replay as live.** Replay mode is labeled REPLAY on screen.
8. **Claim only measured numbers.** No "always works", "guaranteed", "self-improving", or "5 sigma" as a literal claim.

---

## 9. Live demo script (3 minutes)

| Seconds | Say and do |
|---|---|
| 0 to 20 | "Real bug reports are screen recordings and voice notes. AI coding tools are built for text tickets. Glint takes the video, and only returns a fix it can prove." |
| 20 to 35 | "Pick a number." Run `make_live_bug.py --seed <number>`. "Neither of us knows what is broken now." |
| 35 to 80 | The judge clicks around in the app tab while Glint records. They can talk. Stop. The stage rail starts: watching, understanding (timeline markers appear on the video), reproducing (failing screenshot), four fixes racing the checks |
| 80 to 120 | Verdict. Show the before/after slider, the small diff, each check's probability. If it abstained: "It refused to guess. Here is the failing test it wrote." |
| 120 to 170 | Results strip: "20 bugs frozen before we ran anything. A single Neon call ships a wrong fix X% of the time. When Glint says fixed, it is right Y% of the time. On clean videos with no bug, the baseline invented Z fixes, Glint invented W. Phone-camera video is where it still struggles." Real numbers only |
| 170 to 180 | "Neon sees, Hydrogen builds, Decision 1 judges. The model never grades its own work." |

Backups, in order: a second live attempt with a different seed, then a pre-recorded full run labeled as recorded, then REPLAY mode labeled as replay.

---

## 10. Q&A prep

| Question | Answer |
|---|---|
| The bugs are synthetic | They are machine-generated, not hand-written, frozen before any run, plus phone recordings and your live input. A real-world claim needs real bug reports. That is the next step and RESULTS.md says so |
| Why not compare to the original code? | Glint never sees the original. It only exists inside the hidden check, so Glint cannot learn the answer |
| Can it cheat by editing its own test? | The repro test is frozen before any fix is written, fixes cannot touch test files, and the real verdict comes from 200 hidden random sequences |
| Why the Decisions API instead of asking Neon? | Calibrated probabilities with zero output tokens, so thresholds mean something. We tuned them on 8 dev bugs only, then froze them |
| Why two models? | Neon for perception across video, audio, and the whole app. Hydrogen for fast parallel generation. Show the cost and latency columns |
| Bigger codebases? | Not shown today. Say what was measured, nothing more |
| What changed after you saw failures? | The one change from 15:15, with v1 and v2 numbers side by side |

---

## 11. Submission

- **1-minute video:** 0 to 10 s the problem in one sentence; 10 to 40 s a real run sped up with the checks visible; 40 to 55 s the results table and one honest failure; 55 to 60 s the repo URL. On screen: "Built Nov 21, 2026 at the Gemini 4 NYC Hackathon. TodoMVC is a third-party MIT test subject."
- **README:** what was built today with the commit timeline, how to run the demo and the eval, limitations first (synthetic bugs, one small app, no sandbox, thresholds tuned on 8 dev bugs, one run per configuration).
- **Check:** repo public, demo link works, both people added to the submission.
