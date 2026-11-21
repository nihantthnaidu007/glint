# Example runs

Three complete evidence bundles written by the Glint pipeline, copied unchanged from real runs on Nov 21, 2026.

| Folder | Input | Verdict |
|---|---|---|
| `visual-check-passed_m004` | bug video m004 | VERIFIED_FIX, visual check passed |
| `fixes-agree_m001` | bug video m001 | VERIFIED_FIX, 3 of 4 fixes agree, visual check inconclusive |
| `abstain_clean-video_c02` | a video of the unmodified app | ABSTAIN, no fix attempted |

In each folder: `input.webm` (the recording), `video.mp4` (normalized), `spec.json` (the bug report read from
the video), `repro_test.py` (the generated failing test), `shots/` (failing and after screenshots),
`diffs/` (each candidate fix), `frames/` (video frames at the evidence times), `events.jsonl` (every step),
`result.json` (the verdict), `usage.jsonl` (model calls and tokens).
