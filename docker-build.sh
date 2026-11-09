#!/usr/bin/env bash
# Build the image. Refreshes vendor/datalabs_paths.py first so the container and
# the host share one definition of the three invariants (see bootstrap.py).
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f ../datalabs_paths.py ]]; then
  cp ../datalabs_paths.py vendor/datalabs_paths.py
  echo "refreshed vendor/datalabs_paths.py from the workspace root"
fi
# The compose bind mounts: a missing ../.env would be created by Docker as a
# root-owned *directory* and then fail to mount as a file, and the container
# user (uid 10001, or MD_UID) must be able to write ../outputs.
if [[ -d ../.env ]]; then
  echo "error: ../.env is a directory (Docker created it because the file was missing)." >&2
  echo "       remove it, then create the file:  rmdir ../.env && touch ../.env" >&2
  exit 1
fi
if [[ ! -f ../.env ]]; then
  echo "error: ../.env does not exist; create it first (touch ../.env, then add GITHUB_TOKEN / GITLAB_TOKEN)" >&2
  exit 1
fi
mkdir -p ../outputs
if [[ ! -w ../outputs ]] || [[ "$(stat -c %u ../outputs)" != "${MD_UID:-10001}" && "$(stat -c %a ../outputs | tail -c 2)" != "77" ]]; then
  echo "note: the container runs as uid ${MD_UID:-10001}; ensure it can write ../outputs, either" >&2
  echo "      chown -R ${MD_UID:-10001} ../outputs   or   MD_UID=\$(id -u) MD_GID=\$(id -g) docker compose up -d" >&2
fi
docker build -t migration-downloader:latest .
echo
echo "run it:  docker compose up -d   →  http://127.0.0.1:8765"
