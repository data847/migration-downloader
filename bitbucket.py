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

import shutil
import tarfile
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Iterable, List, Optional

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


# ── full history: git clone --mirror, with the same token ───────────────────
#
# Bitbucket Cloud has no export archive, so the repository itself comes from git. The token the
# REST calls use works for git over HTTPS too (scope read:repository):
#   an API token, pasted as email:token -> user x-bitbucket-api-token-auth, password the token
#   an access token (workspace, project or repo), pasted bare -> user x-token-auth
# The credentials go through git's environment (supplementary.git_env), never the URL or argv.
# The mirror (every branch, tag and note; refs/pull-requests are not in a Cloud mirror) is packed
# as <workspace>-<repo>.tar.gz holding <repo>.git/, recorded as the target's archive, so the
# download bundle puts it in one zip with that repo's extras/<workspace>-<repo>/ metadata.

def git_credentials(token: str) -> tuple:
    """(username, password) git uses for a Bitbucket Cloud token."""
    token = token.strip()
    if ":" in token:                                   # email:api-token
        return "x-bitbucket-api-token-auth", token.split(":", 1)[1]
    return "x-token-auth", token


def clone_url(api_base: str, target: str) -> str:
    """https://bitbucket.org/<workspace>/<repo>.git for a target, from the API base's host."""
    ws, repo = (target.strip("/").split("/", 1) + [""])[:2]
    if not ws or not repo:
        raise MigrationError(f"'{target}' is not workspace/repo")
    parsed = urllib.parse.urlparse(api_base or BITBUCKET_CLOUD_BASE)
    host = parsed.netloc or "api.bitbucket.org"
    if host.startswith("api."):
        host = host[len("api."):]
    return f"{parsed.scheme or 'https'}://{host}/{urllib.parse.quote(ws)}/{urllib.parse.quote(repo)}.git"


def mirror_archive(target: str, token: str, api_base: str, dest_dir: Path, log: Callable[[str], None],
                   before_pack: Optional[Callable[[Path, Path], None]] = None) -> Path:
    """Clone target with --mirror and pack it as dest_dir/<safe target>.tar.gz (written as .part, then renamed).
    `before_pack(work, bare)` may add files beside <repo>.git/ in work (the archive's metadata) and refs to the
    mirror; everything in work is packed at the archive's root."""
    url = supplementary.http_url(clone_url(api_base, target))
    repo = target.strip("/").split("/", 1)[1]
    work = dest_dir / ".mirror-work" / safe_name(target)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    bare = work / f"{safe_name(repo)}.git"
    user, password = git_credentials(token)
    try:
        log(f"  git clone --mirror {url}")
        proc = supplementary.run_git(["clone", "--mirror", "--", url, str(bare)], env=supplementary.git_env(user, password))
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            raise MigrationError(f"git clone --mirror failed: {err[-1] if err else proc.returncode}"
                                 + (" (check the token has read:repository and can see this repo)"
                                    if any(w in " ".join(err).lower() for w in ("403", "401", "authentication", "not found")) else ""))
        if before_pack:
            before_pack(work, bare)
        dest = dest_dir / f"{safe_name(target)}.tar.gz"
        part = dest.with_name(dest.name + ".part")
        with tarfile.open(part, "w:gz") as tar:
            tar.add(bare, arcname=bare.name)
            for extra in sorted(p for p in work.iterdir() if p != bare):
                tar.add(extra, arcname=extra.name)
        part.replace(dest)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            (dest_dir / ".mirror-work").rmdir()
        except OSError:
            pass


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
    import bitbucket_archive
    git_dir = bitbucket_archive.local_git(ctx)
    commits = bitbucket_archive.commits(ctx, git_dir) if git_dir else cloud_paged(ctx, bb_url(ctx, "/commits"))
    ctx.shared.setdefault("bb_commits", {})[ctx.target] = [c["hash"] for c in commits]
    return [ctx.save_json("commits", commits)]


def _commit_hashes(ctx: Ctx) -> list:
    known = ctx.shared.get("bb_commits", {}).get(ctx.target)
    if known is not None:
        return known
    import bitbucket_archive
    git_dir = bitbucket_archive.local_git(ctx)
    if git_dir:
        hashes = [c["hash"] for c in bitbucket_archive.commits(ctx, git_dir)]
    else:
        hashes = [c["hash"] for c in cloud_paged(ctx, bb_url(ctx, "/commits"))]
    ctx.shared.setdefault("bb_commits", {})[ctx.target] = hashes
    return hashes


# One call per commit each: on a long history these take hours under Bitbucket's hourly limit, so they are heavy
# (left out of `all`, run by `everything` or by name) and the run says up front what they cost.
@collector("bitbucket", "commit-diffstat", heavy=True)
def bb_commit_diffstat(ctx):
    import bitbucket_archive
    out = {}
    git_dir = bitbucket_archive.local_git(ctx)
    if git_dir:                       # the same numbers from the mirror, no API calls
        for sha in _commit_hashes(ctx):
            ctx.check_cancelled()
            try:
                out[sha] = {"values": bitbucket_archive.diffstat(git_dir, sha), "source": "git mirror"}
            except MigrationError as exc:
                out[sha] = ctx.item_error(f"commit {sha[:10]} diffstat", exc)
        return [ctx.save_json("commit-diffstat", out)]
    for sha in _commit_hashes(ctx):
        ctx.check_cancelled()
        out[sha] = _all_or_error(ctx, bb_url(ctx, f"/diffstat/{_seg(sha)}"), f"commit {sha[:10]} diffstat")
    return [ctx.save_json("commit-diffstat", out)]


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


