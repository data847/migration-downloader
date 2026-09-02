#!/usr/bin/env bash
# Build the image. Refreshes vendor/datalabs_paths.py first so the container and
# the host share one definition of the three invariants (see bootstrap.py).
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f ../datalabs_paths.py ]]; then
  cp ../datalabs_paths.py vendor/datalabs_paths.py
  echo "refreshed vendor/datalabs_paths.py from the workspace root"
fi
docker build -t migration-downloader:latest .
echo
echo "run it:  docker compose up -d   →  http://127.0.0.1:8765"
