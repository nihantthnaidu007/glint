# Freeze

Freeze commit: 7aae1b6c0d01ab94a538aa6a03653ebe86aa2ec2

- Frozen on Nov 21 2026 at 15:00 local time, before the scored run.
- Corpus tree at that commit (`git rev-parse val/corpus`): tree a299c22cb0f39471c8e909c90ae0e83e4bbb8e89
- Branch: phase/2-pipeline

## Counts

- dev = 7 mutants, test = 18 mutants, 3 mutants excluded (28 generated).
- Test split recordings scored: 18 R0 (clean screen recordings) and 3 R3 (phone recordings: m009, m010, m011).
- Clean videos scored: 1 (c04). Clean videos excluded: 4.

## Exclusions (full reasons in eval/corpus/EXCLUSIONS.md)

- Mutants m008, m014, m019: the bug is not visible on screen in the recording (m008 and m014 are also not
  detected by the oracle).
- Clean videos c01, c02, c03, c05: the recording uses the toggle-all control, where the unmutated app has a
  real quirk, so the video is not clean.

## Thresholds and gate settings (glint/config.py at the freeze commit)

- TH_G0_IS_DEFECT = 0.10
- TH_G1B_SYMPTOM_MATCH = 0.60
- TH_G4_DEFECT_GONE = 0.68
- DECISION_VIDEO_RESOLUTION = "high"
- N_CANDIDATES = 4, RUN_BUDGET_S = 150
- Models: gemini-4-neon, gemini-4-hydrogen, gemini-decision-1

## What is frozen from this point

Nothing under eval/corpus, no threshold or question wording in glint/config.py, no file in glint/, and
eval/PREDICTIONS.md.
