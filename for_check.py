"""`<archive>.for-check.csv`: source-side facts recorded at export time.

The archive alone cannot say what it *should* contain. This module asks the source (GitHub / GitLab API and
`git ls-remote`) for counts and refs just before and just after the export, then lists what the downloaded
archive holds, so a checker (Garmr) can verify the archive is complete without any token or network.

One long-format CSV per archive, beside it:   repo,section,key,value

  meta            provider, host, scope, org, migration_id, state, api_version, archive, archive_bytes,
                  archive_sha256, content_length, opt_*, started_at, finished_at, format_version
  requested_repo  repos asked for
  export_repo     repos the source says the export contains (GitHub: GET .../migrations/:id/repositories)
  counts_before   entity counts before the export started      (blank value = unknown, never a guess)
  counts_after    entity counts after it finished
  ref_before      refs on the source before / after: heads, tags, PR/MR head refs -> sha
  ref_after
  file            every archive entry outside <repo>.git/objects/ -> size in bytes
  file_summary    objects_count / objects_bytes: the aggregated <repo>.git/objects/ entries
  error           anything that could not be collected (non-fatal)
"""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import subprocess
import tarfile
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import migration_api as mig
from migration_api import MigrationError

COLUMNS = ["repo", "section", "key", "value"]
FORMAT_VERSION = 1
REF_KEEP = re.compile(r"^refs/(heads/.+|tags/.+|pull/\d+/head|merge-requests/\d+/head)$")
OBJECTS = re.compile(r"^repositories/[^/]+/[^/]+\.git/objects/")
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")

GITHUB_QUERY = """
query($o:String!,$r:String!){repository(owner:$o,name:$r){
  defaultBranchRef{name target{... on Commit{history{totalCount}}}}
  pullRequests{totalCount} issues{totalCount} labels{totalCount} milestones{totalCount} releases{totalCount}
  branches:refs(refPrefix:"refs/heads/"){totalCount} tags:refs(refPrefix:"refs/tags/"){totalCount}
  diskUsage hasWikiEnabled hasIssuesEnabled}}
"""


# ---------------------------------------------------------------- csv

def row(repo, section, key, value="") -> dict:
    return {"repo": repo, "section": section, "key": str(key), "value": "" if value is None else str(value)}


def for_check_path(archive: Path) -> Path:
    name = archive.name
    for s in ARCHIVE_SUFFIXES:
        if name.lower().endswith(s):
            name = name[: -len(s)]
            break
    return archive.with_name(f"{name}.for-check.csv")


def write_csv(path: Path, rows: List[dict]) -> Path:
    partial = path.with_suffix(path.suffix + ".part")
    with partial.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    partial.replace(path)
    return path


# ---------------------------------------------------------------- HTTP helpers

def _get(url: str, headers: dict):
    """GET -> (parsed json or None, response headers)."""
    try:
        with mig._request(url, headers=headers, timeout=60) as resp:
            raw = resp.read()
            hdr = resp.headers
    except urllib.error.HTTPError as exc:
        raise MigrationError(f"HTTP {exc.code} GET {url}") from None
    except urllib.error.URLError as exc:
        raise MigrationError(f"network error GET {url}: {exc.reason}") from None
    return (json.loads(raw) if raw.strip() else None), hdr


def graphql_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    return base + "/graphql" if base.endswith("api.github.com") else re.sub(r"/api/v3$", "/api/graphql", base)


def github_web_base(api_base: str) -> str:
    base = api_base.rstrip("/")
    return "https://github.com" if base.endswith("api.github.com") else re.sub(r"/api/v3$", "", base)


def _graphql(url: str, headers: dict, query: str, variables: dict) -> dict:
    _status, data = mig._json_request(url, method="POST", headers=headers,
                                      payload={"query": query, "variables": variables})
    if not isinstance(data, dict) or data.get("errors") or not data.get("data"):
        raise MigrationError(f"graphql: {json.dumps((data or {}).get('errors', data))[:200]}")
    return data["data"]