NOT_SET_UP = {"enabled": False, "note": "Bitbucket Pipelines was never set up for this repository (HTTP 404)"}


def _pipelines_part(ctx: Ctx, url: str, what: str, fetch):
    """A Pipelines settings call. Bitbucket answers 404 when Pipelines was never set up for the repo: that is a fact about
    the repo, recorded as such, not a missing item. Any other failure is counted as before."""
    try:
        return fetch(url)
    except MigrationError as exc:
        if "cancelled" in str(exc):
            raise
        if "http 404" in brief_error(exc).lower():
            ctx.log(f"    {what}: unavailable for this repo, not an error")
            return dict(NOT_SET_UP)
        return ctx.item_error(what, exc)


@collector("bitbucket", "pipelines")
def bb_pipelines(ctx):
    pipelines = cloud_paged(ctx, bb_url(ctx, "/pipelines?sort=-created_on"))
    steps = _pipeline_steps(ctx, pipelines)
    config = _pipelines_part(ctx, bb_url(ctx, "/pipelines_config"), "pipelines config", lambda u: get_json(u, ctx.headers))
    return [
        ctx.save_json("pipelines-config", config),
        ctx.save_json("pipelines", pipelines),
        ctx.save_json("pipeline-steps", steps),
        # variable names only: secured values are never returned
        ctx.save_json("pipeline-variables", _pipelines_part(ctx, bb_url(ctx, "/pipelines_config/variables"), "pipeline variables",
                                                            lambda u: {"values": cloud_paged(ctx, u, pagelen=100)})),
    ]


@collector("bitbucket", "forks")
def bb_forks(ctx):
    return [ctx.save_json("forks", cloud_paged(ctx, bb_url(ctx, "/forks")))]


@collector("bitbucket", "pr-tasks")
def bb_pr_tasks(ctx):
    """Only pull requests whose listing says they have tasks; every PR when the listing does not say."""
    prs = _prs(ctx)
    counted = any("task_count" in pr for pr in prs)
    out = {}
    for pr in prs:
        if counted and not pr.get("task_count"):
            continue
        ctx.check_cancelled()
        pid = pr["id"]
        out[str(pid)] = _all_or_error(ctx, bb_url(ctx, f"/pullrequests/{pid}/tasks"), f"PR {pid} tasks", PR_PAGELEN)
    return [ctx.save_json("pr-tasks", out)]


@collector("bitbucket", "deployments")
def bb_deployments(ctx):
    data = {}
    for key, suffix in (("environments", "/environments/"), ("deployments", "/deployments/")):
        try:
            data[key] = cloud_paged(ctx, bb_url(ctx, suffix))
        except MigrationError as exc:
            if not supplementary.not_found(brief_error(exc)):
                raise
            data[key] = []                   # no Pipelines, so no deployments: nothing to have, not an error
    return [ctx.save_json("deployments", data)]


@collector("bitbucket", "commit-comments", heavy=True)
def bb_commit_comments(ctx):
    """One call per commit: Bitbucket has no repository-wide list of commit comments."""
    out = {}
    for sha in _commit_hashes(ctx):
        ctx.check_cancelled()
        out[sha] = _all_or_error(ctx, bb_url(ctx, f"/commit/{_seg(sha)}/comments"), f"commit {sha[:10]} comments")
    return [ctx.save_json("commit-comments", out)]


@collector("bitbucket", "test-reports", heavy=True)
def bb_test_reports(ctx):
    """Test reports and their cases, per pipeline step. A step without reports answers 404: skipped quietly."""
    steps = ctx.shared.get("bb_steps", {}).get(ctx.target)
    if steps is None:
        pipelines = _pipelines_part(ctx, bb_url(ctx, "/pipelines/?sort=-created_on"), "pipelines",
                                    lambda url: cloud_paged(ctx, url))
        steps = _pipeline_steps(ctx, pipelines if isinstance(pipelines, list) else [])
    out = {}
    for pipe_uuid, listing in steps.items():
        for step in (listing.get("values", []) if isinstance(listing, dict) else []):
            ctx.check_cancelled()
            base = bb_url(ctx, f"/pipelines/{_seg(pipe_uuid)}/steps/{_seg(step['uuid'])}/test_reports")
            try:
                summary = get_json(base, ctx.headers)
            except MigrationError as exc:
                if supplementary.not_found(brief_error(exc)):
                    continue
                ctx.item_error(f"step {step['uuid']} test report", exc)
                continue
            out[f"{pipe_uuid}/{step['uuid']}"] = {
                "summary": summary,
                "test_cases": _all_or_error(ctx, base + "/test_cases", f"step {step['uuid']} test cases")
                .get("values", []),
            }
    return [ctx.save_json("test-reports", out)]


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
