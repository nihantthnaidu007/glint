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
