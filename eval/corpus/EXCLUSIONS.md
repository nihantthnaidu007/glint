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

## Excluded clean videos

Decided on Nov 21 2026 at 14:59 local time, BEFORE any scored run. No scored result was looked at.

Rule: a clean video must show the unmutated app behaving correctly. The vendored TodoMVC app has a real
quirk in its toggle-all control (`view.js` sets the checked state on the label, not on the checkbox input, so
the arrow's state can disagree with the list). A recording that uses toggle-all can therefore show incorrect
behavior of the unmutated app, so it is not a clean video. Each recording's actions were replayed on a page
that was not recorded, counting real clicks on the toggle-all control. No model was involved.

| id | toggle-all clicks in the recording | decision |
| --- | --- | --- |
| c01 | 3 | excluded |
| c02 | 1 | excluded |
| c03 | 2 | excluded |
| c04 | 0 | kept |
| c05 | 2 | excluded |

Excluded clean videos carry a `meta.json` with `"split": null`, which `eval/run_eval.py` skips.
Clean videos scored: 1 (c04).

For information only, not an exclusion: 11 of the 18 in-split test mutant recordings also include a
toggle-all action (counted from the stored action sequences, never given to any model). They stay in the
test split, because the mutant's own bug is visible in them.
