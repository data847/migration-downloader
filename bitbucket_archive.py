"""Bitbucket Cloud archive at GitHub/GitLab parity, and the per-commit extras computed from it.

Bitbucket Cloud has no export API, so the archive is assembled: the `git clone --mirror` that `bitbucket.mirror_archive`
makes, plus, packed beside `<repo>.git/` in the same tar.gz, what a GitHub migration archive or a GitLab project export
carries about the repo's review history and settings. The REST budget goes on listings only:

  repo, workspace, project               3 calls
  pull requests, every state             one listing (reviewers and participants included via `fields=`)
  reviews, PR comments, PR events        the repo-wide /pullrequests/activity feed, split locally
  branch protection, downloads, hooks,   a handful of listings
  pipelines, schedules
  users                                  derived from all of the above, no calls

LFS objects come through git (`git lfs fetch --all`) when the history uses LFS, and `refs/pull/<id>/head` is written
into the mirror for every PR whose source commit it holds, as a GitHub archive has. A part that fails does not stop
the archive: `schema.json` lists it and the run reports it as an issue.

The per-commit extras (`commits`, `commit-diffstat`, `commit-patches`) are computed here from the mirror when the run
has one, instead of one API call per commit each.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import supplementary
from migration_api import MigrationError, brief_error, safe_name

FORMAT = "bitbucket-cloud-archive"
FORMAT_VERSION = 1
PR_FIELDS = "+values.reviewers,+values.participants"
NOT_AVAILABLE = ["issues", "wikis", "labels", "milestones", "attachments", "reactions"]
OMITTED_BY_POLICY = ["deploy_keys", "members", "permissions"]
FILES = {
    "repositories_000001.json": "the repository object",
    "organizations_000001.json": "the workspace and the project the repository belongs to",
    "pull_requests_000001.json": "every pull request, every state, with reviewers and participants",
    "pull_request_reviews_000001.json": "approvals and change requests, from the activity feed",
    "pull_request_review_comments_000001.json": "inline (file and line) pull request comments",
    "issue_comments_000001.json": "pull request conversation comments (not inline)",
    "issue_events_000001.json": "pull request updates: state changes, new commits, edits",
    "raw/pullrequest_activity.json": "the activity feed exactly as Bitbucket returned it",
    "protected_branches_000001.json": "branch restrictions and the branching model",
    "releases_000001.json": "the Downloads listing (metadata; files are not part of the archive)",
    "webhooks_000001.json": "repository webhooks",
    "pipelines_000001.json": "the pipeline listing",
    "pipeline_schedules_000001.json": "pipeline schedules",
    "users_000001.json": "every account seen in the files above",
}
# A shell-free empty tree for the root commit's diff; computed per repo (SHA-1 and SHA-256 repos differ).
_EMPTY_TREE: Dict[str, str] = {}


# ── local git (no network, bytes in and out) ────────────────────────────────

def _git(git_dir: Path, *args: str, check: bool = True) -> bytes:
    proc = subprocess.run(["git", "--git-dir", str(git_dir), *args], capture_output=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=supplementary.GIT_TIMEOUT)
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise MigrationError(f"git {args[0]} failed: {err[-1] if err else proc.returncode}")
    return proc.stdout


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def full_commit(git_dir: Path, sha: str) -> str:
    """The full hash of a (possibly abbreviated) commit the mirror holds, else ''."""
    if not sha:
        return ""
    out = subprocess.run(["git", "--git-dir", str(git_dir), "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
                         capture_output=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return _text(out.stdout).strip() if out.returncode == 0 else ""


def uses_lfs(git_dir: Path) -> bool:
    """Any .gitattributes anywhere in the history ever mentioned filter=lfs."""
    out = _git(git_dir, "log", "--all", "-S", "filter=lfs", "--format=%H", "-1", "--",
               ".gitattributes", "*/.gitattributes", check=False)
    return bool(out.strip())


# ── the archive's metadata ──────────────────────────────────────────────────

class _Parts:
    """Runs one part at a time; a failure is recorded, never raised (except a cancel)."""

    def __init__(self, log: Callable[[str], None]):
        self.errors: List[dict] = []
        self.notes: List[str] = []
        self.log = log

    def run(self, name: str, fn, default, *, missing_ok: bool = True, missing_note: str = ""):
        try:
            return fn()
        except MigrationError as exc:
            message = brief_error(exc)
            if "cancelled" in message:
                raise
            if missing_ok and supplementary.not_found(message):
                if missing_note:
                    self.notes.append(missing_note)
                return default
            self.errors.append({"part": name, "error": message})
            self.log(f"    archive part {name}: {message}")
            return default


def _pr_listing(ctx, parts: _Parts) -> list:
    import bitbucket
    states = "&".join(f"state={s}" for s in bitbucket.PR_STATES)
    url = bitbucket.bb_url(ctx, f"/pullrequests?{states}&fields={urllib.parse.quote(PR_FIELDS, safe=',')}")
    try:
        prs = bitbucket.cloud_paged(ctx, url, pagelen=bitbucket.PR_PAGELEN)
    except MigrationError as exc:
        text = str(exc).lower()
        if "http 400" not in text or "pagelen" in text:
            raise
        parts.notes.append("the pull request listing refused the reviewers/participants fields; listed without them")
        prs = bitbucket._prs(ctx)
    ctx.shared.setdefault("bb_prs", {})[ctx.target] = prs
    return prs


def _activity(ctx, prs: list, parts: _Parts) -> list:
    import bitbucket
    try:
        return bitbucket.cloud_paged(ctx, bitbucket.bb_url(ctx, "/pullrequests/activity"), pagelen=bitbucket.PR_PAGELEN)
    except MigrationError as exc:
        message = brief_error(exc)
        if "cancelled" in message or not ("http 404" in message.lower() or "http 400" in message.lower()):
            raise
    parts.notes.append("the repository-wide activity feed was refused; activity was read per pull request")
    entries: list = []
    for pr in prs:
        ctx.check_cancelled()
        pid = pr.get("id")
        rows = parts.run(f"activity PR {pid}", lambda: bitbucket.cloud_paged(
            ctx, bitbucket.bb_url(ctx, f"/pullrequests/{pid}/activity"), pagelen=bitbucket.PR_PAGELEN), [])
        for row in rows:
            row.setdefault("pull_request", {"id": pid})
        entries += rows
    return entries


def split_activity(entries: list) -> Dict[str, list]:
    """The feed's entries, sorted into GitHub archive files. Each keeps its pull request id."""
    out: Dict[str, list] = {"reviews": [], "review_comments": [], "issue_comments": [], "issue_events": []}
    for entry in entries:
        pid = (entry.get("pull_request") or {}).get("id")
        body = {k: v for k, v in entry.items() if k != "pull_request"}
        if "approval" in entry or "changes_requested" in entry:
            key = "approval" if "approval" in entry else "changes_requested"
            item = entry[key] or {}
            out["reviews"].append({"pull_request_id": pid,
                                   "state": "approved" if key == "approval" else "changes_requested",
                                   "user": item.get("user"), "date": item.get("date")})
        elif "comment" in entry:
            target = "review_comments" if (entry["comment"] or {}).get("inline") else "issue_comments"
            out[target].append({"pull_request_id": pid, **body})
        else:
            out["issue_events"].append({"pull_request_id": pid, **body})
    return out


