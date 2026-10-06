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

## Step 1: the token

The one manual step, straight from the document. Create a PAT with:

| | Scopes |
|---|---|
| GitHub, personal repos | `repo` + `user` |
| GitHub, org repos | `repo` + `admin:org` — and you must be an org **Owner** or hold the [Migrator role](https://docs.github.com/en/migrations/ado/granting-the-migrator-role) |
| GitLab | `api` + `write_repository` (a *legacy* token on GitLab.com) |

Then either paste it into the UI (used for that run only, never stored), or put
it in an env file so you never paste it again:

```bash
printf 'GITHUB_TOKEN=ghp_…\nGITLAB_TOKEN=glpat-…\n' > .env && chmod 600 .env
DATALABS_ENV_FILE="$PWD/.env" ./run.sh
```

Inside the DataLabs workspace this is already handled — the one `.env` at the
workspace root is picked up automatically.

## Run the UI

```bash
./run.sh                       # http://127.0.0.1:8765
./run.sh --port 9000
```

Requires Python 3.9+ and nothing else. First run creates `.venv` — the DataLabs
bundled `uv` if present, `uv` from PATH if you have it, otherwise
`python3 -m venv` — and installs Flask, the only dependency (the API clients
are stdlib). Inside the DataLabs workspace it is also registered in
`.claude/launch.json` as `migration-downloader`.

The page: pick GitHub/GitLab, pick the host, pick user/org, choose the repos,
start. The run log streams live (POST → state transitions → download
progress), then each archive appears with its size and SHA-256.

### Host — github.com, GitLab.com, or your own

The **Host** row under the provider toggle is the whole story: `github.com` /
`gitlab.com`, or **Self-hosted**, which reveals an API base URL field.

- GitHub Enterprise: the API root, usually `https://<host>/api/v3`
- Self-hosted GitLab: the instance root, e.g. `https://gitlab.example.com` —
  `/api/v4` is appended for you

Everything honours it: discovery, initiate, poll and download. Nested subgroups
(`group/subgroup/project`) and numeric project IDs work. Two things about a
private instance are worth knowing: **project export must be enabled**
server-side (*Admin → Settings → General → Import and export settings*), and a
**private CA** needs to be trusted — `urllib` uses Python's default TLS
context, so export the bundle before launching:

```bash
SSL_CERT_FILE=/path/to/internal-ca.pem ./run.sh
```

(For the container, mount the PEM and set `SSL_CERT_FILE` in
`docker-compose.yml`.)

### Choosing repos instead of typing them

**Browse & select…** lists what the token can actually see, with a filter box,
select-all and private/archived badges:

| | GitHub | GitLab |
|---|---|---|
| Owners | — (one list covers them) | **Browse groups** → groups you hold Maintainer+ in |
| Repos | one list: repos you own or collaborate on plus every org repo you can reach (`/user/repos?affiliation=owner,collaborator,organization_member`) | projects you could export, newest activity first |

GitHub has no user/org switch any more: targets are always `owner/repo`, and
the default `--scope auto` decides per repo. Repos owned by the token's own
login go through `/user/migrations`; every other owner's go through that org's
`/orgs/{org}/migrations`, one group per owner (so `--single-archive` bundles
within an owner, never across). `--scope user` / `--scope org --org X` still
force the old behaviour, and org scope still takes bare names.

Selected entries are appended to the target list as `owner/repo`
(GitHub) or `group/subgroup/project` (GitLab). GitLab discovery filters to Maintainer
(access level 40) or above, since that is the floor for exporting a project, so
the list holds no rows that would 403 on initiate. Typing targets by hand still
works; the picker only fills the box.

### Downloading

Archives are on disk under `outputs/migration-downloader/<run>/` either way,
but the UI can hand them straight to the browser:

- **Download** per archive, in the Archives table;
- **Download all (.zip)** for a whole run — the tarballs are bundled *stored*,
  not recompressed, so it is a fast repackage rather than a second squeeze;
- **.zip** per past run, in the Output folders table.

### Downloading the log

**⬇ Log** in the run-log header (and per row in the Runs table) saves the run
as a text file — the natural thing to attach when a run fails.

It is scrubbed on the way out by [`redact.py`](redact.py): GitHub and GitLab
token shapes, `Authorization` / `PRIVATE-TOKEN` / `Bearer` values, credentials
embedded in URLs, `token=`-style query parameters, and other common key shapes
(AWS, Slack, JWTs, `sk-…`) are replaced with `<redacted-…>` markers, and the
home directory collapses to `~` so the log does not name the machine's user.
The log carries API URLs, states, sizes, checksums and file names — never
repository content, and never a token in the clear (the runner only ever logs
the masked form).

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

## `for-check.csv` (for Garmr)

Next to every archive the run also writes `<archive>.for-check.csv` (for example `acme-widgets-42.tar.gz` and
`acme-widgets-42.for-check.csv`): facts the archive cannot tell about itself, so a checker can verify it is complete
without a token or network. Long format, columns `repo,section,key,value`; a blank value means unknown, never a guess.

| Section | Contents |
|---|---|
| `meta` | provider, host, scope, org, migration/export id, final state, API version, start/finish time, archive name, bytes, SHA-256, `Content-Length` the server reported, request options |
| `requested_repo` / `export_repo` | repos asked for, and (GitHub) the repos `GET .../migrations/:id/repositories` says the export contains |
| `counts_before` / `counts_after` | PRs/MRs, issues, labels, milestones, releases, branches, tags, commits on the default branch, issue/review/commit comments, collaborators, default branch, taken just before the export started and just after it finished (GitHub: one GraphQL call plus REST counts; GitLab: `X-Total` headers and `?statistics=true`) |
| `ref_before` / `ref_after` | branches, tags and PR/MR head refs on the source with their shas, from `git ls-remote --refs` (the token goes in the environment, never on the command line) |
| `file` / `file_summary` | every entry in the downloaded archive outside `<repo>.git/objects/` with its size, plus the entry count and bytes of the objects |
| `error` | anything that could not be collected; collection failures never fail the download |

Disable with `--no-for-check` (CLI) or `for_check=false` in the job spec. Counts are two snapshots because things change while
an export runs; a checker should only fail when the archive holds fewer than the lower of the two.
## Extras: what the archives leave out

`--extras` runs read-only collectors after the export flow and writes them
under `<run>/extras/<target>/`. **The web UI runs every collector by default**
(`everything`) when you press Start; the CLI runs only what you ask for.
Names go in a comma list;
`all` runs every light collector, `everything` adds the heavy ones (wikis,
CI logs). `--list-extras` prints the names per provider. A
collector that fails (403, feature disabled) is recorded in `manifest.json`
under `extras` and the rest carry on. `--max-items` is how many items each
listing collects (default 200, `0` = no limit); a listing cut at the limit is
reported as an issue, never silently.

In the web UI, the word "download" in the page header opens a dialog where
extras can be unticked for the next runs (the choice and the item limit are
remembered in the browser). Leaving everything ticked is the default.

| Provider | Light collectors | Heavy (name them or `everything`) |
|---|---|---|
| `github` | `repo-metadata` `releases` `actions` `actions-artifacts` `hooks` `branch-protection` `dependabot-alerts` `code-scanning` `secret-scanning` `projects-v2` `packages` | `wiki` `actions-logs` |
| `gitlab` | `repo-archive` `wiki-pages` `releases` `pipelines` `hooks` | `wiki` `job-traces` `job-artifacts` `relations-export` |
| `bitbucket` (Cloud) | `repo-metadata` `refs` `commits` `commit-statuses` `commit-diffstat` `pullrequests` `branch-restrictions` `branching-model` `downloads` `snippets` `hooks` `pipelines` `forks` | `commit-patches` `pr-diffs` `pipeline-logs` |
| `bitbucket-dc` | | `archive` |

Not collected on purpose: issue/PR/commit comments, discussions, activity and
tasks.

Extra token needs beyond the export scopes: GitHub `admin:repo_hook` (or
`read:repo_hook`), `security_events`, `read:project`, `read:packages`; GitLab
Maintainer on the project (for `hooks`). Bitbucket Cloud takes a user API token
(an `email:token` pair or a bare access token, `BITBUCKET_TOKEN` in the env
file) with `read:repository`, `read:pullrequest`, `read:pipeline`,
`admin:repository` (branch restrictions, pipelines config), `read:webhook` and
`read:snippet`.

`wiki` shells out to `git` (the image installs it). Credentials are passed
through git's environment config, never the URL or argv.

Bitbucket Cloud has no export archive, so `cli.py bitbucket ws/repo --extras all`
is extras-only. Bitbucket Data Center (`bitbucket-dc PROJ/repo --api-base URL`,
token `BITBUCKET_DC_TOKEN`) starts and monitors the server-side export job
(`--dc-action export|preview|cancel|none`, `--dc-job-id` for cancel); the
resulting `.tar` is written to the server's shared home, not downloaded here.

In the web UI, one **Bitbucket** button covers both products, the way GitLab
covers gitlab.com and self-hosted: host `bitbucket.org` is Cloud (extras only),
and `Data Center (self-hosted)` takes a base URL plus an export-job selector
(start, preview, cancel or none). A run's extras and export jobs show in
their own results card. The repo/org browser is GitHub and GitLab only.

Additional export options:

- GitHub: `--exclude metadata,git_data,attachments,releases,owner_projects`,
  `--org-metadata-only`; after a successful download `--unlock-repos` (with
  `--lock-repositories`) and `--delete-archive` (irreversible, off by default).
- GitLab: `--upload-url`/`--upload-method`/`--description` make GitLab push the
  export to a URL itself (nothing is downloaded; only the host is logged).

## Downloading a run

**Download everything (.zip)** returns a zip of zips: one `<target>.zip` per
target holding its archive, its `.for-check.csv` and its `extras/` files, plus
`run-files.zip` (manifest and anything else at the top of the run). Runs with
only extras (Bitbucket Cloud) download the same way. The zip is built in a
temp file one target at a time, so it needs roughly the run's size in free
temp space; the request fails with a clear message if that is missing, and the
temp file is removed once the download ends or the browser disconnects.

## Issues

A run that did not go cleanly no longer says "partial". It shows **N issues**
as a button (in the run header and the run list); clicking it lists each thing
that went wrong: the target, the step, the host's own message, and in plain
words what usually causes it (missing org Owner role, SSO not authorized, a
feature switched off, a rate limit...). The same list is in `manifest.json`
under `issues`, also for runs recorded before this existed. A listing cut off
by the item limit shows up here too.

## Rate limits and retries

Every API call goes through one place that waits instead of failing: a `429`
waits `Retry-After`; a `403` that says the hourly budget or a secondary limit
is spent waits for the reset; `5xx` and dropped connections back off and retry
a few times. When a response says the budget is almost used, the next call to
that host pauses first. Waits are logged in the run and can be cancelled. A
`POST` (starting a migration) is never repeated after a `5xx` or a dropped
connection, because that could start a second one; it is only retried when the
host clearly did nothing (`429`, rate-limit `403`). A genuine permission `403`
is reported straight away.

## Paging

Listings follow the `Link: rel="next"` header the host sends, not a guessed
`?page=N` (Dependabot and secret-scanning alerts only take cursors, so
`page=2` would repeat page 1). The repo picker loads one page at a time as you
scroll, so there is no cap on how many repos it can list, and keeps loading
while a filter is active so a match on a later page is not missed. Counts show
a `+` until everything is loaded. Organizations that enforce SAML SSO only
appear once the token is authorized for them; when GitHub says results are
hidden for that reason, the picker says so.

## Secrets in collected data

Extras are scrubbed before they are written: JSON fields named like secrets
(`secret`, `token`, `password`, `api_key`, ...) are blanked, which includes the
leaked value in a secret-scanning alert, and strings go through the same
patterns as the run log (tokens, `Authorization` headers, URLs with
credentials, Slack/Discord/Teams webhook URLs, private-key blocks). CI logs
(Actions log zips, GitLab job traces, Bitbucket pipeline logs) are scrubbed
after download. Source diffs, patches and build artifacts are repository
content and are left as they are.

## Safety checks

- Repository names are validated before anything runs (letters, digits and
  `. _ - ~` separated by `/`; no `..`, spaces, `;` or `?`).
- A pagination link is only followed on the host the listing started from, so
  the token cannot be sent elsewhere; only `http(s)` URLs are ever opened.
- A file name that comes from an API response cannot write outside its target
  folder (symlinks included).
- A run refuses to start with under 512 MB free; the download zip checks its
  own space first; scrubbing a zip is skipped past 256 MB uncompressed.
- `--delete-archive` only deletes from GitHub after the downloaded file is
  exactly the size GitHub announced and opens as a tarball; otherwise it keeps
  the remote copy and says why.
- The run zip leaves out symlinks and unfinished `.part` files.

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

## Tests

```bash
python3 -m pytest tests          # offline: any real network call fails the test
```

`tests/conftest.py` blocks the network and replaces sleeping with a no-op.
`tests/test_packaging.py` fails if a module is missing from the Dockerfile
`COPY` line, which would otherwise only show up as an ImportError in the
container.
