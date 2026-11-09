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
from safety import require_same_origin
from supplementary import (
    Ctx,
    collector,
    fetch_file,
    get_json,
    try_json,
)
import supplementary

BITBUCKET_CLOUD_BASE = "https://api.bitbucket.org/2.0"
PR_STATES = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")
PR_PAGELEN = 50  # the pull request listings answer 400 "Invalid pagelen" above 50


def _q(job_id: str) -> str:
    return urllib.parse.quote(str(job_id), safe="")


def cloud_headers(token: str) -> dict:
    """`email:token` -> Basic; a bare token -> Bearer (access tokens)."""
    import base64
    if ":" in token:
        auth = "Basic " + base64.b64encode(token.encode()).decode()
    else:
        auth = f"Bearer {token}"
    return {"Authorization": auth, "Accept": "application/json",
            "User-Agent": "DataLabs-migration-downloader"}


def cloud_paged(ctx, url: str, *, pagelen: int = 100) -> list:
    """Bitbucket pages with `next` links in the body, not page numbers.

    The link comes from the response, so it is only followed on the origin the
    listing started from; otherwise the token would go wherever it pointed.
    """
    items: list = []
    sep = "&" if "?" in url else "?"
    nxt = f"{url}{sep}pagelen={pagelen}"
    first = True
    while nxt:
        ctx.check_cancelled()
        try:
            data, _link, _hdrs = supplementary._json_page(nxt, headers=ctx.headers)
        except MigrationError as exc:
            # Each listing has its own page-size ceiling and answers 400 "Invalid pagelen" above it. Only the first page
            # carries our pagelen; later pages follow Bitbucket's own next links.
            if first and pagelen > 10 and "invalid pagelen" in str(exc).lower():
                pagelen //= 2
                nxt = f"{url}{sep}pagelen={pagelen}"
                continue
            raise
        first = False
        supplementary.require_json(data, nxt)
        values = data.get("values", []) if isinstance(data, dict) else []
        items.extend(values)
        follow = data.get("next") if isinstance(data, dict) else None
        if not follow and values and isinstance(data, dict):
            follow = _numbered_next(url, sep, data, pagelen)
        if ctx.max_items and len(items) >= ctx.max_items:
            if follow or len(items) > ctx.max_items:
                ctx.truncate(url)
            return items[:ctx.max_items]
        nxt = require_same_origin(url, follow) if follow else None
    return items


def _numbered_next(url: str, sep: str, data: dict, pagelen: int):
    """Some listings (Pipelines) answer page/size/pagelen but no `next` link; without this the listing would stop
    quietly after its first page."""
    try:
        page, size, per = int(data.get("page") or 0), int(data.get("size") or 0), int(data.get("pagelen") or pagelen)
    except (TypeError, ValueError):
        return None
    if page and size and per and page * per < size:
        return f"{url}{sep}pagelen={per}&page={page + 1}"
    return None


def _all_or_error(ctx: Ctx, url: str, what: str, pagelen: int = 100) -> dict:
    """Every page of a per-item listing, shaped like one page ({"values": [...]}). A failure is counted as a
    missing item (the run reports it) instead of sinking the collector."""
    try:
        return {"values": cloud_paged(ctx, url, pagelen=pagelen)}
    except MigrationError as exc:
        if "cancelled" in str(exc):
            raise
        return ctx.item_error(what, exc)


def _seg(value) -> str:
    """One path segment. Pipeline and step UUIDs come as {...}; braces are not safe in a URL path unencoded."""
    return urllib.parse.quote(str(value), safe="")


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
    return [ctx.save_json("refs", cloud_paged(ctx, bb_url(ctx, "/refs")))]


@collector("bitbucket", "commits")
def bb_commits(ctx):
    commits = cloud_paged(ctx, bb_url(ctx, "/commits"))
    ctx.shared.setdefault("bb_commits", {})[ctx.target] = [c["hash"] for c in commits]
    return [ctx.save_json("commits", commits)]


