"""Supplementary collectors: everything the export archives leave out.

The GitHub migration archive and the GitLab project export cover repos, issues
and PRs, but not wikis, LFS blobs, CI history, webhooks, deploy keys, alerts,
packages and so on. Each collector here fetches one such thing for one target
and writes it under `<run>/extras/<target>/`. All collectors are read-only.

Comment/discussion threads are deliberately not collected.

A collector is `fn(ctx) -> list[str]` returning the relative paths it wrote;
it raises `MigrationError` on failure and the runner records that per
collector, so one forbidden endpoint never aborts the rest.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from migration_api import (
    MigrationError,
    _download,
    _json_request,
    brief_error,
    safe_name,
)

Collector = Callable[["Ctx"], List[str]]

REGISTRY: Dict[str, Dict[str, Collector]] = {}
# Collectors that pull bulk content (full clones, logs, binaries). Skipped by
# `all`; asked for by name or with `everything`.
HEAVY: Dict[str, set] = {}

GIT_TIMEOUT = 3600


def collector(provider: str, name: str, *, heavy: bool = False):
    def register(fn: Collector) -> Collector:
        REGISTRY.setdefault(provider, {})[name] = fn
        if heavy:
            HEAVY.setdefault(provider, set()).add(name)
        return fn
    return register


def resolve_names(provider: str, requested: List[str]) -> List[str]:
    """Expand `all` / `everything`; reject names the provider doesn't have."""
    available = REGISTRY.get(provider, {})
    names: List[str] = []
    for item in requested:
        item = item.strip().lower()
        if not item:
            continue
        if item == "all":
            names += [n for n in available if n not in HEAVY.get(provider, set())]
        elif item == "everything":
            names += list(available)
        elif item in available:
            names.append(item)
        else:
            raise MigrationError(
                f"unknown extra '{item}' for {provider}; choose from "
                f"{sorted(available)} or all / everything"
            )
    return list(dict.fromkeys(names))


# ── context + http helpers ───────────────────────────────────────────────────


@dataclass
class Ctx:
    provider: str
    target: str                    # github "owner/repo" · gitlab "ns/project" · bitbucket "ws/repo"
    token: str
    api_base: str
    headers: dict
    out: Path                      # this target's extras directory
    log: Callable[[str], None]
    org: str = ""                  # github org scope: the target is a bare repo name
    scope: str = "user"
    max_items: int = 200           # cap for unbounded listings (runs, pipelines, commits…)
    shared: dict = field(default_factory=dict)   # once-per-run state (groups, workspaces)
    cancelled: Optional[Callable[[], bool]] = None

    def save_json(self, name: str, data) -> str:
        path = self.out / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str))
        return str(path.relative_to(self.out))

    def check_cancelled(self) -> None:
        if self.cancelled and self.cancelled():
            raise MigrationError("cancelled")


def get_json(url: str, headers: dict):
    return _json_request(url, headers=headers)[1]


def paged_all(url: str, headers: dict, *, key: str = "", cap: int = 200, per_page: int = 100) -> list:
    """`?page=N` paging. `key` unwraps endpoints that answer {key: [...]}."""
    items: list = []
    page = 1
    while len(items) < cap:
        sep = "&" if "?" in url else "?"
        data = get_json(f"{url}{sep}per_page={per_page}&page={page}", headers)
        batch = data.get(key, []) if key and isinstance(data, dict) else data
        if not isinstance(batch, list) or not batch:
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return items[:cap]


def fetch_file(ctx: Ctx, url: str, rel: str, *, headers: Optional[dict] = None) -> str:
    dest = ctx.out / rel
    _download(url, dest, headers=headers or ctx.headers, log=lambda _m: None)
    return rel


def try_json(ctx: Ctx, url: str):
    """Per-item calls (one branch, one run) shouldn't sink the whole collector."""
    try:
        return get_json(url, ctx.headers)
    except MigrationError as exc:
        return {"error": brief_error(exc)}


def feature_off(exc: MigrationError) -> bool:
    """403/404 whose message says the feature is simply off for this repo
    (Dependabot, code or secret scanning), as opposed to a missing permission."""
    text = brief_error(exc).lower()
    return (("http 403" in text or "http 404" in text)
            and any(w in text for w in ("disabled", "must be enabled", "not enabled",
                                        "advanced security", "no analysis found")))


