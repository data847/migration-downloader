"""Bitbucket support: Cloud collectors and the Data Center export job.

Bitbucket Cloud has no export archive at all (no export/import/archive path in
its OpenAPI), so everything here is a collector made of REST calls. (Pull-request
refs are not in a Cloud mirror, so PR data has to come from REST anyway.)

Bitbucket Data Center does have an export job, but it writes a `.tar` into the
server's shared home directory, so this tool can start and monitor it but not
download the result.

Issues and wikis were removed from Bitbucket Cloud (Aug 2026), so there are no
collectors for them. Comment, activity and task endpoints are intentionally
not collected.
"""

from __future__ import annotations

import time
import urllib.parse
from pathlib import Path
from typing import Callable, Iterable, List

from migration_api import MigrationError, _download, _json_request, brief_error, safe_name
from supplementary import (
    Ctx,
    collector,
    fetch_file,
    get_json,
    try_json,
)

BITBUCKET_CLOUD_BASE = "https://api.bitbucket.org/2.0"
PR_STATES = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")


def cloud_headers(token: str) -> dict:
    """`email:token` -> Basic; a bare token -> Bearer (access tokens)."""
    import base64
    if ":" in token:
        auth = "Basic " + base64.b64encode(token.encode()).decode()
    else:
        auth = f"Bearer {token}"
    return {"Authorization": auth, "Accept": "application/json",
            "User-Agent": "DataLabs-migration-downloader"}


def cloud_paged(url: str, headers: dict, *, cap: int = 200, pagelen: int = 100) -> list:
    """Bitbucket pages with `next` links in the body, not page numbers."""
    items: list = []
    sep = "&" if "?" in url else "?"
    nxt = f"{url}{sep}pagelen={pagelen}"
    while nxt and len(items) < cap:
        data = get_json(nxt, headers)
        items.extend(data.get("values", []) if isinstance(data, dict) else [])
        nxt = data.get("next") if isinstance(data, dict) else None
    return items[:cap]


def bb_url(ctx: Ctx, suffix: str = "") -> str:
    ws, repo = (ctx.target.split("/", 1) + [""])[:2]
    return f"{ctx.api_base}/repositories/{ws}/{repo}{suffix}"


def bb_workspace(ctx: Ctx) -> str:
    return ctx.target.split("/", 1)[0]


@collector("bitbucket", "repo-metadata")
def bb_repo_metadata(ctx):
    return [ctx.save_json("repo-metadata", get_json(bb_url(ctx), ctx.headers))]


@collector("bitbucket", "refs")
def bb_refs(ctx):
    return [ctx.save_json("refs", cloud_paged(bb_url(ctx, "/refs"), ctx.headers, cap=ctx.max_items))]


@collector("bitbucket", "commits")
def bb_commits(ctx):
    commits = cloud_paged(bb_url(ctx, "/commits"), ctx.headers, cap=ctx.max_items)
    ctx.shared.setdefault("bb_commits", {})[ctx.target] = [c["hash"] for c in commits]
    return [ctx.save_json("commits", commits)]


def _commit_hashes(ctx: Ctx) -> list:
    known = ctx.shared.get("bb_commits", {}).get(ctx.target)
    if known is not None:
        return known
    return [c["hash"] for c in cloud_paged(bb_url(ctx, "/commits"), ctx.headers, cap=ctx.max_items)]


@collector("bitbucket", "commit-statuses")
def bb_commit_statuses(ctx):
    out = {}
    for sha in _commit_hashes(ctx):
        ctx.check_cancelled()
        out[sha] = try_json(ctx, bb_url(ctx, f"/commit/{sha}/statuses?pagelen=100"))
    return [ctx.save_json("commit-statuses", out)]


@collector("bitbucket", "commit-diffstat")
def bb_commit_diffstat(ctx):
    out = {}
    for sha in _commit_hashes(ctx):
        ctx.check_cancelled()
        out[sha] = try_json(ctx, bb_url(ctx, f"/diffstat/{sha}?pagelen=100"))
    return [ctx.save_json("commit-diffstat", out)]


@collector("bitbucket", "commit-patches", heavy=True)
def bb_commit_patches(ctx):
    written = []
    for sha in _commit_hashes(ctx):
        ctx.check_cancelled()
        try:
            written.append(fetch_file(ctx, bb_url(ctx, f"/patch/{sha}"), f"commit-patches/{sha}.patch"))
        except MigrationError as exc:
            ctx.log(f"    {sha[:10]}: {brief_error(exc)}")
    return written


def _prs(ctx: Ctx) -> list:
    cached = ctx.shared.setdefault("bb_prs", {})
    if ctx.target not in cached:
        states = "&".join(f"state={s}" for s in PR_STATES)
        cached[ctx.target] = cloud_paged(bb_url(ctx, f"/pullrequests?{states}"), ctx.headers,
                                         cap=ctx.max_items)
    return cached[ctx.target]