def _count(url: str, headers: dict):
    """Item count of a list endpoint using one item per page: X-Total (GitLab) or the rel=last page (GitHub)."""
    data, hdr = _get(url + ("&" if "?" in url else "?") + "per_page=1", headers)
    total = hdr.get("X-Total")
    if total not in (None, ""):
        return int(total)
    m = re.search(r'[?&]page=(\d+)>;\s*rel="last"', hdr.get("Link") or "")
    if m:
        return int(m.group(1))
    return len(data) if isinstance(data, list) else ""


def _try(errors: list, label: str, fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # unknown stays blank: a wrong guess would hide a real gap
        errors.append(f"{label}: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
        return ""


# ---------------------------------------------------------------- counts

def github_counts(owner: str, repo: str, *, headers: dict, api_base: str) -> Dict[str, object]:
    errors: list = []
    c: Dict[str, object] = {k: "" for k in (
        "prs", "issues", "labels", "milestones", "releases", "branches", "tags", "commits", "default_branch",
        "size_kb", "has_wiki", "has_issues", "issue_comments", "review_comments", "commit_comments", "collaborators")}
    try:
        r = _graphql(graphql_url(api_base), headers, GITHUB_QUERY, {"o": owner, "r": repo})["repository"]
        ref = r.get("defaultBranchRef")
        c.update(prs=r["pullRequests"]["totalCount"], issues=r["issues"]["totalCount"],
                 labels=r["labels"]["totalCount"], milestones=r["milestones"]["totalCount"],
                 releases=r["releases"]["totalCount"], branches=r["branches"]["totalCount"],
                 tags=r["tags"]["totalCount"], size_kb=r.get("diskUsage", ""),
                 commits=ref["target"]["history"]["totalCount"] if ref else 0,
                 default_branch=ref["name"] if ref else "",
                 has_wiki=str(bool(r.get("hasWikiEnabled"))).lower(),
                 has_issues=str(bool(r.get("hasIssuesEnabled"))).lower())
    except Exception as exc:
        errors.append(f"graphql: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
    base = f"{api_base.rstrip('/')}/repos/{owner}/{repo}"
    c["issue_comments"] = _try(errors, "issue_comments", _count, f"{base}/issues/comments", headers)
    c["review_comments"] = _try(errors, "review_comments", _count, f"{base}/pulls/comments", headers)
    c["commit_comments"] = _try(errors, "commit_comments", _count, f"{base}/comments", headers)
    c["collaborators"] = _try(errors, "collaborators", _count, f"{base}/collaborators?affiliation=all", headers)
    c["_errors"] = errors
    return c


def gitlab_counts(project: str, *, headers: dict, api_base: str) -> Dict[str, object]:
    errors: list = []
    ident = project if project.isdigit() else urllib.parse.quote(project, safe="")
    base = f"{api_base.rstrip('/')}/api/v4/projects/{ident}"
    c: Dict[str, object] = {k: "" for k in (
        "prs", "issues", "labels", "milestones", "releases", "branches", "tags", "commits", "default_branch",
        "collaborators", "has_wiki")}
    try:
        info, _ = _get(f"{base}?statistics=true", headers)
        c["commits"] = (info.get("statistics") or {}).get("commit_count", "")
        c["default_branch"] = info.get("default_branch") or ""
        c["has_wiki"] = str(bool(info.get("wiki_enabled"))).lower()
    except Exception as exc:
        errors.append(f"project: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
    for key, path in (("issues", "issues?scope=all"), ("prs", "merge_requests?scope=all&state=all"),
                      ("labels", "labels?include_ancestor_groups=false"), ("milestones", "milestones"),
                      ("releases", "releases"), ("branches", "repository/branches"),
                      ("tags", "repository/tags"), ("collaborators", "members/all")):
        c[key] = _try(errors, key, _count, f"{base}/{path}", headers)
    c["_errors"] = errors
    return c


# ---------------------------------------------------------------- refs

def ls_remote_refs(url: str, user: str, token: str, run: Callable = subprocess.run) -> Dict[str, str]:
    """Branches, tags and PR/MR head refs on the source. The token travels in the environment, never on argv."""
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}"}
    res = run(["git", "ls-remote", "--refs", url], capture_output=True, text=True, env=env, timeout=600)
    if res.returncode != 0:
        msg = (res.stderr or "").strip().splitlines()
        raise MigrationError("git ls-remote failed: " + (msg[0].replace(token, "***") if msg else f"exit {res.returncode}"))
    refs = {}
    for line in res.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref and REF_KEEP.match(ref):
            refs[ref] = sha
    return refs


# ---------------------------------------------------------------- archive listing

def list_archive(path: Path) -> Tuple[Dict[str, int], int, int]:
    """(file -> size outside <repo>.git/objects/, objects entry count, objects bytes)."""
    files: Dict[str, int] = {}
    ocount = obytes = 0
    with tarfile.open(path, "r:*") as tar:
        for ti in tar:
            if not ti.isfile():
                continue
            name = re.sub(r"^\./", "", ti.name)
            if OBJECTS.match(name):
                ocount += 1
                obytes += ti.size
            else:
                files[name] = ti.size
    return files, ocount, obytes


# ---------------------------------------------------------------- orchestration (called by runner.py)

def snapshot(provider: str, label: str, *, headers: dict, api_base: str, token: str, owner: str = "",
             repo: str = "") -> dict:
    """Counts + refs for one repo right now. Never raises: failures become `errors`."""
    errors: list = []
    counts, refs = {}, {}
    try:
        if provider == "github":
            counts = github_counts(owner, repo, headers=headers, api_base=api_base)
        else:
            counts = gitlab_counts(label, headers=headers, api_base=api_base)
        errors += counts.pop("_errors", [])
    except Exception as exc:
        errors.append(f"counts: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
    try:
        if provider == "github":
            url = f"{github_web_base(api_base)}/{owner}/{repo}.git"
            refs = ls_remote_refs(url, "x-access-token", token)
        else:
            refs = ls_remote_refs(f"{api_base.rstrip('/')}/{label}.git", "oauth2", token)
    except Exception as exc:
        errors.append(f"refs: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
    return {"counts": counts, "refs": refs, "errors": errors}


def build_rows(info: dict, *, archive: Path, digest: str, files=None, ocount=0, obytes=0) -> List[dict]:
    """info: {archive_label, meta, requested, exported, repos:[{label, before, after}], errors}."""
    a = info["archive_label"]
    rows = [row(a, "meta", "format_version", FORMAT_VERSION)]
    meta = dict(info["meta"], archive=archive.name, archive_bytes=archive.stat().st_size if archive.exists() else "",
                archive_sha256=digest)
    rows += [row(a, "meta", k, v) for k, v in meta.items()]
    rows += [row(a, "requested_repo", r) for r in info["requested"]]
    rows += [row(a, "export_repo", r) for r in info["exported"]]
    for rp in info["repos"]:
        for tag in ("before", "after"):
            snap = rp.get(tag) or {}
            rows += [row(rp["label"], f"counts_{tag}", k, v) for k, v in (snap.get("counts") or {}).items()]
            rows += [row(rp["label"], f"ref_{tag}", k, v) for k, v in sorted((snap.get("refs") or {}).items())]
            rows += [row(rp["label"], "error", f"{tag}", e) for e in snap.get("errors", [])]
    rows += [row(a, "error", "for_check", e) for e in info.get("errors", [])]
    if files is not None:
        rows += [row(a, "file", k, v) for k, v in sorted(files.items())]
        rows += [row(a, "file_summary", "objects_count", ocount), row(a, "file_summary", "objects_bytes", obytes)]
    return rows
