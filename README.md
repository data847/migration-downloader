# migration-downloader

UI + CLI for the flow in *Coding Pilot – Upload Instructions v1.0*: pull a
repository out of GitHub or GitLab as a migration/export tarball.

Step 1 of the document (creating the PAT in the web UI) stays manual — it can't
be automated. Steps 2–4 are what this component does, per target, with polling
and retry-free error reporting:

| Step | GitHub (user) | GitHub (org) | GitLab |
|---|---|---|---|
| 2 initiate | `POST /user/migrations` | `POST /orgs/:org/migrations` | `POST /projects/:path/export` |
| 3 poll | `GET /user/migrations/:id` → `state` | `GET /orgs/:org/migrations/:id` | `GET /projects/:path/export` → `export_status` |
| 4 download | `GET …/:id/archive` | `GET …/:id/archive` | `GET /projects/:id/export/download` |

`X-GitHub-Api-Version` defaults to `2022-11-28` for user scope and `2026-03-10`
for org scope, exactly as the document specifies; both are overridable.

## Run the UI

```bash
./run.sh                       # http://127.0.0.1:8765
./run.sh --port 9000
```

First run creates `.venv` (bundled `uv`, Python 3.12, Flask only — the API
clients are stdlib). Inside the DataLabs workspace it is also registered in
`.claude/launch.json` as `migration-downloader`.

The page: pick GitHub/GitLab, user/org, paste repos one per line, start. The
run log streams live (POST → state transitions → download progress), then each
archive appears with its size and SHA-256.

### Downloading

Archives are on disk under `outputs/migration-downloader/<run>/` either way,
but the UI can hand them straight to the browser:

- **Download** per archive, in the Archives table;
- **Download all (.zip)** for a whole run — the tarballs are bundled *stored*,
  not recompressed, so it is a fast repackage rather than a second squeeze;
- **.zip** per past run, in the Output folders table.

### Runs survive reloads and restarts

Every run's state and full log are written to
`outputs/migration-downloader/.jobs/` as it progresses:

- **Reload the page** mid-run and it re-attaches: the log replays and keeps
  streaming, download buttons appear when it finishes.
- **Restart the server** (or the container) and the run is restored as
  `interrupted` — the archive is still building on GitHub/GitLab regardless of
  what happens here. Press **Resume this run** and it polls the *same*
  migration id (step 2 skipped), reuses the same output folder, and skips any
  archive already downloaded. Nothing is re-exported needlessly.
- The **Runs** table lists every run with its state; **View** reopens any log.

## Docker

```bash
./docker-build.sh              # or: docker compose build
docker compose up -d           # http://127.0.0.1:8765
docker compose logs -f
```

The image is `python:3.12-slim`, runs as non-root uid 10001, and carries no
secrets — both invariants arrive as mounts, declared in `docker-compose.yml`:

| Host | Container | |
|---|---|---|
| `../outputs` | `/data/outputs` | archives, manifests, job records |
| `../.env` | `/app/secrets/.env` | read-only |

`DATALABS_OUTPUTS_DIR` and `DATALABS_ENV_FILE` point at those paths, so job
records land on the host and survive `docker compose down`. Standalone (outside
the DataLabs tree) mount your own:

```bash
docker run -d -p 8765:8765 \
  -v "$PWD/archives:/data/outputs" \
  -v "$PWD/.env:/app/secrets/.env:ro" \
  migration-downloader:latest
```

The CLI works in the container too:

```bash
docker exec migration-downloader python cli.py github --scope org --org TEST_ORG guava
```

## CLI

```bash
./.venv/bin/python cli.py github --scope user google/guava
./.venv/bin/python cli.py github --scope org --org TEST_ORG guava
./.venv/bin/python cli.py gitlab google/guava --poll-interval 20
./.venv/bin/python cli.py github --file ../github_repos.txt --single-archive
```

Exit status: 0 all archives downloaded, 1 some failed, 2 bad invocation.

## Invariants (DataLabs workspace)

- **Tokens** come from `DataLabs/.env` (`GH_TOKEN`/`GITHUB_TOKEN`,
  `GITLAB_TOKEN`) via `datalabs_paths`; a PAT typed into the UI overrides for
  that run only and is never persisted or logged (only masked).
- **Outputs**: `outputs/migration-downloader/<run>/` — the tarballs plus a
  `manifest.json` per run (targets, migration ids, sizes, checksums, errors),
  and `.jobs/` for the run records. Nothing is written anywhere else; no clone
  store is touched.
- `bootstrap.py` resolves `datalabs_paths` from the workspace root first and
  `vendor/` only as a fallback, so the vendored copy the image needs can never
  shadow the real one. `./docker-build.sh` refreshes it.

## Notes

- Each target gets its own migration (one tarball per repo) unless
  `--single-archive` / the matching UI checkbox is set, which puts every repo in
  one GitHub migration and one tarball.
- Org migrations need the PAT to hold `admin:org` and the user to be an Owner or
  hold the Migrator role.
- Archives are GitHub/GitLab *migration* tarballs, not git clones — unpack them
  before feeding anything that expects a working tree.
- `--api-base` targets GitHub Enterprise or a self-hosted GitLab.
- Downloads stream to `<name>.tar.gz.part` and are renamed on completion, so a
  killed run never leaves a truncated file that looks finished.