def _commit_hashes(ctx: Ctx) -> list:
    known = ctx.shared.get("bb_commits", {}).get(ctx.target)
    if known is not None:
        return known
    return [c["hash"] for c in cloud_paged(ctx, bb_url(ctx, "/commits"))]


# One call per commit each: on a long history these take hours under Bitbucket's hourly limit, so they are heavy
# (left out of `all`, run by `everything` or by name) and the run says up front what they cost.
@collector("bitbucket", "commit-statuses", heavy=True)
def bb_commit_statuses(ctx):
    out = {}
    for sha in _commit_hashes(ctx):
        ctx.check_cancelled()
        out[sha] = _all_or_error(ctx, bb_url(ctx, f"/commit/{_seg(sha)}/statuses"), f"commit {sha[:10]} statuses")
    return [ctx.save_json("commit-statuses", out)]


@collector("bitbucket", "commit-diffstat", heavy=True)
def bb_commit_diffstat(ctx):
    out = {}
    for sha in _commit_hashes(ctx):
        ctx.check_cancelled()
        out[sha] = _all_or_error(ctx, bb_url(ctx, f"/diffstat/{_seg(sha)}"), f"commit {sha[:10]} diffstat")
    return [ctx.save_json("commit-diffstat", out)]


@collector("bitbucket", "commit-patches", heavy=True)
def bb_commit_patches(ctx):
    written = []
    for sha in _commit_hashes(ctx):
        ctx.check_cancelled()
        try:
            written.append(fetch_file(ctx, bb_url(ctx, f"/patch/{_seg(sha)}"), f"commit-patches/{sha}.patch"))
        except MigrationError as exc:
            ctx.item_error(f"commit {sha[:10]} patch", exc)
    return written


def _prs(ctx: Ctx) -> list:
    cached = ctx.shared.setdefault("bb_prs", {})
    if ctx.target not in cached:
        try:
            cached[ctx.target] = cloud_paged(ctx, _pr_list_url(ctx, PR_STATES), pagelen=PR_PAGELEN)
        except MigrationError as exc:
            # SUPERSEDED is the newest state; if a Bitbucket ever refuses it, losing every PR would be far worse than
            # missing the superseded ones
            text = str(exc).lower()
            if "http 400" not in text or "pagelen" in text:
                raise
            ctx.log("    the PR listing refused state=SUPERSEDED; listing the other states")
            cached[ctx.target] = cloud_paged(ctx, _pr_list_url(ctx, PR_STATES[:-1]), pagelen=PR_PAGELEN)
    return cached[ctx.target]


def _pr_list_url(ctx: Ctx, states) -> str:
    return bb_url(ctx, "/pullrequests?" + "&".join(f"state={s}" for s in states))