@collector("bitbucket", "pullrequests")
def bb_pullrequests(ctx):
    """List plus per-PR detail, commits and statuses (about 4 calls per PR)."""
    prs = _prs(ctx)
    detail = {}
    for pr in prs:
        ctx.check_cancelled()
        pid = pr["id"]
        detail[str(pid)] = {
            "pullrequest": try_json(ctx, bb_url(ctx, f"/pullrequests/{pid}")),
            "commits": try_json(ctx, bb_url(ctx, f"/pullrequests/{pid}/commits?pagelen=100")),
            "statuses": try_json(ctx, bb_url(ctx, f"/pullrequests/{pid}/statuses?pagelen=100")),
        }
    return [ctx.save_json("pullrequests", prs), ctx.save_json("pullrequest-details", detail)]


@collector("bitbucket", "pr-diffs", heavy=True)
def bb_pr_diffs(ctx):
    written = []
    for pr in _prs(ctx):
        ctx.check_cancelled()
        pid = pr["id"]
        for kind in ("diff", "patch"):
            try:
                written.append(fetch_file(ctx, bb_url(ctx, f"/pullrequests/{pid}/{kind}"),
                                          f"pr-diffs/{pid}.{kind}"))
            except MigrationError as exc:
                ctx.log(f"    PR {pid} {kind}: {brief_error(exc)}")
        written.append(ctx.save_json(f"pr-diffs/{pid}-diffstat",
                                     try_json(ctx, bb_url(ctx, f"/pullrequests/{pid}/diffstat?pagelen=100"))))
    return written


@collector("bitbucket", "branch-restrictions")
def bb_branch_restrictions(ctx):
    return [ctx.save_json("branch-restrictions", cloud_paged(
        bb_url(ctx, "/branch-restrictions"), ctx.headers, cap=ctx.max_items))]


@collector("bitbucket", "branching-model")
def bb_branching_model(ctx):
    return [ctx.save_json("branching-model", get_json(bb_url(ctx, "/branching-model"), ctx.headers))]


@collector("bitbucket", "downloads")
def bb_downloads(ctx):
    return [ctx.save_json("downloads", cloud_paged(bb_url(ctx, "/downloads"), ctx.headers, cap=ctx.max_items))]


@collector("bitbucket", "snippets")
def bb_snippets(ctx):
    """Workspace-level, so once per workspace however many repos are targeted."""
    ws = bb_workspace(ctx)
    done = ctx.shared.setdefault("bb_snippets", set())
    if ws in done:
        return []
    base = f"{ctx.api_base}/snippets/{ws}"
    snippets = cloud_paged(base, ctx.headers, cap=ctx.max_items)
    detail = {}
    for snip in snippets:
        sid = snip["id"]
        files = {}
        for fname in (snip.get("files") or {}):
            try:
                files[fname] = fetch_file(
                    ctx, f"{base}/{sid}/files/{urllib.parse.quote(fname, safe='')}",
                    f"snippets-{safe_name(ws)}/{sid}/{safe_name(fname)}")
            except MigrationError as exc:
                files[fname] = {"error": brief_error(exc)}
        detail[sid] = {"commits": try_json(ctx, f"{base}/{sid}/commits"), "files": files}
    done.add(ws)
    return [ctx.save_json(f"snippets-{safe_name(ws)}", snippets),
            ctx.save_json(f"snippet-details-{safe_name(ws)}", detail)]


@collector("bitbucket", "hooks")
def bb_hooks(ctx):
    written = [ctx.save_json("hooks", cloud_paged(bb_url(ctx, "/hooks"), ctx.headers, cap=ctx.max_items))]
    ws = bb_workspace(ctx)
    done = ctx.shared.setdefault("bb_ws_hooks", set())
    if ws not in done:
        written.append(ctx.save_json(f"workspace-hooks-{safe_name(ws)}", cloud_paged(
            f"{ctx.api_base}/workspaces/{ws}/hooks", ctx.headers, cap=ctx.max_items)))
        done.add(ws)
    return written


@collector("bitbucket", "pipelines")
def bb_pipelines(ctx):
    pipelines = cloud_paged(bb_url(ctx, "/pipelines/?sort=-created_on"), ctx.headers, cap=ctx.max_items)
    steps = {}
    for pipe in pipelines:
        ctx.check_cancelled()
        steps[pipe["uuid"]] = try_json(ctx, bb_url(ctx, f"/pipelines/{pipe['uuid']}/steps/?pagelen=100"))
    ctx.shared.setdefault("bb_steps", {})[ctx.target] = steps
    return [
        ctx.save_json("pipelines-config", try_json(ctx, bb_url(ctx, "/pipelines_config"))),
        ctx.save_json("pipelines", pipelines),
        ctx.save_json("pipeline-steps", steps),
        # variable names only: secured values are never returned
        ctx.save_json("pipeline-variables", try_json(ctx, bb_url(ctx, "/pipelines_config/variables"))),
    ]


