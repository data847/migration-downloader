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

Also registered in `.claude/launch.json` as `migration-downloader`.
First run creates `.venv` (bundled `uv`, Python 3.12, Flask only — the API
clients are stdlib).

The page: pick GitHub/GitLab, user/org, paste repos one per line, start. The
run log streams live (POST → state transitions → download progress), then each
archive appears with its size, SHA-256 and a download link.

## CLI

```bash
./.venv/bin/python cli.py github --scope user google/guava
./.venv/bin/python cli.py github --scope org --org TEST_ORG guava
./.venv/bin/python cli.py gitlab google/guava --poll-interval 20
./.venv/bin/python cli.py github --file ../github_repos.txt --single-archive
```

Exit status: 0 all archives downloaded, 1 some failed, 2 bad invocation.

## Invariants

- **Tokens** come from `DataLabs/.env` (`GH_TOKEN`/`GITHUB_TOKEN`,
  `GITLAB_TOKEN`) via `datalabs_paths`; a PAT typed into the UI overrides for
  that run only and is never persisted or logged (only masked).
- **Outputs**: `outputs/migration-downloader/<run>/` — the tarballs plus a
  `manifest.json` per run (targets, migration ids, sizes, checksums, errors).
  Nothing is written anywhere else; no clone store is touched.

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
