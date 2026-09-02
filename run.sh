#!/usr/bin/env bash
# Launch the migration-downloader web UI.
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "creating .venv…"
  ../.tools/uv venv .venv -p 3.12
  ../.tools/uv pip install --python .venv/bin/python flask
fi
exec .venv/bin/python app.py "$@"