def _users(prs: list, entries: list) -> list:
    seen: Dict[str, dict] = {}

    def add(user):
        if not isinstance(user, dict):
            return
        key = user.get("uuid") or user.get("account_id") or user.get("nickname") or user.get("display_name")
        if key and key not in seen:
            seen[key] = {k: user[k] for k in ("uuid", "account_id", "nickname", "display_name", "type") if k in user}

    for pr in prs:
        add(pr.get("author"))
        for r in pr.get("reviewers") or []:
            add(r)
        for p in pr.get("participants") or []:
            add((p or {}).get("user"))
    for e in entries:
        for key in ("approval", "changes_requested", "comment"):
            add((e.get(key) or {}).get("user"))
        add((e.get("update") or {}).get("author"))
    return list(seen.values())


def _organizations(ctx, repo: dict, parts: _Parts) -> list:
    """Workspace + project. A repository access token cannot read workspace endpoints: the copies embedded in the
    repo object stand in, and schema.json says so."""
    from supplementary import get_json
    ws = ctx.target.split("/", 1)[0]
    out = []
    try:
        out.append(get_json(f"{ctx.api_base}/workspaces/{urllib.parse.quote(ws, safe='')}", ctx.headers))
    except MigrationError as exc:
        if "cancelled" in str(exc):
            raise
        out.append(repo.get("workspace") or {"slug": ws})
        parts.notes.append(f"workspace details unavailable to this token ({brief_error(exc)}); the repo's copy is used")
    key = (repo.get("project") or {}).get("key")
    if key:
        try:
            out.append(get_json(f"{ctx.api_base}/workspaces/{urllib.parse.quote(ws, safe='')}/projects/"
                                f"{urllib.parse.quote(key, safe='')}", ctx.headers))
        except MigrationError as exc:
            if "cancelled" in str(exc):
                raise
            out.append(repo["project"])
            parts.notes.append(f"project details unavailable to this token ({brief_error(exc)}); the repo's copy is used")
    return out


