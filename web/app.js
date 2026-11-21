"use strict";

(() => {
  const MAX_RECORD_S = 90;
  const MAX_UPLOAD_BYTES = 200 * 1024 * 1024;
  const ACCEPTED_EXT = [".webm", ".mp4", ".mov", ".m4v", ".mkv"];
  const STAGE_ORDER = ["watch", "understand", "reproduce", "fix", "verify", "verdict"];

  const dom = {
    appSelect: document.getElementById("app-select"),
    healthDot: document.getElementById("health-dot"),
    healthLabel: document.getElementById("health-label"),
    replayPill: document.getElementById("replay-pill"),
    dropZone: document.getElementById("drop-zone"),
    idleActions: document.getElementById("idle-actions"),
    recordBtn: document.getElementById("record-btn"),
    uploadBtn: document.getElementById("upload-btn"),
    fileInput: document.getElementById("file-input"),
    recordingControls: document.getElementById("recording-controls"),
    recordTimer: document.getElementById("record-timer"),
    stopBtn: document.getElementById("stop-btn"),
    previewControls: document.getElementById("preview-controls"),
    previewVideo: document.getElementById("preview-video"),
    previewName: document.getElementById("preview-name"),
    previewDetail: document.getElementById("preview-detail"),
    clearBtn: document.getElementById("clear-btn"),
    analyzeBtn: document.getElementById("analyze-btn"),
    runningNote: document.getElementById("running-note"),
    runningText: document.getElementById("running-text"),
    runningId: document.getElementById("running-id"),
    captureError: document.getElementById("capture-error"),

    stageRail: document.getElementById("stage-rail"),
    stageSteps: document.getElementById("stage-steps"),
    stageShimmer: document.getElementById("stage-shimmer"),

    evidenceCard: document.getElementById("evidence-card"),
    evidenceVideo: document.getElementById("evidence-video"),
    evidenceTimeline: document.getElementById("evidence-timeline"),
    timelineProgress: document.getElementById("timeline-progress"),
    evidenceTicks: document.getElementById("evidence-ticks"),
    timelineTooltip: document.getElementById("timeline-tooltip"),
    timelineCurrent: document.getElementById("timeline-current"),
    timelineDuration: document.getElementById("timeline-duration"),
    g0Line: document.getElementById("g0-line"),
    specPending: document.getElementById("spec-pending"),
    specContent: document.getElementById("spec-content"),
    specSummary: document.getElementById("spec-summary"),
    specSteps: document.getElementById("spec-steps"),
    specExpected: document.getElementById("spec-expected"),
    specObserved: document.getElementById("spec-observed"),

    reproCard: document.getElementById("repro-card"),
    reproShotPlaceholder: document.getElementById("repro-shot-placeholder"),
    reproScreenshot: document.getElementById("repro-screenshot"),
    reproStatusBlock: document.getElementById("repro-status-block"),
    g1bLine: document.getElementById("g1b-line"),
    reproAttemptsList: document.getElementById("repro-attempts-list"),
    reproTestDetails: document.getElementById("repro-test-details"),
    reproTestCode: document.getElementById("repro-test-code"),

    lanesCard: document.getElementById("lanes-card"),
    lanesSubtitle: document.getElementById("lanes-subtitle"),
    lanesList: document.getElementById("lanes-list"),

    verdictCard: document.getElementById("verdict-card"),
    verdictBanner: document.getElementById("verdict-banner"),
    verdictIcon: document.getElementById("verdict-icon"),
    verdictWord: document.getElementById("verdict-word"),
    verdictReason: document.getElementById("verdict-reason"),
    verdictMeta: document.getElementById("verdict-meta"),
    replayBtn: document.getElementById("replay-btn"),
    verdictShimmer: document.getElementById("verdict-shimmer"),
    verdictProof: document.getElementById("verdict-proof"),
    compareSection: document.getElementById("compare-section"),
    compareBox: document.getElementById("compare-box"),
    compareBefore: document.getElementById("compare-before"),
    compareAfterWrap: document.getElementById("compare-after-wrap"),
    compareAfter: document.getElementById("compare-after"),
    compareDivider: document.getElementById("compare-divider"),
    diffSection: document.getElementById("diff-section"),
    diffFileLabel: document.getElementById("diff-file-label"),
    diffViewer: document.getElementById("diff-viewer"),

    resultsStrip: document.getElementById("results-strip"),
    resultsTbody: document.getElementById("results-tbody"),
    resultsFootnote: document.getElementById("results-footnote"),
  };

  let state = createInitialState();
  let pendingFile = null;
  let pendingObjectUrl = null;
  let mediaRecorder = null;
  let recordedChunks = [];
  let activeStreams = [];
  let recordInterval = null;
  let recordStartMs = 0;
  let eventSource = null;
  let pollTimer = null;
  let fixtureTimers = [];
  let diffCache = new Map();
  let lastCompletedStepIndex = -1;

  function createInitialState(opts = {}) {
    return {
      mode: opts.fixture ? "fixture" : "live",
      fixtureName: opts.fixture || null,
      runId: null,
      appId: null,
      started: false,
      running: false,
      finished: false,
      isReplay: false,
      stageReached: null,
      stepState: {
        watch: "pending",
        understand: "pending",
        reproduce: "pending",
        fix: "pending",
        verify: "pending",
        verdict: "pending",
      },
      video: null,
      videoSrc: null,
      spec: null,
      gates: {},
      reproAttempts: [],
      repro: {
        testCode: "",
        testPath: null,
        screenshot: null,
      },
      candidates: [],
      candidateMap: {},
      repairStarted: false,
      winnerId: null,
      verdict: null,
      latencyS: null,
      costUsd: null,
      diffText: null,
      diffPath: null,
      verdictAnimated: false,
    };
  }

  function assetUrl(stateObj, relPath) {
    if (!relPath) return "";
    const clean = String(relPath).replace(/^\/+/, "");
    if (stateObj.mode === "fixture" && stateObj.fixtureName) {
      return `/web/fixtures/${encodeURIComponent(stateObj.fixtureName)}/${clean}`;
    }
    if (stateObj.runId) {
      return `/api/runs/${encodeURIComponent(stateObj.runId)}/files/${clean}`;
    }
    return "";
  }

  function ensureCandidate(next, candidateId, source = "initial") {
    if (!candidateId) return null;
    if (!next.candidateMap[candidateId]) {
      const cand = {
        id: candidateId,
        source: source === "repair" ? "repair" : "initial",
        filesChanged: [],
        linesChanged: 0,
        gates: {},
        survived: null,
        rejectReason: "",
        diff: null,
        screenshot: null,
      };
      next.candidateMap[candidateId] = cand;
      next.candidates.push(cand);
    }
    return next.candidateMap[candidateId];
  }

  function setActiveStep(stepState, activeKey) {
    const idx = STAGE_ORDER.indexOf(activeKey);
    for (let i = 0; i < STAGE_ORDER.length; i += 1) {
      const key = STAGE_ORDER[i];
      if (i < idx) {
        if (stepState[key] !== "fail") stepState[key] = "done";
      } else if (i === idx) {
        if (stepState[key] !== "done" && stepState[key] !== "fail") {
          stepState[key] = "active";
        }
      }
    }
  }

  /**
   * Pure-ish reducer over state + event.
   * Shared by live SSE and fixture playback. Never throws on unknown events or missing fields.
   */
  function applyEvent(prevState, event) {
    if (!event || typeof event !== "object") return prevState;
    const type = typeof event.type === "string" ? event.type : "";
    const data = event.data && typeof event.data === "object" ? event.data : {};
    const stage = typeof event.stage === "string" ? event.stage : prevState.stageReached;

    const next = prevState;
    if (event.run_id && !next.runId) {
      next.runId = String(event.run_id);
    }
    if (event.replay === true) {
      next.isReplay = true;
    }
    if (stage) {
      next.stageReached = stage;
    }

    switch (type) {
      case "run.started": {
        next.started = true;
        next.running = true;
        next.appId = data.app_id || next.appId;
        setActiveStep(next.stepState, "watch");
        break;
      }

      case "input.normalized": {
        next.started = true;
        next.video = {
          path: data.video || data.path || "video.mp4",
          duration_s: Number(data.duration_s) || 0,
          width: Number(data.width) || 0,
          height: Number(data.height) || 0,
          has_audio: Boolean(data.has_audio),
          trimmed: Boolean(data.trimmed),
        };
        next.videoSrc = assetUrl(next, next.video.path);
        setActiveStep(next.stepState, "watch");
        break;
      }

      case "gate.result": {
        const gateName = data.gate;
        if (!gateName) break;
        const gateObj = {
          gate: gateName,
          passed: Boolean(data.passed),
          p_yes: typeof data.p_yes === "number" ? data.p_yes : null,
          detail: typeof data.detail === "string" ? data.detail : "",
          candidate_id: data.candidate_id || null,
        };

        if (gateObj.candidate_id) {
          const cand = ensureCandidate(next, gateObj.candidate_id);
          if (cand) {
            cand.gates[gateName] = gateObj;
          }
          setActiveStep(next.stepState, "verify");
        } else {
          next.gates[gateName] = gateObj;
          if (gateName === "G0") {
            // G0 is advisory (CONTRACTS.md section 10, "tune gates on dev"): never blocks the rail.
            next.stepState.watch = "done";
            setActiveStep(next.stepState, "understand");
          } else if (gateName === "G1") {
            if (!gateObj.passed) {
              next.stepState.reproduce = "fail";
            }
          } else if (gateName === "G1B") {
            if (gateObj.passed) {
              next.stepState.reproduce = "done";
            } else {
              next.stepState.reproduce = "fail";
            }
          }
        }
        break;
      }

      case "spec.ready": {
        next.spec = {
          summary: typeof data.summary === "string" ? data.summary : "",
          steps: Array.isArray(data.steps) ? data.steps.map(String) : [],
          expected: typeof data.expected === "string" ? data.expected : "",
          observed: typeof data.observed === "string" ? data.observed : "",
          evidence_timestamps_s: Array.isArray(data.evidence_timestamps_s)
            ? data.evidence_timestamps_s.map(Number).filter((n) => Number.isFinite(n))
            : [],
          evidence_notes: Array.isArray(data.evidence_notes) ? data.evidence_notes.map(String) : [],
          suspect_files: Array.isArray(data.suspect_files) ? data.suspect_files.map(String) : [],
          confidence: typeof data.confidence === "number" ? data.confidence : null,
          frames: Array.isArray(data.frames) ? data.frames.map(String) : [],
        };
        next.stepState.watch = "done";
        next.stepState.understand = "done";
        setActiveStep(next.stepState, "reproduce");
        break;
      }

      case "repro.attempt": {
        next.reproAttempts.push({
          attempt: Number(data.attempt) || next.reproAttempts.length + 1,
          outcome: typeof data.outcome === "string" ? data.outcome : "failed_other",
          detail: typeof data.detail === "string" ? data.detail : "",
        });
        setActiveStep(next.stepState, "reproduce");
        break;
      }

      case "repro.ready": {
        next.repro.testCode = typeof data.test_code === "string" ? data.test_code : next.repro.testCode;
        next.repro.screenshot = typeof data.screenshot === "string" ? data.screenshot : next.repro.screenshot;
        // Real runs send repro.ready without test_code; the frozen test lives at the bundle root.
        if (!next.repro.testCode) next.repro.testPath = "repro_test.py";
        next.stepState.reproduce = "done";
        setActiveStep(next.stepState, "fix");
        break;
      }

      case "candidate.created": {
        const cand = ensureCandidate(next, data.candidate_id, data.source);
        if (cand) {
          if (data.source) cand.source = data.source === "repair" ? "repair" : "initial";
          if (Array.isArray(data.files_changed)) cand.filesChanged = data.files_changed.map(String);
          if (typeof data.lines_changed === "number") cand.linesChanged = data.lines_changed;
        }
        if (next.stepState.fix === "pending") {
          setActiveStep(next.stepState, "fix");
        }
        break;
      }

      case "candidate.final": {
        const cand = ensureCandidate(next, data.candidate_id);
        if (cand) {
          cand.survived = Boolean(data.survived);
          cand.rejectReason = typeof data.reject_reason === "string" ? data.reject_reason : "";
          cand.diff = typeof data.diff === "string" ? data.diff : cand.diff;
          cand.screenshot = typeof data.screenshot === "string" ? data.screenshot : cand.screenshot;
        }
        next.stepState.fix = "done";
        setActiveStep(next.stepState, "verify");
        break;
      }

      case "repair.started": {
        next.repairStarted = true;
        setActiveStep(next.stepState, "fix");
        break;
      }

      case "verdict": {
        next.verdict = {
          status: typeof data.status === "string" ? data.status : "ABSTAIN",
          winnerId: data.winner_id || null,
          reason: typeof data.reason === "string" ? data.reason : "",
        };
        next.winnerId = next.verdict.winnerId;
        const ok = next.verdict.status === "VERIFIED_FIX";
        if (next.stepState.watch === "active") next.stepState.watch = ok ? "done" : "fail";
        if (next.stepState.understand === "active") next.stepState.understand = ok ? "done" : "fail";
        if (next.stepState.reproduce === "active") next.stepState.reproduce = "fail";
        if (next.stepState.fix === "active") next.stepState.fix = "done";
        if (next.stepState.verify === "active") next.stepState.verify = ok ? "done" : "fail";
        next.stepState.verdict = ok ? "done" : (next.verdict.status === "NO_VERIFIED_FIX" ? "fail" : "done");
        break;
      }

      case "run.finished": {
        next.running = false;
        next.finished = true;
        if (typeof data.latency_s === "number") next.latencyS = data.latency_s;
        if (typeof data.cost_usd === "number") next.costUsd = data.cost_usd;
        if (!next.verdict && data.status) {
          next.verdict = {
            status: String(data.status),
            winnerId: next.winnerId,
            reason: "",
          };
        }
        break;
      }

      case "log":
      default:
        break;
    }
    return next;
  }

  function applyRunResult(prevState, res) {
    if (!res || typeof res !== "object") return prevState;
    const next = prevState;
    next.started = true;
    next.running = false;
    next.finished = true;
    next.runId = res.run_id || next.runId;
    next.appId = res.app_id || next.appId;
    next.stageReached = res.stage_reached || next.stageReached;

    if (res.video) {
      next.video = res.video;
      next.videoSrc = assetUrl(next, res.video.path || "video.mp4");
    }
    if (res.spec) {
      next.spec = res.spec;
    }
    if (Array.isArray(res.gates)) {
      for (const g of res.gates) {
        if (g && g.gate) next.gates[g.gate] = g;
      }
    }
    if (res.repro_test) {
      next.repro.testPath = res.repro_test;
      if (!next.repro.screenshot && next.gates.G1 && next.gates.G1.passed) {
        next.repro.screenshot = "shots/repro_fail.png";
      }
    }
    if (Array.isArray(res.candidates)) {
      next.candidates = [];
      next.candidateMap = {};
      for (const c of res.candidates) {
        if (!c || !c.id) continue;
        const cand = ensureCandidate(next, c.id, c.source);
        cand.filesChanged = Array.isArray(c.files_changed) ? c.files_changed : [];
        cand.linesChanged = typeof c.lines_changed === "number" ? c.lines_changed : 0;
        cand.survived = Boolean(c.survived);
        cand.rejectReason = c.reject_reason || "";
        cand.diff = `diffs/${c.id}.diff`;
        if (c.survived || (Array.isArray(c.gates) && c.gates.some((g) => g.gate === "G4"))) {
          cand.screenshot = `shots/${c.id}_after.png`;
        }
        if (Array.isArray(c.gates)) {
          for (const cg of c.gates) {
            if (cg && cg.gate) cand.gates[cg.gate] = cg;
          }
        }
      }
      if (next.candidates.some((c) => c.source === "repair")) {
        next.repairStarted = true;
      }
    }
    next.winnerId = res.winner_id || null;
    next.verdict = {
      status: res.status || "ABSTAIN",
      winnerId: next.winnerId,
      reason: res.reason || "",
    };
    next.latencyS = typeof res.latency_s === "number" ? res.latency_s : null;
    next.costUsd = res.usage && typeof res.usage.cost_usd === "number" ? res.usage.cost_usd : null;

    if (next.gates.G0 || next.video) next.stepState.watch = "done";
    if (next.spec) next.stepState.understand = "done";
    if (next.repro.testPath || next.repro.testCode) next.stepState.reproduce = "done";
    if (next.candidates.length > 0) {
      next.stepState.fix = "done";
      next.stepState.verify = next.verdict.status === "VERIFIED_FIX" ? "done" : "fail";
    }
    next.stepState.verdict = next.verdict.status === "VERIFIED_FIX" ? "done" : "fail";
    return next;
  }

  // --- Rendering functions per card ---

  function renderHeader(s) {
    dom.replayPill.hidden = !s.isReplay;
  }

  function renderCapture(s) {
    if (s.running) {
      dom.idleActions.hidden = true;
      dom.previewControls.hidden = true;
      dom.runningNote.hidden = false;
      dom.runningText.textContent = s.mode === "fixture" ? "Playing fixture run…" : "Analyzing recording…";
      dom.runningId.textContent = s.runId || "";
    } else {
      dom.runningNote.hidden = true;
      if (!mediaRecorder || mediaRecorder.state !== "recording") {
        dom.idleActions.hidden = Boolean(pendingFile);
        dom.previewControls.hidden = !pendingFile;
      }
    }
  }

  function renderStageRail(s) {
    dom.stageRail.hidden = !s.started;
    if (!s.started) return;

    let completedCount = 0;
    const stepEls = dom.stageSteps.querySelectorAll(".stage-step");
    stepEls.forEach((el, idx) => {
      const key = el.dataset.step;
      const st = s.stepState[key] || "pending";
      el.classList.remove("is-pending", "is-active", "is-done", "is-fail");
      el.classList.add(`is-${st}`);

      const node = el.querySelector(".stage-node");
      if (st === "done") {
        node.textContent = "✓";
        completedCount = Math.max(completedCount, idx);
      } else if (st === "fail") {
        node.textContent = "✕";
      } else {
        node.innerHTML = '<span class="stage-dot"></span>';
      }
    });

    if (completedCount > lastCompletedStepIndex) {
      lastCompletedStepIndex = completedCount;
      dom.stageShimmer.classList.remove("is-sweeping");
      void dom.stageShimmer.offsetWidth;
      dom.stageShimmer.classList.add("is-sweeping");
    }
  }

  function formatSeconds(sec) {
    const val = Number.isFinite(sec) ? Math.max(0, sec) : 0;
    return `${val.toFixed(1)}s`;
  }

  function renderEvidence(s) {
    const show = Boolean(s.video || s.spec || s.gates.G0);
    dom.evidenceCard.hidden = !show;
    if (!show) return;

    if (s.videoSrc && dom.evidenceVideo.getAttribute("src") !== s.videoSrc) {
      dom.evidenceVideo.src = s.videoSrc;
    }

    const duration = (s.video && s.video.duration_s) || dom.evidenceVideo.duration || 0;
    if (duration > 0) {
      dom.timelineDuration.textContent = formatSeconds(duration);
    }

    if (s.gates.G0) {
      const g0 = s.gates.G0;
      const pTxt = typeof g0.p_yes === "number" ? g0.p_yes.toFixed(2) : "—";
      // Advisory only: a quiet line, no pass/fail styling.
      dom.g0Line.hidden = false;
      dom.g0Line.className = "gate-pill is-advisory";
      dom.g0Line.textContent = `Bug likelihood · ${pTxt}`;
    } else {
      dom.g0Line.hidden = true;
    }

    if (s.spec) {
      dom.specPending.hidden = true;
      dom.specContent.hidden = false;
      dom.specSummary.textContent = s.spec.summary || "";
      dom.specExpected.textContent = s.spec.expected || "";
      dom.specObserved.textContent = s.spec.observed || "";

      dom.specSteps.innerHTML = "";
      for (const stepText of s.spec.steps || []) {
        const li = document.createElement("li");
        li.textContent = stepText;
        dom.specSteps.appendChild(li);
      }

      renderTimelineTicks(s);
    } else {
      dom.specPending.hidden = false;
      dom.specContent.hidden = true;
      dom.evidenceTicks.innerHTML = "";
    }
  }

  function renderTimelineTicks(s) {
    dom.evidenceTicks.innerHTML = "";
    if (!s.spec || !Array.isArray(s.spec.evidence_timestamps_s)) return;
    const duration = (s.video && s.video.duration_s) || dom.evidenceVideo.duration || 0;
    if (duration <= 0) return;

    s.spec.evidence_timestamps_s.forEach((t, idx) => {
      const pct = Math.max(0, Math.min(100, (t / duration) * 100));
      const note = (s.spec.evidence_notes && s.spec.evidence_notes[idx]) || `Marker at ${t.toFixed(1)}s`;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "timeline-tick";
      btn.style.left = `${pct}%`;
      btn.setAttribute("aria-label", `Jump to ${t.toFixed(1)}s: ${note}`);

      const showTip = () => {
        dom.timelineTooltip.textContent = `${t.toFixed(1)}s · ${note}`;
        dom.timelineTooltip.style.left = `${Math.max(14, Math.min(86, pct))}%`;
        dom.timelineTooltip.hidden = false;
      };
      const hideTip = () => {
        dom.timelineTooltip.hidden = true;
      };

      btn.addEventListener("mouseenter", showTip);
      btn.addEventListener("focus", showTip);
      btn.addEventListener("mouseleave", hideTip);
      btn.addEventListener("blur", hideTip);
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        seekVideo(t);
      });
      dom.evidenceTicks.appendChild(btn);
    });
  }

  function seekVideo(t) {
    const vid = dom.evidenceVideo;
    if (!vid) return;
    const duration = (state.video && state.video.duration_s) || vid.duration || 0;
    const clamped = duration > 0 ? Math.max(0, Math.min(duration, t)) : Math.max(0, t);
    try {
      vid.currentTime = clamped;
    } catch (_) {
      // Ignore if metadata not loaded yet
    }
    updateTimelineProgress();
  }

  function updateTimelineProgress() {
    const vid = dom.evidenceVideo;
    const duration = (state.video && state.video.duration_s) || vid.duration || 0;
    const cur = vid.currentTime || 0;
    dom.timelineCurrent.textContent = formatSeconds(cur);
    if (duration > 0) {
      const pct = Math.max(0, Math.min(100, (cur / duration) * 100));
      dom.timelineProgress.style.width = `${pct}%`;
      dom.evidenceTimeline.setAttribute("aria-valuenow", String(Math.round(pct)));
    }
  }

  function renderRepro(s) {
    const show = Boolean(s.reproAttempts.length || s.gates.G1 || s.gates.G1B || s.repro.screenshot || s.repro.testCode);
    dom.reproCard.hidden = !show;
    if (!show) return;

    if (s.repro.screenshot) {
      const src = assetUrl(s, s.repro.screenshot);
      if (dom.reproScreenshot.getAttribute("src") !== src) {
        dom.reproScreenshot.src = src;
      }
      dom.reproScreenshot.hidden = false;
      dom.reproShotPlaceholder.hidden = true;
    } else {
      dom.reproScreenshot.hidden = true;
      dom.reproShotPlaceholder.hidden = false;
    }

    dom.reproStatusBlock.innerHTML = "";
    const g1 = s.gates.G1;
    if (g1) {
      const headline = document.createElement("div");
      headline.className = "repro-headline";
      if (g1.passed) {
        const mismatch = s.gates.G1B && !s.gates.G1B.passed;
        const note = mismatch ? "Not the symptom in the video" : "Bug reproduced";
        headline.innerHTML = `<span class="repro-marks" aria-hidden="true">✕ ✕ ✕</span><span>Fails 3 of 3</span><span class="muted small">${note}</span>`;
      } else {
        headline.innerHTML = '<span class="repro-marks" aria-hidden="true">✕</span><span>Did not reproduce reliably</span>';
      }
      dom.reproStatusBlock.appendChild(headline);
    } else if (s.reproAttempts.length > 0) {
      const headline = document.createElement("div");
      headline.className = "repro-headline";
      headline.textContent = `Running reproduction attempt ${s.reproAttempts.length}…`;
      dom.reproStatusBlock.appendChild(headline);
    }

    if (s.gates.G1B) {
      const g1b = s.gates.G1B;
      const pTxt = typeof g1b.p_yes === "number" ? g1b.p_yes.toFixed(2) : "—";
      dom.g1bLine.hidden = false;
      dom.g1bLine.className = `repro-g1b ${g1b.passed ? "is-pass" : "is-fail"}`;
      dom.g1bLine.textContent = g1b.passed
        ? `✓ Symptom match · ${pTxt}`
        : `✕ Symptom mismatch · ${pTxt}`;
    } else {
      dom.g1bLine.hidden = true;
    }

    dom.reproAttemptsList.innerHTML = "";
    for (const att of s.reproAttempts) {
      const row = document.createElement("div");
      row.className = "attempt-row tabular-nums";
      const label = ATTEMPT_LABELS[att.outcome] || att.outcome;
      const detail = shortText(att.detail, 140);
      row.textContent = `Attempt ${att.attempt}: ${label}${detail ? ` — ${detail}` : ""}`;
      if (att.detail && detail !== att.detail) row.title = att.detail;
      dom.reproAttemptsList.appendChild(row);
    }

    if (s.repro.testCode) {
      dom.reproTestDetails.hidden = false;
      dom.reproTestCode.textContent = s.repro.testCode;
    } else {
      dom.reproTestDetails.hidden = true;
    }
  }

  const ATTEMPT_LABELS = {
    failed_assertion: "Assertion failed as expected",
    failed_other: "Test broke before its assertion",
    passed: "Test passed, bug not shown",
  };

  // First line only, capped, so tool output (tracebacks, banners) never floods a card.
  function shortText(text, max) {
    if (!text) return "";
    const first = String(text).split(/\r?\n/)[0].trim();
    return first.length > max ? `${first.slice(0, max - 1).trimEnd()}…` : first;
  }

  // Keep the verdict to one readable sentence: drop a long trailing "(...)" tool detail.
  function calmReason(reason) {
    if (!reason) return "";
    let r = String(reason).trim();
    const paren = r.indexOf(" (");
    if (r.length > 160 && paren > 20) r = `${r.slice(0, paren).trimEnd()}.`;
    return shortText(r, 220);
  }

  function makeCheckpointEl(label, status, extraText = "") {
    const span = document.createElement("span");
    span.className = `checkpoint is-${status}`;
    const icon = document.createElement("span");
    icon.className = "cp-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = status === "pass" ? "✓" : (status === "fail" ? "✕" : "•");
    const txt = document.createElement("span");
    txt.textContent = extraText ? `${label} · ${extraText}` : label;
    span.appendChild(icon);
    span.appendChild(txt);
    return span;
  }

  function renderLanes(s) {
    const show = s.candidates.length > 0 || s.repairStarted;
    dom.lanesCard.hidden = !show;
    if (!show) return;

    dom.lanesSubtitle.textContent = s.repairStarted
      ? "Initial candidates + Neon repair round"
      : "Parallel candidate patches";

    dom.lanesList.innerHTML = "";
    let repairHeadingShown = false;

    for (const cand of s.candidates) {
      if (cand.source === "repair" && !repairHeadingShown) {
        repairHeadingShown = true;
        const div = document.createElement("div");
        div.className = "repair-divider";
        div.textContent = "Repair round";
        dom.lanesList.appendChild(div);
      }

      const row = document.createElement("div");
      const isWinner = s.winnerId && s.winnerId === cand.id;
      const isRejected = cand.survived === false;
      row.className = `lane-row${isWinner ? " is-winner" : ""}${isRejected ? " is-rejected" : ""}`;

      const top = document.createElement("div");
      top.className = "lane-top";

      const ident = document.createElement("div");
      ident.className = "lane-identity";
      const idSpan = document.createElement("span");
      idSpan.className = "lane-id";
      idSpan.textContent = cand.id;
      ident.appendChild(idSpan);

      const fileStr = cand.filesChanged.length ? cand.filesChanged.join(", ") : "pending";
      const metaSpan = document.createElement("span");
      metaSpan.className = "muted small tabular-nums";
      metaSpan.textContent = `${fileStr} · ${cand.linesChanged} ${cand.linesChanged === 1 ? "line" : "lines"}`;
      ident.appendChild(metaSpan);

      if (isWinner) {
        const badge = document.createElement("span");
        badge.className = "winner-tag";
        badge.textContent = "Winner";
        ident.appendChild(badge);
      }
      top.appendChild(ident);

      const cps = document.createElement("div");
      cps.className = "lane-checkpoints";

      const g2 = cand.gates.G2;
      const g3 = cand.gates.G3;
      const g4 = cand.gates.G4;

      const g2Status = g2 ? (g2.passed ? "pass" : "fail") : "pending";
      const g3Status = g3 ? (g3.passed ? "pass" : "fail") : "pending";

      const patchLimitFail =
        cand.survived === false &&
        /patch|limit|lines|files|extension|test/i.test(cand.rejectReason || "") &&
        !g2;
      const smallStatus = patchLimitFail
        ? "fail"
        : (cand.filesChanged.length <= 2 && cand.linesChanged <= 40 && (g2 || cand.survived !== null)
            ? "pass"
            : "pending");

      const g4Status = g4 ? (g4.passed ? "pass" : "fail") : "pending";
      const g4Extra = g4 && typeof g4.p_yes === "number" ? g4.p_yes.toFixed(2) : "";

      cps.appendChild(makeCheckpointEl("Test passes", g2Status));
      cps.appendChild(makeCheckpointEl("Nothing breaks", g3Status));
      cps.appendChild(makeCheckpointEl("Small change", smallStatus));
      cps.appendChild(makeCheckpointEl("Bug gone", g4Status, g4Extra));

      top.appendChild(cps);
      row.appendChild(top);

      if (isRejected && cand.rejectReason) {
        const reason = document.createElement("div");
        reason.className = "lane-reason";
        reason.textContent = cand.rejectReason;
        row.appendChild(reason);
      }

      dom.lanesList.appendChild(row);
    }
  }

  function verdictPresentation(status, s) {
    // G0 is advisory, so an ABSTAIN after the video was understood means "could not reproduce", not "not a bug".
    if (status === "ABSTAIN" && s && (s.spec || s.reproAttempts.length)) {
      return { word: "No fix", cls: "is-neutral", icon: "—" };
    }
    switch (status) {
      case "VERIFIED_FIX":
        return { word: "Verified", cls: "is-verified", icon: "✓" };
      case "NO_VERIFIED_FIX":
        return { word: "No verified fix", cls: "is-none", icon: "!" };
      case "REJECTED_INPUT":
        return { word: "Could not read this video", cls: "is-neutral", icon: "—" };
      case "ABSTAIN":
      default:
        return { word: "Not a bug video", cls: "is-neutral", icon: "—" };
    }
  }

  function renderDiff(diffText) {
    dom.diffViewer.innerHTML = "";
    const lines = diffText.replace(/\r\n/g, "\n").split("\n");
    for (const line of lines) {
      if (line === "" && lines[lines.length - 1] === line) continue;
      const el = document.createElement("div");
      let kind = "is-ctx";
      if (line.startsWith("+++") || line.startsWith("---")) kind = "is-meta";
      else if (line.startsWith("@@")) kind = "is-hunk";
      else if (line.startsWith("+")) kind = "is-add";
      else if (line.startsWith("-")) kind = "is-del";
      el.className = `diff-line ${kind}`;
      el.textContent = line || " ";
      dom.diffViewer.appendChild(el);
    }
  }

  function renderVerdict(s) {
    const show = Boolean(s.verdict);
    dom.verdictCard.hidden = !show;
    if (!show) return;

    const pres = verdictPresentation(s.verdict.status, s);
    dom.verdictBanner.className = `verdict-banner ${pres.cls}`;
    dom.verdictIcon.textContent = pres.icon;
    dom.verdictWord.textContent = pres.word;
    dom.verdictReason.textContent = calmReason(s.verdict.reason);
    if (s.verdict.reason && dom.verdictReason.textContent !== s.verdict.reason) {
      dom.verdictReason.title = s.verdict.reason;
    }

    const metaParts = [];
    if (typeof s.latencyS === "number") metaParts.push(`${s.latencyS.toFixed(1)}s`);
    if (typeof s.costUsd === "number" && s.costUsd > 0) metaParts.push(`$${s.costUsd.toFixed(3)}`);
    dom.verdictMeta.textContent = metaParts.join(" · ");

    dom.replayBtn.hidden = !(s.mode === "live" && s.finished && s.runId && s.verdict.status !== "REJECTED_INPUT");

    if (!s.verdictAnimated) {
      s.verdictAnimated = true;
      dom.verdictShimmer.classList.remove("is-sweeping");
      void dom.verdictShimmer.offsetWidth;
      dom.verdictShimmer.classList.add("is-sweeping");
    }

    const winner = s.winnerId ? s.candidateMap[s.winnerId] : null;
    const hasCompare = Boolean(
      s.verdict.status === "VERIFIED_FIX" &&
        s.repro.screenshot &&
        winner &&
        winner.screenshot
    );
    const diffPath = winner && winner.diff ? winner.diff : null;

    dom.verdictProof.hidden = !(hasCompare || diffPath);
    dom.compareSection.hidden = !hasCompare;
    if (hasCompare) {
      const beforeUrl = assetUrl(s, s.repro.screenshot);
      const afterUrl = assetUrl(s, winner.screenshot);
      if (dom.compareBefore.getAttribute("src") !== beforeUrl) dom.compareBefore.src = beforeUrl;
      if (dom.compareAfter.getAttribute("src") !== afterUrl) dom.compareAfter.src = afterUrl;
    }

    dom.diffSection.hidden = !diffPath;
    if (diffPath) {
      dom.diffFileLabel.textContent = winner.filesChanged.join(", ");
      const url = assetUrl(s, diffPath);
      if (s.diffPath !== url) {
        s.diffPath = url;
        loadDiffText(url);
      } else if (s.diffText) {
        renderDiff(s.diffText);
      }
    }
  }

  async function loadDiffText(url) {
    if (!url) return;
    if (diffCache.has(url)) {
      state.diffText = diffCache.get(url);
      renderDiff(state.diffText);
      return;
    }
    try {
      const resp = await fetch(url);
      if (!resp.ok) return;
      const text = await resp.text();
      diffCache.set(url, text);
      if (state.diffPath === url) {
        state.diffText = text;
        renderDiff(text);
      }
    } catch (_) {
      // Quietly leave diff empty on fetch error
    }
  }

  async function loadReproText(url) {
    if (!url) return;
    try {
      const resp = await fetch(url);
      if (!resp.ok) return;
      const text = await resp.text();
      if (!state.repro.testCode) {
        state.repro.testCode = text;
        renderRepro(state);
      }
    } catch (_) {
      // Ignore
    }
  }

  function renderAll() {
    renderHeader(state);
    renderCapture(state);
    renderStageRail(state);
    renderEvidence(state);
    renderRepro(state);
    renderLanes(state);
    renderVerdict(state);
  }

  // --- Before/after compare slider ---

  function setComparePercent(pct) {
    const clamped = Math.max(0, Math.min(100, pct));
    dom.compareAfterWrap.style.clipPath = `inset(0 0 0 ${clamped}%)`;
    dom.compareDivider.style.left = `${clamped}%`;
    dom.compareDivider.setAttribute("aria-valuenow", String(Math.round(clamped)));
  }

  function initCompareSlider() {
    let dragging = false;
    const updateFromClientX = (clientX) => {
      const rect = dom.compareBox.getBoundingClientRect();
      if (!rect.width) return;
      setComparePercent(((clientX - rect.left) / rect.width) * 100);
    };
    dom.compareDivider.addEventListener("pointerdown", (e) => {
      dragging = true;
      dom.compareDivider.setPointerCapture(e.pointerId);
      updateFromClientX(e.clientX);
    });
    window.addEventListener("pointermove", (e) => {
      if (dragging) updateFromClientX(e.clientX);
    });
    window.addEventListener("pointerup", () => {
      dragging = false;
    });
    dom.compareDivider.addEventListener("keydown", (e) => {
      const cur = Number(dom.compareDivider.getAttribute("aria-valuenow")) || 50;
      const step = e.shiftKey ? 10 : 5;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        setComparePercent(cur - step);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        setComparePercent(cur + step);
      } else if (e.key === "Home") {
        e.preventDefault();
        setComparePercent(0);
      } else if (e.key === "End") {
        e.preventDefault();
        setComparePercent(100);
      }
    });
  }

  // --- Timeline scrubbing ---

  function initTimelineScrub() {
    const scrub = (clientX) => {
      const rect = dom.evidenceTimeline.getBoundingClientRect();
      if (!rect.width) return;
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const duration = (state.video && state.video.duration_s) || dom.evidenceVideo.duration || 0;
      if (duration > 0) seekVideo(ratio * duration);
    };
    dom.evidenceTimeline.addEventListener("click", (e) => {
      if (e.target.closest(".timeline-tick")) return;
      scrub(e.clientX);
    });
    dom.evidenceVideo.addEventListener("timeupdate", updateTimelineProgress);
    dom.evidenceVideo.addEventListener("loadedmetadata", () => {
      updateTimelineProgress();
      renderTimelineTicks(state);
    });
  }

  // --- Recording & Upload ---

  function showCaptureError(msg) {
    if (!msg) {
      dom.captureError.hidden = true;
      dom.captureError.textContent = "";
      return;
    }
    dom.captureError.textContent = msg;
    dom.captureError.hidden = false;
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes)) return "";
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function formatTimer(elapsedS) {
    const e = Math.min(MAX_RECORD_S, Math.max(0, Math.floor(elapsedS)));
    const mm = String(Math.floor(e / 60)).padStart(2, "0");
    const ss = String(e % 60).padStart(2, "0");
    const left = Math.max(0, MAX_RECORD_S - e);
    return `${mm}:${ss} · ${left}s left`;
  }

  function stopMediaTracks() {
    for (const s of activeStreams) {
      s.getTracks().forEach((t) => t.stop());
    }
    activeStreams = [];
  }

  function setPendingFile(file) {
    if (pendingObjectUrl) {
      URL.revokeObjectURL(pendingObjectUrl);
      pendingObjectUrl = null;
    }
    pendingFile = file;
    if (!file) {
      dom.previewVideo.removeAttribute("src");
      dom.previewControls.hidden = true;
      dom.idleActions.hidden = false;
      return;
    }
    pendingObjectUrl = URL.createObjectURL(file);
    dom.previewVideo.src = pendingObjectUrl;
    dom.previewName.textContent = file.name;
    dom.previewDetail.textContent = formatBytes(file.size);
    dom.idleActions.hidden = true;
    dom.previewControls.hidden = false;
    showCaptureError("");
  }

  function validateSelectedFile(file) {
    if (!file) return "No file selected.";
    const lower = (file.name || "").toLowerCase();
    const hasValidExt = ACCEPTED_EXT.some((ext) => lower.endsWith(ext));
    if (!hasValidExt) {
      return `Unsupported format. Accepted extensions: ${ACCEPTED_EXT.join(", ")}.`;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      return `Upload is ${(file.size / (1024 * 1024)).toFixed(0)} MB, limit is 200 MB.`;
    }
    return "";
  }

  async function startRecording() {
    showCaptureError("");
    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
      showCaptureError("Screen recording is not supported in this browser.");
      return;
    }

    let displayStream;
    try {
      displayStream = await navigator.mediaDevices.getDisplayMedia({ video: true });
    } catch (_) {
      showCaptureError("Screen recording was cancelled or permission was denied.");
      return;
    }

    const tracks = [...displayStream.getVideoTracks()];
    activeStreams.push(displayStream);

    if (navigator.mediaDevices.getUserMedia) {
      try {
        const micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        activeStreams.push(micStream);
        tracks.push(...micStream.getAudioTracks());
      } catch (_) {
        // Optional microphone denied or unavailable; proceed with video only
      }
    }

    const combined = new MediaStream(tracks);
    const mimeCandidates = [
      "video/webm;codecs=vp9,opus",
      "video/webm;codecs=vp8,opus",
      "video/webm",
    ];
    const mimeType = mimeCandidates.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";

    try {
      mediaRecorder = mimeType ? new MediaRecorder(combined, { mimeType }) : new MediaRecorder(combined);
    } catch (_) {
      stopMediaTracks();
      showCaptureError("Could not start the screen recorder in this browser.");
      return;
    }

    recordedChunks = [];
    mediaRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) recordedChunks.push(e.data);
    };
    mediaRecorder.onstop = () => {
      clearInterval(recordInterval);
      recordInterval = null;
      stopMediaTracks();
      dom.recordingControls.hidden = true;
      const blob = new Blob(recordedChunks, { type: mediaRecorder.mimeType || "video/webm" });
      const file = new File([blob], "recording.webm", { type: blob.type });
      file._glintSource = "record";
      setPendingFile(file);
    };

    const videoTrack = displayStream.getVideoTracks()[0];
    if (videoTrack) {
      videoTrack.onended = () => {
        if (mediaRecorder && mediaRecorder.state === "recording") {
          mediaRecorder.stop();
        }
      };
    }

    mediaRecorder.start(250);
    recordStartMs = Date.now();
    dom.idleActions.hidden = true;
    dom.previewControls.hidden = true;
    dom.recordingControls.hidden = false;
    dom.recordTimer.textContent = formatTimer(0);

    recordInterval = setInterval(() => {
      const elapsedS = (Date.now() - recordStartMs) / 1000;
      dom.recordTimer.textContent = formatTimer(elapsedS);
      if (elapsedS >= MAX_RECORD_S && mediaRecorder && mediaRecorder.state === "recording") {
        mediaRecorder.stop();
      }
    }, 250);
  }

  function stopRecording() {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      mediaRecorder.stop();
    }
  }

  // --- Live run SSE + polling fallback ---

  function cleanupRunTransports() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    for (const id of fixtureTimers) {
      clearTimeout(id);
    }
    fixtureTimers = [];
  }

  function resetRunState(opts = {}) {
    cleanupRunTransports();
    lastCompletedStepIndex = -1;
    diffCache.clear();
    state = createInitialState(opts);
    setComparePercent(50);
    renderAll();
  }

  async function extractApiError(resp, fallbackMsg) {
    try {
      const body = await resp.json();
      if (body && body.error && body.error.message) {
        return body.error.message;
      }
    } catch (_) {
      // Ignore parse failure
    }
    return fallbackMsg;
  }

  async function submitAnalyze() {
    if (!pendingFile) return;
    const validationErr = validateSelectedFile(pendingFile);
    if (validationErr) {
      showCaptureError(validationErr);
      return;
    }
    showCaptureError("");

    const form = new FormData();
    form.append("video", pendingFile, pendingFile.name || "recording.webm");
    form.append("app_id", dom.appSelect.value || "todomvc");
    form.append("source", pendingFile._glintSource === "record" ? "record" : "upload");

    dom.analyzeBtn.disabled = true;
    try {
      const resp = await fetch("/api/runs", { method: "POST", body: form });
      if (!resp.ok) {
        const msg = await extractApiError(resp, `Unable to start run (HTTP ${resp.status}).`);
        showCaptureError(msg);
        dom.analyzeBtn.disabled = false;
        return;
      }
      const payload = await resp.json();
      setPendingFile(null);
      resetRunState();
      state.runId = payload.run_id;
      state.started = true;
      state.running = true;
      renderAll();
      subscribeRunEvents(payload.run_id);
    } catch (_) {
      showCaptureError("Could not reach the Glint backend server.");
    } finally {
      dom.analyzeBtn.disabled = false;
    }
  }

  async function triggerReplay() {
    if (!state.runId) return;
    showCaptureError("");
    try {
      const resp = await fetch(`/api/runs/${encodeURIComponent(state.runId)}/replay`, { method: "POST" });
      if (!resp.ok) {
        showCaptureError(await extractApiError(resp, "Unable to replay this run."));
        return;
      }
      const payload = await resp.json();
      resetRunState();
      state.runId = payload.run_id;
      state.started = true;
      state.running = true;
      state.isReplay = true;
      renderAll();
      subscribeRunEvents(payload.run_id);
    } catch (_) {
      showCaptureError("Could not reach the Glint backend server.");
    }
  }

  function subscribeRunEvents(runId) {
    if (!window.EventSource) {
      startPolling(runId);
      return;
    }
    const url = `/api/runs/${encodeURIComponent(runId)}/events`;
    try {
      // Keep the run in the address bar so a reload (or a backup tab) reopens it.
      window.history.replaceState(null, "", `?run=${encodeURIComponent(runId)}`);
    } catch (_) {
      // Ignore: history API unavailable
    }
    eventSource = new EventSource(url);

    const handleMsg = (e) => {
      if (!e || !e.data) return;
      try {
        const ev = JSON.parse(e.data);
        state = applyEvent(state, ev);
        renderAll();
        if (ev.type === "repro.ready" && state.repro.testPath && !state.repro.testCode) {
          loadReproText(assetUrl(state, state.repro.testPath));
        }
        if (ev.type === "run.finished") {
          cleanupRunTransports();
        }
      } catch (_) {
        // Ignore malformed event payload
      }
    };

    const eventNames = [
      "run.started",
      "input.normalized",
      "gate.result",
      "spec.ready",
      "repro.attempt",
      "repro.ready",
      "candidate.created",
      "candidate.final",
      "repair.started",
      "verdict",
      "run.finished",
      "log",
    ];
    for (const name of eventNames) {
      eventSource.addEventListener(name, handleMsg);
    }
    eventSource.onmessage = handleMsg;

    eventSource.onerror = () => {
      if (state.finished) {
        cleanupRunTransports();
        return;
      }
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      startPolling(runId);
    };
  }

  function startPolling(runId) {
    if (pollTimer) return;
    const pollOnce = async () => {
      try {
        const resp = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
        if (resp.status === 404) {
          clearInterval(pollTimer);
          pollTimer = null;
          state.running = false;
          state.started = false;
          renderAll();
          showCaptureError(await extractApiError(resp, "This run was not found."));
          return;
        }
        if (!resp.ok) return;
        const body = await resp.json();
        if (body && body.running === true && !body.status) {
          return;
        }
        if (body && body.status) {
          clearInterval(pollTimer);
          pollTimer = null;
          state = applyRunResult(state, body);
          renderAll();
          if (state.repro.testPath && !state.repro.testCode) {
            loadReproText(assetUrl(state, state.repro.testPath));
          }
        }
      } catch (_) {
        // Retry on next 2s tick
      }
    };
    pollOnce();
    pollTimer = setInterval(pollOnce, 2000);
  }

  // --- Fixture playback ---

  async function playFixture(fixtureName) {
    resetRunState({ fixture: fixtureName });
    state.started = true;
    state.running = true;
    renderAll();

    try {
      const resp = await fetch(`/web/fixtures/${encodeURIComponent(fixtureName)}/events.jsonl`);
      if (!resp.ok) {
        showCaptureError(`Could not load fixture '${fixtureName}'.`);
        state.running = false;
        renderAll();
        return;
      }
      const raw = await resp.text();
      const events = raw
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => JSON.parse(line));

      if (events.length === 0) return;

      let cumulativeDelay = 0;
      let prevTs = Date.parse(events[0].ts);
      events.forEach((ev, idx) => {
        if (idx > 0) {
          const curTs = Date.parse(ev.ts);
          const rawDelta = Number.isFinite(curTs) && Number.isFinite(prevTs) ? curTs - prevTs : 350;
          const clampedDelta = Math.max(80, Math.min(1500, rawDelta));
          cumulativeDelay += clampedDelta;
          prevTs = curTs;
        }
        const tid = setTimeout(() => {
          state = applyEvent(state, ev);
          renderAll();
          if (ev.type === "repro.ready" && state.repro.testPath && !state.repro.testCode) {
            loadReproText(assetUrl(state, state.repro.testPath));
          }
        }, cumulativeDelay);
        fixtureTimers.push(tid);
      });
    } catch (_) {
      showCaptureError(`Failed to parse fixture '${fixtureName}'.`);
      state.running = false;
      renderAll();
    }
  }

  // --- Results strip & live metadata ---

  function formatPct(val) {
    if (typeof val !== "number" || Number.isNaN(val)) return "—";
    return `${Math.round(val * 100)}%`;
  }

  function renderResultsStrip(data) {
    if (!data || data.available !== true || !Array.isArray(data.rows) || data.rows.length === 0) {
      dom.resultsStrip.hidden = true;
      return;
    }
    dom.resultsTbody.innerHTML = "";
    for (const row of data.rows) {
      const tr = document.createElement("tr");
      const cells = [
        { text: row.system || "", cls: "" },
        { text: formatPct(row.true_fix_rate), cls: "num" },
        { text: formatPct(row.precision_when_claiming), cls: "num" },
        { text: formatPct(row.wrong_fix_rate), cls: "num" },
        { text: formatPct(row.abstain_rate), cls: "num" },
      ];
      for (const c of cells) {
        const td = document.createElement("td");
        if (c.cls) td.className = c.cls;
        td.textContent = c.text;
        tr.appendChild(td);
      }
      dom.resultsTbody.appendChild(tr);
    }
    const nMut = typeof data.n_test_mutants === "number" ? data.n_test_mutants : 0;
    const hash = data.frozen_hash || "—";
    dom.resultsFootnote.textContent = `${nMut} bugs frozen before any scored run · commit ${hash}`;
    dom.resultsStrip.hidden = false;
  }

  async function initLiveMetadata() {
    try {
      const [healthResp, appsResp, resultsResp] = await Promise.all([
        fetch("/api/health"),
        fetch("/api/apps"),
        fetch("/api/results"),
      ]);

      if (healthResp.ok) {
        const h = await healthResp.json();
        const ok = h && h.status === "ok";
        dom.healthDot.className = `health-dot ${ok ? "is-ok" : "is-error"}`;
        dom.healthLabel.textContent = ok ? "Ready" : "Unavailable";
      } else {
        dom.healthDot.className = "health-dot is-error";
        dom.healthLabel.textContent = "Offline";
      }

      if (appsResp.ok) {
        const apps = await appsResp.json();
        if (Array.isArray(apps) && apps.length > 0) {
          dom.appSelect.innerHTML = "";
          for (const app of apps) {
            const opt = document.createElement("option");
            opt.value = app.id;
            opt.textContent = app.url ? `${app.name} (${app.url})` : app.name;
            dom.appSelect.appendChild(opt);
          }
        }
      }

      if (resultsResp.ok) {
        const resData = await resultsResp.json();
        renderResultsStrip(resData);
      }
    } catch (_) {
      dom.healthDot.className = "health-dot is-error";
      dom.healthLabel.textContent = "Offline";
    }
  }

  function initEventListeners() {
    dom.recordBtn.addEventListener("click", startRecording);
    dom.stopBtn.addEventListener("click", stopRecording);
    dom.uploadBtn.addEventListener("click", () => dom.fileInput.click());
    dom.clearBtn.addEventListener("click", () => setPendingFile(null));
    dom.analyzeBtn.addEventListener("click", submitAnalyze);
    dom.replayBtn.addEventListener("click", triggerReplay);

    dom.fileInput.addEventListener("change", () => {
      const f = dom.fileInput.files && dom.fileInput.files[0];
      dom.fileInput.value = "";
      if (!f) return;
      const err = validateSelectedFile(f);
      if (err) {
        showCaptureError(err);
        return;
      }
      f._glintSource = "upload";
      setPendingFile(f);
    });

    ["dragenter", "dragover"].forEach((evName) => {
      dom.dropZone.addEventListener(evName, (e) => {
        e.preventDefault();
        dom.dropZone.classList.add("is-dragover");
      });
    });

    ["dragleave", "drop"].forEach((evName) => {
      dom.dropZone.addEventListener(evName, (e) => {
        e.preventDefault();
        dom.dropZone.classList.remove("is-dragover");
      });
    });

    dom.dropZone.addEventListener("drop", (e) => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      const err = validateSelectedFile(f);
      if (err) {
        showCaptureError(err);
        return;
      }
      f._glintSource = "upload";
      setPendingFile(f);
    });
  }

  function boot() {
    initCompareSlider();
    initTimelineScrub();
    initEventListeners();

    const params = new URLSearchParams(window.location.search);
    const fixture = params.get("fixture");
    const runParam = params.get("run");
    if (fixture) {
      dom.healthDot.className = "health-dot is-ok";
      dom.healthLabel.textContent = "Fixture";
      playFixture(fixture);
    } else {
      initLiveMetadata();
      if (runParam && /^[A-Za-z0-9_-]+$/.test(runParam)) {
        // Open an existing run (demo backup): the events route replays every past event to a late subscriber.
        resetRunState();
        state.runId = runParam;
        state.started = true;
        state.running = true;
        renderAll();
        subscribeRunEvents(runParam);
      }
    }
  }

  // Export reducer for potential smoke/inspection hooks without polluting global UX
  window.GlintUI = { applyEvent, applyRunResult };

  document.addEventListener("DOMContentLoaded", boot);
})();
