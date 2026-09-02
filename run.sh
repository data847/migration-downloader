#!/usr/bin/env bash
# Launch the migration-downloader web UI (http://127.0.0.1:8765).
#
# Creates .venv on first run: the bundled DataLabs uv if this is a checkout
# inside that workspace, uv from PATH if you have it, otherwise plain
# python3 -m venv. Flask is the only dependency; the API clients are stdlib.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "creating .venv…"
  if [[ -x ../.tools/uv ]]; then
    ../.tools/uv venv .venv -p 3.12
    ../.tools/uv pip install --python .venv/bin/python -r requirements.txt
  elif command -v uv >/dev/null 2>&1; then
    uv venv .venv
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    "${PYTHON:-python3}" -m venv .venv
    .venv/bin/python -m pip install -q --upgrade pip
    .venv/bin/python -m pip install -q -r requirements.txt
  fi
fi

# Tokens are optional at startup — one can be pasted into the UI instead.
env_file="${DATALABS_ENV_FILE:-$(cd .. 2>/dev/null && pwd)/.env}"
if [[ ! -f "$env_file" ]]; then
  echo "note: no env file at $env_file — paste a PAT in the UI, or set"
  echo "      DATALABS_ENV_FILE=/path/to/.env with GITHUB_TOKEN / GITLAB_TOKEN"
fi

exec .venv/bin/python app.py "$@"