@collector("bitbucket", "pipeline-logs", heavy=True)
def bb_pipeline_logs(ctx):
    steps = ctx.shared.get("bb_steps", {}).get(ctx.target, {})
    written = []
    for pipe_uuid, listing in steps.items():
        for step in (listing.get("values", []) if isinstance(listing, dict) else []):
            ctx.check_cancelled()
            try:
                written.append(fetch_file(
                    ctx, bb_url(ctx, f"/pipelines/{pipe_uuid}/steps/{step['uuid']}/log"),
                    f"pipeline-logs/{safe_name(pipe_uuid)}-{safe_name(step['uuid'])}.log"))
            except MigrationError as exc:
                ctx.log(f"    step {step['uuid']}: {brief_error(exc)}")
    return written


@collector("bitbucket", "forks")
def bb_forks(ctx):
    return [ctx.save_json("forks", cloud_paged(bb_url(ctx, "/forks"), ctx.headers, cap=ctx.max_items))]


# ── Bitbucket Data Center ────────────────────────────────────────────────────


class BitbucketDCExport:
    """The Data Center migration export job. The output `.tar` lands in
    `$BITBUCKET_SHARED_HOME/data/migration/export/` on the server."""

    TERMINAL_OK = {"COMPLETED"}
    TERMINAL_BAD = {"FAILED", "ABORTED"}

    def __init__(self, token: str, api_base: str, log: Callable[[str], None] = lambda _m: None):
        if not token:
            raise MigrationError("a Bitbucket Data Center token is required")
        if not api_base:
            raise MigrationError("a Bitbucket Data Center base URL is required (--api-base)")
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.log = log

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                "User-Agent": "DataLabs-migration-downloader"}

    @property
    def _root(self) -> str:
        return f"{self.api_base}/rest/api/1.0/migration/exports"

    @staticmethod
    def request_body(targets: Iterable[str]) -> dict:
        includes = []
        for raw in targets:
            key, _sep, slug = raw.strip().strip("/").partition("/")
            if not key or not slug:
                raise MigrationError(f"'{raw}' should be PROJECTKEY/repo-slug")
            includes.append({"projectKey": key, "slug": slug})
        if not includes:
            raise MigrationError("no repositories given")
        return {"repositoriesRequest": {"includes": includes}}

    def preview(self, targets: Iterable[str]) -> dict:
        self.log(f"  POST {self._root}/preview")
        return _json_request(f"{self._root}/preview", method="POST", headers=self._headers,
                             payload=self.request_body(targets))[1]

    def start(self, targets: Iterable[str]) -> str:
        self.log(f"  POST {self._root}")
        _s, data = _json_request(self._root, method="POST", headers=self._headers,
                                 payload=self.request_body(targets))
        job_id = data.get("id")
        if not job_id:
            raise MigrationError(f"no export job id in response: {str(data)[:300]}")
        return str(job_id)

    def status(self, job_id: str) -> dict:
        return _json_request(f"{self._root}/{job_id}", headers=self._headers)[1]

    def messages(self, job_id: str) -> list:
        data = _json_request(f"{self._root}/{job_id}/messages", headers=self._headers)[1]
        return data.get("values", []) if isinstance(data, dict) else []

    def cancel(self, job_id: str) -> dict:
        self.log(f"  POST {self._root}/{job_id}/cancel")
        return _json_request(f"{self._root}/{job_id}/cancel", method="POST", headers=self._headers)[1]

    def wait(self, job_id: str, *, interval: int = 15, timeout: int = 3600, cancelled=None) -> dict:
        deadline = time.time() + timeout
        last = ""
        while True:
            if cancelled and cancelled():
                raise MigrationError("cancelled while waiting for the export")
            data = self.status(job_id)
            state = str(data.get("state", "?")).upper()
            if state != last:
                self.log(f"  state = {state}")
                last = state
            if state in self.TERMINAL_OK:
                return data
            if state in self.TERMINAL_BAD:
                raise MigrationError(f"export {job_id} ended in state '{state}'")
            if time.time() > deadline:
                raise MigrationError(f"timed out after {timeout}s waiting for export {job_id}")
            for _ in range(max(1, interval)):
                if cancelled and cancelled():
                    break
                time.sleep(1)

    def repo_archive(self, target: str, dest: Path) -> Path:
        key, _sep, slug = target.strip("/").partition("/")
        url = (f"{self.api_base}/rest/api/1.0/projects/{urllib.parse.quote(key)}"
               f"/repos/{urllib.parse.quote(slug)}/archive?format=zip")
        return _download(url, dest, headers=self._headers, log=self.log)


@collector("bitbucket-dc", "archive", heavy=True)
def dc_archive(ctx):
    """Snapshot zip of one repo's default ref (no history, no PR data)."""
    client = BitbucketDCExport(ctx.token, ctx.api_base, log=ctx.log)
    dest = ctx.out / "repo-snapshot.zip"
    client.repo_archive(ctx.target, dest)
    return [dest.name]
