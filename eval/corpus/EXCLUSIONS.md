# Corpus exclusions

Decided on Nov 21 2026 at 14:24 local time, BEFORE any scored run. No scored result was looked at when this list was made.

## Rule

A mutant is excluded from scoring (its `meta.json` has `"split": null`, so it is in neither dev nor test) if either:

1. the bug is not visible on screen in its recording (a video-only system has nothing to see), or
2. it is not detected by the stabilized oracle, i.e. `differential(apps/todomvc, <mutant>/app, 200, seed=10000)` returns `None`.

Only the `split` field was changed. The corpus and the videos were not regenerated.

## Excluded mutants

| id | previous split | reason |
| --- | --- | --- |
| m008 | dev | bug is not visible on screen in the recording; also not detected by the stabilized oracle (200 sequences, seed 10000) |
| m014 | test | bug is not visible on screen in the recording; also not detected by the stabilized oracle (200 sequences, seed 10000) |
| m019 | test | bug is not visible on screen in the recording (still detected by the oracle: seed 10000, step 7) |

## Resulting counts

28 mutants total: 7 dev, 18 test, 3 excluded.