@collector("bitbucket", "pullrequests")
def bb_pullrequests(ctx):
    """List plus per-PR detail, commits and statuses (about 4 calls per PR)."""
    prs = _prs(ctx)
    detail = {}
    for pr in prs:
        ctx.check_cancelled()
        pid = pr["id"]
        detail[str(pid)] = {
            "pullrequest": try_json(ctx, bb_url(ctx, f"/pullrequests/{pid}"), f"PR {pid}"),
            "commits": _all_or_error(ctx, bb_url(ctx, f"/pullrequests/{pid}/commits"), f"PR {pid} commits",
                                     PR_PAGELEN),
            "statuses": _all_or_error(ctx, bb_url(ctx, f"/pullrequests/{pid}/statuses"), f"PR {pid} statuses",
                                      PR_PAGELEN),
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
                ctx.item_error(f"PR {pid} {kind}", exc)
        written.append(ctx.save_json(f"pr-diffs/{pid}-diffstat",
                                     _all_or_error(ctx, bb_url(ctx, f"/pullrequests/{pid}/diffstat"),
                                                   f"PR {pid} diffstat", PR_PAGELEN)))
    return written


@collector("bitbucket", "branch-restrictions")
def bb_branch_restrictions(ctx):
    return [ctx.save_json("branch-restrictions", cloud_paged(ctx, 
        bb_url(ctx, "/branch-restrictions")))]


@collector("bitbucket", "branching-model")
def bb_branching_model(ctx):
    return [ctx.save_json("branching-model", get_json(bb_url(ctx, "/branching-model"), ctx.headers))]


@collector("bitbucket", "downloads")
def bb_downloads(ctx):
    return [ctx.save_json("downloads", cloud_paged(ctx, bb_url(ctx, "/downloads")))]


@collector("bitbucket", "snippets")
def bb_snippets(ctx):
    """Workspace-level, so once per workspace however many repos are targeted."""
    ws = bb_workspace(ctx)
    done = ctx.shared.setdefault("bb_snippets", {})
    if ws in done:      # workspace-level: collected (or failed) once, with the first repo of the workspace
        if done[ws]:
            ctx.log(f"    workspace {ws}: snippets failed earlier in this run ({done[ws]}); not retried")
        return []
    base = f"{ctx.api_base}/snippets/{_seg(ws)}"
    try:
        snippets = cloud_paged(ctx, base)
    except MigrationError as exc:
        done[ws] = brief_error(exc)
        raise
    done[ws] = ""
    detail = {}
    for snip in snippets:
        ctx.check_cancelled()
        sid = snip["id"]
        # the listing carries no file names; the snippet itself does
        full = try_json(ctx, f"{base}/{_seg(sid)}", f"snippet {sid}")
        files = {}
        for fname in ((full.get("files") if isinstance(full, dict) else None) or {}):
            try:
                files[fname] = fetch_file(
                    ctx, f"{base}/{_seg(sid)}/files/{urllib.parse.quote(fname, safe='')}",
                    f"snippets-{safe_name(ws)}/{safe_name(sid)}/{safe_name(fname)}")
            except MigrationError as exc:
                files[fname] = ctx.item_error(f"snippet {sid} file {fname}", exc)
        detail[sid] = {"snippet": full,
                       "commits": _all_or_error(ctx, f"{base}/{_seg(sid)}/commits", f"snippet {sid} commits"),
                       "files": files}
    return [ctx.save_json(f"snippets-{safe_name(ws)}", snippets),
            ctx.save_json(f"snippet-details-{safe_name(ws)}", detail)]


@collector("bitbucket", "hooks")
def bb_hooks(ctx):
    """Repo webhooks need repo admin, workspace webhooks need workspace admin: one failing never skips the other."""
    written = []
    try:
        written.append(ctx.save_json("hooks", cloud_paged(ctx, bb_url(ctx, "/hooks"))))
    except MigrationError as exc:
        if "cancelled" in str(exc):
            raise
        ctx.item_error("repository webhooks", exc)
    ws = bb_workspace(ctx)
    done = ctx.shared.setdefault("bb_ws_hooks", set())
    if ws not in done:
        done.add(ws)    # once per workspace, failed or not: it would fail the same way for every repo
        try:
            written.append(ctx.save_json(f"workspace-hooks-{safe_name(ws)}",
                                         cloud_paged(ctx, f"{ctx.api_base}/workspaces/{_seg(ws)}/hooks")))
        except MigrationError as exc:
            if "cancelled" in str(exc):
                raise
            ctx.item_error(f"workspace {ws} webhooks", exc)
    return written


def _pipeline_steps(ctx: Ctx, pipelines: list) -> dict:
    steps = {}
    for pipe in pipelines:
        ctx.check_cancelled()
        steps[pipe["uuid"]] = _all_or_error(ctx, bb_url(ctx, f"/pipelines/{_seg(pipe['uuid'])}/steps"),
                                            f"pipeline {pipe.get('build_number', pipe['uuid'])} steps")
    ctx.shared.setdefault("bb_steps", {})[ctx.target] = steps
    return steps


@collector("bitbucket", "pipelines")
def bb_pipelines(ctx):
    pipelines = cloud_paged(ctx, bb_url(ctx, "/pipelines?sort=-created_on"))
    steps = _pipeline_steps(ctx, pipelines)
    return [
        ctx.save_json("pipelines-config", try_json(ctx, bb_url(ctx, "/pipelines_config"), "pipelines config")),
        ctx.save_json("pipelines", pipelines),
        ctx.save_json("pipeline-steps", steps),
        # variable names only: secured values are never returned
        ctx.save_json("pipeline-variables", _all_or_error(ctx, bb_url(ctx, "/pipelines_config/variables"),
                                                          "pipeline variables")),
    ]


@collector("bitbucket", "pipeline-logs", heavy=True)
def bb_pipeline_logs(ctx):
    steps = ctx.shared.get("bb_steps", {}).get(ctx.target)
    if steps is None:   # run on its own, or `pipelines` failed or was skipped on a resume: find the steps here
        steps = _pipeline_steps(ctx, cloud_paged(ctx, bb_url(ctx, "/pipelines?sort=-created_on")))
    written = []
    for pipe_uuid, listing in steps.items():
        for step in (listing.get("values", []) if isinstance(listing, dict) else []):
            ctx.check_cancelled()
            try:
                written.append(fetch_file(
                    ctx, bb_url(ctx, f"/pipelines/{_seg(pipe_uuid)}/steps/{_seg(step['uuid'])}/log"),
                    f"pipeline-logs/{safe_name(pipe_uuid)}-{safe_name(step['uuid'])}.log",
                    scrub="text"))
            except MigrationError as exc:
                if "HTTP 404" in str(exc):      # a step that never ran (skipped, not started) has no log
                    continue
                ctx.item_error(f"pipeline step {step['uuid']} log", exc)
    return written


@collector("bitbucket", "forks")
def bb_forks(ctx):
    return [ctx.save_json("forks", cloud_paged(ctx, bb_url(ctx, "/forks")))]


# ── browsing repos for the picker ────────────────────────────────────────────

BROWSE_PAGE = 100


def _cloud_all(url: str, headers: dict, max_pages: int = 50) -> list:
    """Every value of a Bitbucket Cloud listing, following its `next` links on the same origin only."""
    values: list = []
    nxt = url
    for _ in range(max_pages):
        data, _link, _hdrs = supplementary._json_page(nxt, headers=headers)
        values += data.get("values", []) if isinstance(data, dict) else []
        follow = data.get("next") if isinstance(data, dict) else None
        if not follow:
            break
        nxt = require_same_origin(url, follow)
    return values


def cloud_workspaces_page(token: str, page: int = 1, api_base: str = BITBUCKET_CLOUD_BASE) -> dict:
    """The workspaces the token's user belongs to, by name, the user's own (personal) workspace last.

    Bitbucket Cloud retired its cross-workspace endpoints (CHANGE-2770); /2.0/user/workspaces is the one it keeps. The older
    /2.0/user/permissions/workspaces is tried when the new one is missing (an older or self-proxied API)."""
    base, headers = api_base.rstrip("/"), cloud_headers(token)
    try:
        values = _cloud_all(f"{base}/user/workspaces?pagelen={BROWSE_PAGE}", headers)
    except MigrationError as exc:
        if not any(code in str(exc) for code in ("HTTP 404", "HTTP 410")):
            raise
        values = _cloud_all(f"{base}/user/permissions/workspaces?pagelen={BROWSE_PAGE}", headers)
    me = ""
    try:
        data, _link, _hdrs = supplementary._json_page(f"{base}/user", headers=headers)
        me = (data or {}).get("username", "") if isinstance(data, dict) else ""
    except MigrationError:
        pass
    items, seen = [], set()
    for v in values:
        ws = v.get("workspace") if isinstance(v.get("workspace"), dict) else v
        slug = ws.get("slug") or ""
        if not slug or slug in seen:
            continue
        seen.add(slug)
        items.append({"name": slug, "label": ws.get("name") or slug, "kind": "personal" if me and slug == me else "workspace",
                      "description": ""})
    items.sort(key=lambda i: (i["kind"] == "personal", i["label"].lower()))
    return {"items": items, "has_more": False, "notes": []}


def cloud_repos_page(token: str, page: int = 1, api_base: str = BITBUCKET_CLOUD_BASE, workspace: str = "") -> dict:
    """One page of the repos inside one workspace (Browse workspaces, then click one). Bitbucket no longer lists repos across
    workspaces (GET /2.0/repositories?role=member answers 404 since CHANGE-2770), so a workspace is required."""
    if not workspace.strip("/"):
        raise MigrationError("choose a workspace first: Bitbucket Cloud no longer lists repositories across workspaces "
                             "(use Browse workspaces)")
    url = (f"{api_base.rstrip('/')}/repositories/{urllib.parse.quote(workspace.strip('/'), safe='')}?sort=-updated_on"
           f"&pagelen={BROWSE_PAGE}&page={page}")
    data, _link, _hdrs = supplementary._json_page(url, headers=cloud_headers(token))
    values = data.get("values", []) if isinstance(data, dict) else []
    return {
        "items": [{
            "target": r["full_name"], "label": r["full_name"],
            "private": bool(r.get("is_private")), "archived": False,
            "size_kb": int((r.get("size") or 0) / 1024), "updated": (r.get("updated_on") or "")[:10],
        } for r in values if r.get("full_name")],
        "has_more": bool(data.get("next")) if isinstance(data, dict) else False,
        "notes": [],
    }


def dc_repos_page(token: str, api_base: str, page: int = 1) -> dict:
    """One page of the repos the token can read on a Data Center server, as PROJECT/slug."""
    if not api_base:
        raise MigrationError("Bitbucket Data Center needs its base URL to list repositories")
    start = (page - 1) * BROWSE_PAGE
    url = f"{api_base.rstrip('/')}/rest/api/1.0/repos?limit={BROWSE_PAGE}&start={start}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "User-Agent": "DataLabs-migration-downloader"}
    data, _link, _hdrs = supplementary._json_page(url, headers=headers)
    values = data.get("values", []) if isinstance(data, dict) else []
    rows = []
    for r in values:
        key = (r.get("project") or {}).get("key")
        if key and r.get("slug"):
            rows.append({"target": f"{key}/{r['slug']}", "label": f"{key}/{r['slug']}",
                         "private": not r.get("public", False), "archived": bool(r.get("archived")),
                         "size_kb": 0, "updated": ""})
    more = isinstance(data, dict) and not data.get("isLastPage", True)
    return {"items": rows, "has_more": more, "notes": []}


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
        return _json_request(f"{self._root}/{_q(job_id)}", headers=self._headers)[1]

    def messages(self, job_id: str) -> list:
        data = _json_request(f"{self._root}/{_q(job_id)}/messages", headers=self._headers)[1]
        return data.get("values", []) if isinstance(data, dict) else []

    def cancel(self, job_id: str) -> dict:
        self.log(f"  POST {self._root}/{_q(job_id)}/cancel")
        return _json_request(f"{self._root}/{_q(job_id)}/cancel", method="POST", headers=self._headers)[1]

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
        # a zip, not JSON: some Data Center versions answer 406 to Accept: application/json here
        return _download(url, dest, headers={**self._headers, "Accept": "*/*"}, log=self.log)


@collector("bitbucket-dc", "archive", heavy=True)
def dc_archive(ctx):
    """Snapshot zip of one repo's default ref (no history, no PR data)."""
    client = BitbucketDCExport(ctx.token, ctx.api_base, log=ctx.log)
    dest = ctx.out / "repo-snapshot.zip"
    client.repo_archive(ctx.target, dest)
    return [dest.name]