def skip_when_off(fn: Collector) -> Collector:
    @functools.wraps(fn)
    def wrapper(ctx):
        try:
            return fn(ctx)
        except MigrationError as exc:
            if feature_off(exc):
                ctx.log("    not enabled for this repo — skipped")
                return []
            raise
    return wrapper


def git_env(username: str, token: str) -> dict:
    """Pass credentials through git's env config, never the URL or argv, so they
    can't leak into `ps`, the clone's `.git/config`, or an error message."""
    basic = base64.b64encode(f"{username}:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def run_git(args: List[str], *, env: dict, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], env=env, cwd=cwd, capture_output=True,
                              text=True, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        raise MigrationError("git is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise MigrationError(f"git {args[0]} timed out after {GIT_TIMEOUT}s") from None


def mirror(ctx: Ctx, clone_url: str, rel: str, *, username: str, optional: bool = False) -> List[str]:
    """`git clone --mirror`, or `git remote update` when it's already on disk
    (a resumed run). `optional` treats 'repository not found' as 'none exists'
    (wikis)."""
    dest = ctx.out / rel
    env = git_env(username, ctx.token)
    if (dest / "HEAD").is_file():
        proc = run_git(["remote", "update", "--prune"], env=env, cwd=dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = run_git(["clone", "--mirror", clone_url, str(dest)], env=env)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if optional and ("not found" in err.lower() or "does not exist" in err.lower()):
            ctx.log("    none exists — skipped")
            return []
        raise MigrationError(f"git failed: {err.splitlines()[-1] if err else proc.returncode}")
    return [rel]


# ── GitHub ───────────────────────────────────────────────────────────────────

GITHUB_PACKAGE_TYPES = ("npm", "maven", "rubygems", "docker", "nuget", "container")


def gh_repo(ctx: Ctx) -> str:
    """owner/repo — org-scope targets are bare names."""
    return f"{ctx.org}/{ctx.target}" if ctx.org and "/" not in ctx.target else ctx.target


def gh_url(ctx: Ctx, suffix: str = "") -> str:
    return f"{ctx.api_base}/repos/{gh_repo(ctx)}{suffix}"


def gh_web_base(ctx: Ctx) -> str:
    """Git host for clones: api.github.com -> github.com, GHES …/api/v3 -> host."""
    base = ctx.api_base
    if base.rstrip("/").endswith("/api/v3"):
        return base.rstrip("/")[: -len("/api/v3")]
    return base.replace("://api.", "://")


@collector("github", "repo-metadata")
def gh_repo_metadata(ctx):
    return [ctx.save_json("repo-metadata", get_json(gh_url(ctx), ctx.headers))]


@collector("github", "wiki", heavy=True)
def gh_wiki(ctx):
    return mirror(ctx, f"{gh_web_base(ctx)}/{gh_repo(ctx)}.wiki.git", "wiki.git",
                  username="x-access-token", optional=True)


@collector("github", "releases")
def gh_releases(ctx):
    return [ctx.save_json("releases", paged_all(gh_url(ctx, "/releases"), ctx.headers, cap=ctx.max_items))]


@collector("github", "actions")
def gh_actions(ctx):
    runs = paged_all(gh_url(ctx, "/actions/runs"), ctx.headers, key="workflow_runs", cap=ctx.max_items)
    ctx.shared.setdefault("gh_runs", {})[ctx.target] = [r["id"] for r in runs]
    jobs = {}
    for run in runs:
        ctx.check_cancelled()
        jobs[str(run["id"])] = try_json(ctx, gh_url(ctx, f"/actions/runs/{run['id']}/jobs"))
    return [ctx.save_json("actions-runs", runs), ctx.save_json("actions-jobs", jobs)]


@collector("github", "actions-logs", heavy=True)
def gh_actions_logs(ctx):
    written = []
    for run_id in ctx.shared.get("gh_runs", {}).get(ctx.target) or [
        r["id"] for r in paged_all(gh_url(ctx, "/actions/runs"), ctx.headers,
                                   key="workflow_runs", cap=ctx.max_items)
    ]:
        ctx.check_cancelled()
        try:
            written.append(fetch_file(ctx, gh_url(ctx, f"/actions/runs/{run_id}/logs"),
                                      f"actions-logs/run-{run_id}.zip"))
        except MigrationError as exc:   # logs expire after 90 days -> 410
            ctx.log(f"    run {run_id}: {brief_error(exc)}")
    return written


@collector("github", "actions-artifacts")
def gh_actions_artifacts(ctx):
    artifacts = paged_all(gh_url(ctx, "/actions/artifacts"), ctx.headers, key="artifacts", cap=ctx.max_items)
    return [ctx.save_json("actions-artifacts", artifacts)]


@collector("github", "hooks")
def gh_hooks(ctx):
    return [ctx.save_json("hooks", paged_all(gh_url(ctx, "/hooks"), ctx.headers, cap=ctx.max_items))]


@collector("github", "branch-protection")
def gh_branch_protection(ctx):
    branches = paged_all(gh_url(ctx, "/branches?protected=true"), ctx.headers, cap=ctx.max_items)
    out = {}
    for branch in branches:
        name = branch["name"]
        out[name] = try_json(ctx, gh_url(ctx, f"/branches/{urllib.parse.quote(name, safe='')}/protection"))
    return [ctx.save_json("branch-protection", out)]


@collector("github", "dependabot-alerts")
@skip_when_off
def gh_dependabot(ctx):
    return [ctx.save_json("dependabot-alerts", paged_all(
        gh_url(ctx, "/dependabot/alerts?state=open,fixed,dismissed,auto_dismissed"),
        ctx.headers, cap=ctx.max_items))]


@collector("github", "code-scanning")
@skip_when_off
def gh_code_scanning(ctx):
    return [
        ctx.save_json("code-scanning-alerts", paged_all(
            gh_url(ctx, "/code-scanning/alerts"), ctx.headers, cap=ctx.max_items)),
        ctx.save_json("code-scanning-analyses", paged_all(
            gh_url(ctx, "/code-scanning/analyses"), ctx.headers, cap=ctx.max_items)),
    ]


@collector("github", "secret-scanning")
@skip_when_off
def gh_secret_scanning(ctx):
    # Alert records name the secret type and location; the secret value is
    # masked by the API unless explicitly requested, which we never do.
    return [ctx.save_json("secret-scanning-alerts", paged_all(
        gh_url(ctx, "/secret-scanning/alerts"), ctx.headers, cap=ctx.max_items))]


def _graphql_url(ctx: Ctx) -> str:
    base = ctx.api_base.rstrip("/")
    return base[: -len("/v3")] + "/graphql" if base.endswith("/api/v3") else f"{base}/graphql"


PROJECTS_V2_QUERY = """
query($login: String!) {
  %s(login: $login) {
    projectsV2(first: 50) {
      nodes {
        id number title shortDescription public closed
        fields(first: 50) { nodes { ... on ProjectV2FieldCommon { name dataType } } }
        items(first: 100) {
          totalCount
          nodes { id type content {
            ... on Issue { title url } ... on PullRequest { title url } ... on DraftIssue { title } } }
        }
      }
    }
  }
}"""


@collector("github", "projects-v2")
def gh_projects_v2(ctx):
    owner = ctx.org or gh_repo(ctx).split("/")[0]
    kind = "organization" if ctx.org else "user"
    seen = ctx.shared.setdefault("gh_projects_v2", {})
    if owner in seen:           # owner-level, so once per run is enough
        return []
    _status, data = _json_request(
        _graphql_url(ctx), method="POST", headers=ctx.headers,
        payload={"query": PROJECTS_V2_QUERY % kind, "variables": {"login": owner}})
    if data.get("errors"):
        raise MigrationError(f"GraphQL: {data['errors'][0].get('message', 'error')}")
    seen[owner] = True
    return [ctx.save_json(f"projects-v2-{safe_name(owner)}", data.get("data"))]


@collector("github", "packages")
def gh_packages(ctx):
    owner = ctx.org or gh_repo(ctx).split("/")[0]
    seen = ctx.shared.setdefault("gh_packages", {})
    if owner in seen:
        return []
    base = f"{ctx.api_base}/orgs/{owner}/packages" if ctx.org else f"{ctx.api_base}/users/{owner}/packages"
    out = {}
    for ptype in GITHUB_PACKAGE_TYPES:
        out[ptype] = try_json(ctx, f"{base}?package_type={ptype}&per_page=100")
    seen[owner] = True
    return [ctx.save_json(f"packages-{safe_name(owner)}", out)]


# ── GitLab ───────────────────────────────────────────────────────────────────


def gl_url(ctx: Ctx, suffix: str = "") -> str:
    ident = urllib.parse.quote(ctx.target, safe="")
    return f"{ctx.api_base}/api/v4/projects/{ident}{suffix}"


def gl_clone_url(ctx: Ctx, suffix: str = ".git") -> str:
    return f"{ctx.api_base}/{ctx.target}{suffix}"


@collector("gitlab", "repo-archive")
def gl_repo_archive(ctx):
    return [fetch_file(ctx, gl_url(ctx, "/repository/archive.tar.gz"), "repository-snapshot.tar.gz")]


@collector("gitlab", "wiki", heavy=True)
def gl_wiki(ctx):
    return mirror(ctx, gl_clone_url(ctx, ".wiki.git"), "wiki.git", username="oauth2", optional=True)


@collector("gitlab", "wiki-pages")
def gl_wiki_pages(ctx):
    return [ctx.save_json("wiki-pages", get_json(gl_url(ctx, "/wikis?with_content=1"), ctx.headers))]


@collector("gitlab", "releases")
def gl_releases(ctx):
    return [ctx.save_json("releases", paged_all(gl_url(ctx, "/releases"), ctx.headers, cap=ctx.max_items))]


@collector("gitlab", "pipelines")
def gl_pipelines(ctx):
    pipelines = paged_all(gl_url(ctx, "/pipelines"), ctx.headers, cap=ctx.max_items)
    jobs = {}
    for pipe in pipelines:
        ctx.check_cancelled()
        jobs[str(pipe["id"])] = try_json(ctx, gl_url(ctx, f"/pipelines/{pipe['id']}/jobs?per_page=100"))
    ctx.shared.setdefault("gl_jobs", {})[ctx.target] = [
        j["id"] for js in jobs.values() if isinstance(js, list) for j in js]
    return [ctx.save_json("pipelines", pipelines), ctx.save_json("pipeline-jobs", jobs)]


def _gl_job_ids(ctx: Ctx) -> list:
    ids = ctx.shared.get("gl_jobs", {}).get(ctx.target)
    if ids is not None:
        return ids
    return [j["id"] for j in paged_all(gl_url(ctx, "/jobs"), ctx.headers, cap=ctx.max_items)]


@collector("gitlab", "job-traces", heavy=True)
def gl_job_traces(ctx):
    written = []
    for job_id in _gl_job_ids(ctx):
        ctx.check_cancelled()
        try:
            written.append(fetch_file(ctx, gl_url(ctx, f"/jobs/{job_id}/trace"), f"job-traces/{job_id}.log"))
        except MigrationError as exc:
            ctx.log(f"    job {job_id}: {brief_error(exc)}")
    return written


@collector("gitlab", "job-artifacts", heavy=True)
def gl_job_artifacts(ctx):
    written = []
    for job_id in _gl_job_ids(ctx):
        ctx.check_cancelled()
        try:     # 404 simply means the job produced no artifacts
            written.append(fetch_file(ctx, gl_url(ctx, f"/jobs/{job_id}/artifacts"),
                                      f"job-artifacts/{job_id}.zip"))
        except MigrationError as exc:
            if "HTTP 404" not in str(exc):
                ctx.log(f"    job {job_id}: {brief_error(exc)}")
    return written


@collector("gitlab", "hooks")
def gl_hooks(ctx):
    return [ctx.save_json("hooks", paged_all(gl_url(ctx, "/hooks"), ctx.headers, cap=ctx.max_items))]


@collector("gitlab", "relations-export", heavy=True)
def gl_relations_export(ctx):
    from migration_api import GitLabExport
    client = GitLabExport(ctx.token, api_base=ctx.api_base, log=ctx.log)
    saved = client.relations_export(ctx.target, ctx.out / "relations", cancelled=ctx.cancelled)
    return [str(p.relative_to(ctx.out)) for p in saved]