def write_pr_refs(git_dir: Path, prs: list) -> dict:
    """refs/pull/<id>/head for every PR whose source commit the mirror holds (a Cloud mirror carries no PR refs)."""
    written, missing = 0, []
    for pr in prs:
        pid = pr.get("id")
        sha = full_commit(git_dir, (((pr.get("source") or {}).get("commit")) or {}).get("hash", ""))
        if pid is None:
            continue
        if not sha:
            missing.append(pid)
            continue
        _git(git_dir, "update-ref", f"refs/pull/{int(pid)}/head", sha)
        written += 1
    return {"written": written, "source_commit_missing": missing}


def fetch_lfs(git_dir: Path, user: str, password: str, parts: _Parts, log: Callable[[str], None]) -> dict:
    if not uses_lfs(git_dir):
        return {"used": False, "fetched": False}
    version = supplementary.run_git(["lfs", "version"], env=supplementary.git_env(user, password), cwd=git_dir)
    if version.returncode != 0:
        parts.errors.append({"part": "lfs", "error": "the history uses Git LFS but git-lfs is not installed; "
                                                     "LFS files are not in this archive"})
        log("    LFS: the history uses Git LFS but git-lfs is not installed")
        return {"used": True, "fetched": False}
    log("  git lfs fetch --all")
    proc = supplementary.run_git(["lfs", "fetch", "--all", "origin"], env=supplementary.git_env(user, password),
                                 cwd=git_dir)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        parts.errors.append({"part": "lfs", "error": f"git lfs fetch failed: {err[-1] if err else proc.returncode}"})
        return {"used": True, "fetched": False}
    return {"used": True, "fetched": True}


def build_core(ctx, git_dir: Path, *, user: str, password: str) -> dict:
    """Write the archive's metadata into ctx.out (beside git_dir) and the PR refs into git_dir.
    Returns {"errors": [...], "notes": [...], "counts": {...}}."""
    import bitbucket
    from supplementary import get_json
    parts = _Parts(ctx.log)
    lfs = fetch_lfs(git_dir, user, password, parts, ctx.log)

    ctx.log("  archive metadata: repository, pull requests, activity, settings")
    repo = parts.run("repositories", lambda: get_json(bitbucket.bb_url(ctx), ctx.headers), {}, missing_ok=False)
    orgs = _organizations(ctx, repo, parts) if repo else []
    prs = parts.run("pull_requests", lambda: _pr_listing(ctx, parts), [], missing_ok=False)
    entries = parts.run("activity", lambda: _activity(ctx, prs, parts), [], missing_ok=False)
    split = split_activity(entries)
    protected = {
        "branch_restrictions": parts.run("protected_branches", lambda: bitbucket.cloud_paged(
            ctx, bitbucket.bb_url(ctx, "/branch-restrictions")), []),
        "branching_model": parts.run("protected_branches", lambda: get_json(
            bitbucket.bb_url(ctx, "/branching-model"), ctx.headers), None),
    }
    downloads = parts.run("releases", lambda: bitbucket.cloud_paged(ctx, bitbucket.bb_url(ctx, "/downloads")), [])
    hooks = parts.run("webhooks", lambda: bitbucket.cloud_paged(ctx, bitbucket.bb_url(ctx, "/hooks")), [])
    pipelines = parts.run("pipelines", lambda: bitbucket.cloud_paged(
        ctx, bitbucket.bb_url(ctx, "/pipelines/?sort=-created_on")), [],
        missing_note="Bitbucket Pipelines was never set up for this repository")
    schedules = parts.run("pipeline_schedules", lambda: bitbucket.cloud_paged(
        ctx, bitbucket.bb_url(ctx, "/pipelines_config/schedules/")), [])
    refs = write_pr_refs(git_dir, prs)

    for name, data in (("repositories_000001", [repo] if repo else []), ("organizations_000001", orgs),
                       ("pull_requests_000001", prs), ("pull_request_reviews_000001", split["reviews"]),
                       ("pull_request_review_comments_000001", split["review_comments"]),
                       ("issue_comments_000001", split["issue_comments"]),
                       ("issue_events_000001", split["issue_events"]), ("raw/pullrequest_activity", entries),
                       ("protected_branches_000001", protected), ("releases_000001", downloads),
                       ("webhooks_000001", hooks), ("pipelines_000001", pipelines),
                       ("pipeline_schedules_000001", schedules), ("users_000001", _users(prs, entries))):
        ctx.save_json(name, data)
    ctx.save_json("schema", {
        "format": FORMAT, "version": FORMAT_VERSION, "repository": ctx.target,
        "created_utc": datetime.now(timezone.utc).isoformat(), "git": f"{git_dir.name}/ (git clone --mirror)",
        "files": FILES, "not_available": NOT_AVAILABLE, "omitted_by_policy": OMITTED_BY_POLICY,
        "lfs": lfs, "pull_request_refs": refs, "notes": parts.notes, "errors": parts.errors,
    })
    return {"errors": parts.errors, "notes": parts.notes,
            "counts": {"pull_requests": len(prs), "activity": len(entries)}}


