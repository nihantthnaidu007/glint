# Changes made after the v1 scored run

Written on Nov 21 2026 at 15:41 local time, AFTER the v1 results on the frozen test split had been seen.
This change was chosen because of those results. It is therefore not a pre-registered result: v1 is the
scored number, and anything labeled v2 is a disclosed post-hoc change.

## Why

In the v1 scored run Glint missed 7 of 21 bug runs. In 5 of those 7 (m009 R0, m009 R3, m015, m024, m028) all
four candidates were the exact correct fix, passed the frozen repro test (G2) and the never-worse check (G3),
and were rejected only by G4, the Decision model's screenshot judgment (scores 0.09 to 0.29, threshold 0.68).

## What changed (one change, commit "contract: v2 consensus rule (after v1 results)")

1. `glint/config.py`: `CONSENSUS_MIN = 3`.
2. `glint/pipeline.py`, selection: a candidate that passed G2 and G3 is verified if G4 passed, OR if its
   patch is identical (same files, same contents) to at least `CONSENSUS_MIN - 1` other candidates that also
   passed G2 and G3. G4-passing winners are preferred. The reason sentence states which basis was used:
   "(visual check passed)" or "(N of M independent fixes agree; visual check inconclusive)".
3. `glint/gates.py`, G3: a console error is ignored only when it is the browser's failed load of
   `/favicon.ico`. Nothing else is ignored.

## What did not change

`eval/FROZEN.md`, `eval/PREDICTIONS.md`, everything under `eval/corpus`, all thresholds and gate questions.

## How v2 numbers are produced

"Glint v2 (derived from v1 runs)" recounts the saved v1 Glint runs under the new selection rule, using each
run's saved candidates and gate results. No model is called again. Any fix that v2 claims and v1 did not is
scored by the same oracle, after the same controls.

## Known weakness of the rule

Agreement between candidates is not independent evidence in the way G4 is: the four candidates come from the
same model reading the same bug report, so they can agree on the same wrong fix. G2 and G3 still apply.
