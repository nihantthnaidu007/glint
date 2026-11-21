#!/usr/bin/env bash
set -euo pipefail

# ruff fails on a missing path, so only lint the directories that exist yet.
lint_dirs=()
for d in glint eval scripts; do
  [ -d "$d" ] && lint_dirs+=("$d")
done
ruff check "${lint_dirs[@]}"
pytest -q tests
python scripts/smoke.py --quick

CORPUS_VIDEO=eval/corpus/todomvc/m001/video_r0.webm
if [ -f "$CORPUS_VIDEO" ] && [ -f glint/pipeline.py ]; then
  python -m glint.pipeline --video "$CORPUS_VIDEO" --app eval/corpus/todomvc/m001/app --app-id todomvc --expect-any
else
  echo "skip pipeline run: corpus video or glint/pipeline.py not present yet"
fi