# ── the per-commit extras, from the mirror ──────────────────────────────────

def local_git(ctx) -> Optional[Path]:
    """The run's mirror of ctx.target, unpacked once into the run's work folder; None when the run has no mirror."""
    cache = ctx.shared.setdefault("bb_git", {})
    if ctx.target in cache:
        return cache[ctx.target]
    archive = (ctx.shared.get("bb_archives") or {}).get(ctx.target)
    found = None
    if archive and Path(archive).is_file():
        dest = Path(archive).parent / ".mirror-work" / f"{safe_name(ctx.target)}-extras"
        if not dest.exists():
            dest.mkdir(parents=True)
            with tarfile.open(archive) as tar:
                members = [m for m in tar.getmembers() if m.name.split("/", 1)[0].endswith(".git")]
                tar.extractall(dest, members=members, filter="data")
        found = next((p for p in sorted(dest.iterdir()) if p.name.endswith(".git") and p.is_dir()), None)
        if found:
            ctx.log("    from the repo's git mirror (no API calls)")
    cache[ctx.target] = found
    return found


def commits(ctx, git_dir: Path) -> list:
    raw = _git(git_dir, "log", "--all", "--date-order", "--format=%H%x00%P%x00%aI%x00%an <%ae>%x00%B%x1e")
    out = []
    for record in _text(raw).split("\x1e"):
        record = record.lstrip("\n")
        if not record:
            continue
        sha, parents, date, author, message = (record.split("\x00") + [""] * 5)[:5]
        out.append({"hash": sha, "date": date, "author": {"raw": author}, "message": message,
                    "parents": [{"hash": p} for p in parents.split()], "source": "git mirror"})
        if ctx.max_items and len(out) >= ctx.max_items:
            ctx.truncate("commits")
            break
    return out


def _base(git_dir: Path, sha: str) -> str:
    """First parent, or the empty tree for a root commit (what Bitbucket's diffstat compares against)."""
    parents = _text(_git(git_dir, "rev-parse", f"{sha}^@")).split()
    if parents:
        return parents[0]
    key = str(git_dir)
    if key not in _EMPTY_TREE:
        proc = subprocess.run(["git", "--git-dir", str(git_dir), "hash-object", "-t", "tree", "--stdin"],
                              input=b"", capture_output=True, check=True)
        _EMPTY_TREE[key] = _text(proc.stdout).strip()
    return _EMPTY_TREE[key]


STATUS = {"A": "added", "D": "removed", "M": "modified", "T": "modified", "R": "renamed", "C": "added"}


def diffstat(git_dir: Path, sha: str) -> list:
    base = _base(git_dir, sha)
    names = _text(_git(git_dir, "diff", "--name-status", "-z", "-M", base, sha)).split("\x00")
    nums = _text(_git(git_dir, "diff", "--numstat", "-z", "-M", base, sha)).split("\x00")
    changes, i = [], 0
    while i < len(names) and names[i]:
        code = names[i][0]
        if code in "RC":
            old, new, i = names[i + 1], names[i + 2], i + 3
        else:
            old = new = names[i + 1]
            i += 2
        changes.append((code, old, new))
    counts, j = [], 0
    while j < len(nums) and nums[j]:
        added, removed, path = (nums[j].split("\t") + ["", "", ""])[:3]
        j += 1 if path else 3          # a rename's paths follow as their own fields
        counts.append((0 if added == "-" else int(added), 0 if removed == "-" else int(removed)))
    out = []
    for (code, old, new), (added, removed) in zip(changes, counts):
        status = STATUS.get(code, "modified")
        out.append({"status": status, "lines_added": added, "lines_removed": removed,
                    "old": None if status == "added" else {"path": old},
                    "new": None if status == "removed" else {"path": new}})
    return out


def patch(git_dir: Path, sha: str) -> bytes:
    parents = _text(_git(git_dir, "rev-parse", f"{sha}^@")).split()
    if len(parents) <= 1:
        return _git(git_dir, "format-patch", "-1", "--root", "--stdout", "-M", sha)
    header = _git(git_dir, "show", "-s", "--format=From %H %ad%nFrom: %an <%ae>%nDate: %aD%nSubject: %s%n%n%b", sha)
    return header + b"---\n" + _git(git_dir, "diff", "-M", parents[0], sha)
