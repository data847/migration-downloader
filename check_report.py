"""`check-report.json`: per-repo checks on each archive, against the Codebase Profiler output when one is given.

Written beside `manifest.json` and rewritten (tmp + rename, under a lock) after every repo, so it is useful while the
run is still going. Checks per repo:

  1. valid       the archive opens and holds git data a `git` can read a commit from
                 (GitHub `repositories/<owner>/<repo>.git`, GitLab `project.bundle`, Bitbucket mirror `<repo>.git`)
  2. visibility  public / private / internal, from the archive's repository record, else the run's extras, else the
                 source snapshot for_check took
  3. prs         the PR records in the archive's PR files (never activity or event files), how many are merged, how
                 many carry their metadata, and whether that matches the source's own total
  4. loc         `scc` code lines of the profiler's measured branch, checked out from the archive, vs profiler `loc`
  5. merged_prs  merged PRs in the archive vs profiler `merged_prs` (or `local_merges` when that is blank)

Tolerance for 4 and 5: the profiler value is a floor. Migration >= profiler is OK; migration lower by more than the
tolerance is a mismatch; a profiler value of 0 or blank cannot be compared (null with a reason, never a mismatch).

Nothing here may fail a download: every check catches its own errors and records them in the repo's entry.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from migration_api import MigrationError, safe_name

REPORT_NAME = "check-report.json"
DEFAULT_TOLERANCE = 5.0
PROFILER_SUFFIXES = (".csv", ".xlsx", ".zip")
MAX_PROFILER_BYTES = 64 * 1024 * 1024        # a summary.csv of tens of thousands of repos is a few MB
MAX_ZIP_RATIO = 200                          # compressed -> uncompressed; beyond this it is a zip bomb
MAX_JSON_BYTES = 2 * 1024 * 1024 * 1024      # a PR file larger than this is not parsed (memory)
MIN_FREE_BYTES = 512 * 1024 * 1024           # stop unpacking git data below this much free disk
GIT_TIMEOUT = 3600
SCC_TIMEOUT = int(os.environ.get("MD_SCC_TIMEOUT", "600") or 600)
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")
SAFE_BRANCH = re.compile(r"[^\s~^:?*\[\\\-][^\s~^:?*\[\\]{0,250}")   # never an option, never a revision expression
VISIBILITY_LEVELS = {0: "private", 10: "internal", 20: "public"}

# PR record files only. Activity / event / review / comment files are never counted: Bitbucket's
# raw/pullrequest_activity.json alone holds ~5 entries per PR.
GITHUB_PR_FILE = re.compile(r"^pull_requests_\d+\.json$")
GITLAB_MR_FILE = "tree/project/merge_requests.ndjson"
GITLAB_PROJECT_FILE = "tree/project.json"
REPO_FILE = re.compile(r"^repositories_\d+\.json$")
GH_GIT = re.compile(r"^repositories/([^/]+)/([^/]+)\.git(?:/(.*))?$")
BB_GIT = re.compile(r"^([^/]+)\.git(?:/(.*))?$")


class SccMissing(Exception):
    pass


# ── profiler output ──────────────────────────────────────────────────────────

def load_profiler(path) -> List[dict]:
    """Rows of the profiler's summary (csv, xlsx or the run's .zip holding summary.csv). Raises MigrationError."""
    path = Path(path)
    if not path.is_file():
        raise MigrationError(f"profiler file not found: {path.name}")
    suffix = path.suffix.lower()
    if suffix not in PROFILER_SUFFIXES:
        raise MigrationError("the profiler output must be summary.csv, summary.xlsx or the profiler's .zip")
    if path.stat().st_size > MAX_PROFILER_BYTES and suffix != ".zip":
        raise MigrationError("the profiler file is too large")
    try:
        if suffix == ".csv":
            rows = _csv_rows(path.read_bytes())
        elif suffix == ".xlsx":
            rows = _xlsx_rows(path)
        else:
            rows = _csv_rows(_zip_summary(path))
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(f"could not read the profiler file: {type(exc).__name__}: {str(exc)[:200]}") from None
    rows = [r for r in rows if r.get("repo")]
    if not rows:
        raise MigrationError("the profiler file has no repo rows (expected the profiler's summary with a repo column)")
    return rows


def _clean(rows: Iterable[dict]) -> List[dict]:
    out = []
    for r in rows:
        out.append({str(k).strip().lower(): ("" if v is None else str(v).strip()) for k, v in r.items() if k})
    return out


def _csv_rows(raw: bytes) -> List[dict]:
    text = raw.decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    fields = [str(f or "").strip().lower() for f in reader.fieldnames or []]
    if "repo" not in fields:
        raise MigrationError("the profiler file has no 'repo' column; is it the profiler's summary.csv?")
    return _clean(reader)


def _xlsx_rows(path: Path) -> List[dict]:
    try:
        import openpyxl  # optional: Flask is the only hard dependency
    except ImportError:
        raise MigrationError("reading .xlsx needs openpyxl, which is not installed here; "
                             "upload summary.csv or the profiler's .zip instead") from None
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["summary"] if "summary" in wb.sheetnames else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [str(h or "").strip().lower() for h in next(it, [])]
        if "repo" not in header:
            raise MigrationError("the workbook's summary sheet has no 'repo' column")
        rows = [dict(zip(header, values)) for values in it]
    finally:
        wb.close()
    return _clean(rows)


def _zip_summary(path: Path) -> bytes:
    """summary.csv from the profiler's run zip, read in memory; nothing is extracted."""
    with zipfile.ZipFile(path) as zf:
        candidates = []
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            parts = name.split("/")
            if info.is_dir() or name.startswith("/") or ".." in parts or parts[-1] != "summary.csv":
                continue
            candidates.append((len(parts), name, info))
        if not candidates:
            raise MigrationError("the zip holds no summary.csv; is it the Codebase Profiler's output?")
        _depth, _name, info = min(candidates, key=lambda c: (c[0], c[1]))
        if info.file_size > MAX_PROFILER_BYTES:
            raise MigrationError("summary.csv in the zip is too large")
        if info.compress_size and info.file_size / max(1, info.compress_size) > MAX_ZIP_RATIO:
            raise MigrationError("summary.csv in the zip is compressed suspiciously well (zip bomb?)")
        with zf.open(info) as fh:
            data = fh.read(MAX_PROFILER_BYTES + 1)
        if len(data) > MAX_PROFILER_BYTES:
            raise MigrationError("summary.csv in the zip is too large")
        return data


def profiler_path(row: dict, provider: str) -> str:
    """The repo's path on the source as the downloader names it: GitLab org/subgroup/repo (the subgroup is the path
    between group and project), else org/repo (Bitbucket's subgroup is the project *name*, not part of the path)."""
    org, sub, repo = row.get("org", ""), row.get("subgroup", ""), row.get("repo", "")
    if provider == "gitlab" and sub:
        return "/".join(p.strip("/") for p in (org, sub, repo) if p)
    return "/".join(p for p in (org, repo) if p)


def targets_from_profiler(rows: List[dict], provider: str, scope: str = "auto", org: str = "") -> List[str]:
    """Every profiler repo as a run target for this provider (GitHub org/repo, GitLab org/subgroup/repo, Bitbucket
    workspace/repo). GitHub org scope takes bare names, and only of that org."""
    out: List[str] = []
    for row in rows:
        if provider == "github" and scope == "org":
            if org and row.get("org", "").lower() != org.lower():
                continue
            target = row.get("repo", "")
        else:
            target = profiler_path(row, provider)
        if target and target.lower() not in {t.lower() for t in out}:
            out.append(target)
    return out


# ── name matching (exact only, never fuzzy) ──────────────────────────────────

def normalise(name: str, ids: Iterable[str] = ()) -> str:
    """lowercase, no URL host, no .git, no archive suffix, no `-<migration/export id>` the downloader appended."""
    n = (name or "").strip().strip("/")
    if n.startswith(("http://", "https://")):
        n = re.sub(r"^https?://[^/]+/", "", n).strip("/")
    low = n.lower()
    for s in ARCHIVE_SUFFIXES:
        if low.endswith(s):
            n = n[: -len(s)]
            break
    for i in ids:
        if i and n.endswith(f"-{i}"):
            n = n[: -len(str(i)) - 1]
            break
    if n.lower().endswith(".git"):
        n = n[:-4]
    return n.lower()


def _bare(n: str) -> str:
    return n.split("/")[-1]


def _owner_repo(n: str) -> str:
    parts = n.split("/")
    return f"{parts[0]}/{parts[-1]}" if len(parts) >= 2 else n


def match_repos(repos: List[str], rows: List[dict], provider: str) -> Dict[str, dict]:
    """{repo: {"row": index or None, "by": "path"|"owner/repo"|"name"|"", "reason": ...}}.

    Exact tiers: the full path, then owner/repo (first and last segment), then the bare repo name. A tier matches only
    when its key is unique on *both* sides (one profiler row, one repo of this run); a name shared by two profiler
    rows, or by two repos of the run, is left unmatched and says so. Never fuzzy."""
    keys = [normalise(profiler_path(r, provider)) for r in rows]
    repos = list(dict.fromkeys(repos))
    tiers = [("path", lambda n: n), ("owner/repo", _owner_repo), ("name", _bare)]
    out: Dict[str, dict] = {}
    used: set = set()
    for label, fn in tiers:
        prof: Dict[str, List[int]] = {}
        for i, k in enumerate(keys):
            prof.setdefault(fn(k), []).append(i)
        mine: Dict[str, int] = {}
        for repo in repos:
            key = fn(normalise(repo))
            mine[key] = mine.get(key, 0) + 1
        for repo in repos:
            if out.get(repo, {}).get("row") is not None:
                continue
            key = fn(normalise(repo))
            hits = prof.get(key, [])
            if len(hits) == 1 and mine[key] == 1 and hits[0] not in used:
                out[repo] = {"row": hits[0], "by": label, "reason": ""}
                used.add(hits[0])
            elif hits and repo not in out:
                out[repo] = {"row": None, "by": "", "reason": f"ambiguous {label} '{key}': {len(hits)} profiler "
                                                               f"row(s), {mine[key]} repo(s) of this run"}
    for repo in repos:
        out.setdefault(repo, {"row": None, "by": "", "reason": "no profiler row with this name"})
    return out


# ── tolerance rule ───────────────────────────────────────────────────────────

def _number(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        f = float(str(value).replace(",", ""))
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def compare(migration, profiler, tolerance_pct: float) -> dict:
    """{"migration", "profiler", "diff_pct", "ok", "reason"}. ok None = not comparable (never a mismatch)."""
    m, p = _number(migration), _number(profiler)
    out = {"migration": m, "profiler": p, "diff_pct": None, "ok": None, "reason": ""}
    if p in (None, 0):
        out["reason"] = "profiler value is blank or 0: not comparable"
        return out
    if m is None:
        out["reason"] = out["reason"] or "migration value unknown: not comparable"
        return out
    out["diff_pct"] = round((m - p) / p * 100, 2)
    tol = Fraction(str(tolerance_pct))
    # exact arithmetic: lower by *more* than the tolerance is a mismatch; exactly at it is OK
    out["ok"] = not (Fraction(str(m)) * 100 < Fraction(str(p)) * (100 - tol))
    return out


# ── reading an archive ───────────────────────────────────────────────────────

class RepoData:
    """What one pass over an archive found about one repo inside it."""

    def __init__(self, repo: str):
        self.repo = repo
        self.git: Optional[Path] = None          # extracted bare git dir
        self.bundle: Optional[Path] = None       # extracted GitLab project.bundle
        self.meta: dict = {}                     # the repository record
        self.prs: List[dict] = []
        self.pr_files: List[str] = []


def _member_path(dest: Path, rel: str) -> Optional[Path]:
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or rel.startswith("/"):
        return None
    path = dest.joinpath(*parts)
    try:
        path.resolve().relative_to(dest.resolve())
    except ValueError:
        return None
    return path


def _extract(tar: tarfile.TarFile, ti: tarfile.TarInfo, dest: Path, rel: str) -> None:
    """One regular file or directory, written by hand: no links, devices, or paths outside dest."""
    path = _member_path(dest, rel)
    if path is None:
        return
    if ti.isdir():
        path.mkdir(parents=True, exist_ok=True)
        return
    if not ti.isfile():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    src = tar.extractfile(ti)
    if src is None:
        return
    with path.open("wb") as out:
        shutil.copyfileobj(src, out, 1024 * 1024)


SAFE_CONFIG = "[core]\n\trepositoryformatversion = {version}\n\tbare = true\n{extensions}"


def _new_git_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config").write_text(SAFE_CONFIG.format(version=0, extensions=""))
    return path


def _extract_git(tar: tarfile.TarFile, ti: tarfile.TarInfo, git_dir: Path, rel: str) -> None:
    """A member of an archived git dir. The archive's own `config` and `hooks/` are never used: a config can name
    commands (fsmonitor, filter drivers, ssh) that git would run. Only its object format survives."""
    rel = rel.strip("/")
    if rel == "hooks" or rel.startswith("hooks/"):
        return
    if rel == "config":
        fh = tar.extractfile(ti) if ti.isfile() else None
        text = fh.read(1024 * 1024).decode("utf-8", "replace") if fh else ""
        sha256 = re.search(r"(?im)^\s*objectformat\s*=\s*sha256\s*$", text)
        (git_dir / "config").write_text(SAFE_CONFIG.format(
            version=1 if sha256 else 0, extensions="[extensions]\n\tobjectformat = sha256\n" if sha256 else ""))
        return
    _extract(tar, ti, git_dir, rel)


def _json_member(tar: tarfile.TarFile, ti: tarfile.TarInfo):
    if ti.size > MAX_JSON_BYTES:
        raise MigrationError(f"{ti.name} is too large to parse ({ti.size} bytes)")
    fh = tar.extractfile(ti)
    return json.loads(fh.read().decode("utf-8", "replace")) if fh else None


def _ndjson_member(tar: tarfile.TarFile, ti: tarfile.TarInfo) -> List[dict]:
    fh = tar.extractfile(ti)
    out = []
    for line in (fh or []):
        line = line.strip()
        if line:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _url_repo(url: str) -> str:
    """owner/repo from https://host/owner/repo[/pull/1] (or an API URL .../repos/owner/repo)."""
    path = re.sub(r"^https?://[^/]+/", "", str(url or "")).strip("/")
    path = re.sub(r"^(?:api/v3/)?repos/", "", path)
    parts = path.split("/")
    return f"{parts[0]}/{parts[1]}".lower() if len(parts) >= 2 else ""


def _records(data) -> List[dict]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in ("values", "pull_requests", "merge_requests", "items"):
            if isinstance(data.get(key), list):
                return [d for d in data[key] if isinstance(d, dict)]
    return []


def scan_archive(archive: Path, labels: List[str], work: Optional[Path]) -> Tuple[str, Dict[str, RepoData], List[str]]:
    """(kind, {repo label: RepoData}, errors). One streaming pass: reads the repository and PR record files and,
    when `work` is given, unpacks each repo's git data under it. Raises on an archive that does not open."""
    kind = ""
    by_key: Dict[str, RepoData] = {}
    single = RepoData(labels[0] if len(labels) == 1 else "")
    errors: List[str] = []
    gh_prs: List[dict] = []
    gh_repos: List[dict] = []
    written = 0

    def unpacked(size: int) -> None:
        nonlocal written
        written += size
        if written > 256 * 1024 * 1024:
            written = 0
            if shutil.disk_usage(work).free < MIN_FREE_BYTES:
                raise MigrationError("not enough free disk to unpack the git data for checking")

    def gh(key: str) -> RepoData:
        if key not in by_key:
            by_key[key] = RepoData(key)
        return by_key[key]

    with tarfile.open(archive, "r|*") as tar:
        for ti in tar:
            name = re.sub(r"^(\./)+", "", ti.name)
            m = GH_GIT.match(name)
            if m:
                kind = "github"
                rd = gh(f"{m.group(1)}/{m.group(2)}".lower())
                if work is not None:
                    if rd.git is None:
                        rd.git = _new_git_dir(work / f"gh-{len(by_key)}-{safe_name(m.group(2))}.git")
                    if m.group(3):
                        _extract_git(tar, ti, rd.git, m.group(3))
                        unpacked(ti.size)
                continue
            if name == "project.bundle":
                kind = "gitlab"
                if work is not None and ti.isfile():
                    single.bundle = work / "project.bundle"
                    _extract(tar, ti, work, "project.bundle")
                    unpacked(ti.size)
                continue
            m = BB_GIT.match(name)
            if m:
                kind = kind or "bitbucket"
                if work is not None:
                    if single.git is None:
                        single.git = _new_git_dir(work / f"{safe_name(m.group(1))}.git")
                    if m.group(2):
                        _extract_git(tar, ti, single.git, m.group(2))
                        unpacked(ti.size)
                continue
            if not ti.isfile():
                continue
            try:
                if GITHUB_PR_FILE.match(name):
                    gh_prs += _records(_json_member(tar, ti))
                    single.pr_files.append(name)
                elif REPO_FILE.match(name):
                    gh_repos += _records(_json_member(tar, ti))
                elif name == GITLAB_MR_FILE:
                    kind = "gitlab"
                    single.prs += _ndjson_member(tar, ti)
                    single.pr_files.append(name)
                elif name == GITLAB_PROJECT_FILE:
                    kind = "gitlab"
                    data = _json_member(tar, ti)
                    single.meta = data if isinstance(data, dict) else {}
                elif name == "project.json" and not single.meta:     # legacy GitLab JSON export
                    data = _json_member(tar, ti)
                    if isinstance(data, dict):
                        kind = kind or "gitlab"
                        single.meta = data
                        if isinstance(data.get("merge_requests"), list) and GITLAB_MR_FILE not in single.pr_files:
                            single.prs += _records(data["merge_requests"])
                            single.pr_files.append("project.json#merge_requests")
            except MigrationError as exc:
                errors.append(str(exc))
            except ValueError as exc:
                errors.append(f"{name}: not valid JSON ({str(exc)[:120]})")

    if not kind and gh_repos:
        # no git data at all: tell a GitHub migration (html `url` strings) from a Bitbucket archive (API objects)
        bb = any(isinstance(r.get("links"), dict) or "full_name" in r for r in gh_repos)
        kind = "bitbucket" if bb else "github"
    if kind == "github":
        # repos and PRs carry the repo's URL: split them per repo (one archive may hold several)
        for rec in gh_repos:
            key = _url_repo(rec.get("url") or rec.get("html_url") or "")
            if key:
                gh(key).meta = rec
        for rec in gh_prs:
            ref = rec.get("repository")
            if isinstance(ref, dict):
                ref = ref.get("html_url") or ref.get("url")
            key = _url_repo(ref or "") or _url_repo(rec.get("html_url") or rec.get("url") or "")
            if key:
                gh(key).prs.append(rec)
        for rd in by_key.values():
            rd.pr_files = list(single.pr_files)
        out: Dict[str, RepoData] = {}
        want = {normalise(label): label for label in labels}
        for key, rd in by_key.items():
            label = want.get(key) or (labels[0] if len(labels) == 1 and len(by_key) == 1 else key)
            rd.repo = label
            out[label] = rd
        return kind, out, errors
    # one repo per archive (GitLab export, Bitbucket mirror)
    if not kind and gh_repos:
        kind = "bitbucket"
    if gh_prs:
        single.prs += gh_prs
    if gh_repos and not single.meta:
        single.meta = gh_repos[0]
    single.repo = single.repo or (labels[0] if labels else "")
    return kind, {single.repo: single}, errors


# ── per-repo facts ───────────────────────────────────────────────────────────

def pr_stats(prs: List[dict]) -> dict:
    """PR records -> counts. Merged = merged_at set, or state merged. With metadata = a record that carries its
    identity (number / iid / id / url) and title, i.e. a real PR record rather than a stub."""
    seen = set()
    total = merged = with_meta = 0
    for rec in prs:
        ident = next((rec.get(k) for k in ("number", "iid", "id", "url", "html_url") if rec.get(k) not in (None, "")),
                     None)
        key = str(ident) if ident is not None else f"#{id(rec)}"
        if key in seen:
            continue
        seen.add(key)
        total += 1
        state = str(rec.get("state") or "").lower()
        metrics = rec.get("metrics") if isinstance(rec.get("metrics"), dict) else {}
        if rec.get("merged_at") or metrics.get("merged_at") or state == "merged" or rec.get("merged") is True:
            merged += 1
        if ident is not None and str(rec.get("title") or "").strip():
            with_meta += 1
    return {"in_archive": total, "merged": merged, "with_metadata": with_meta}


def visibility_of(meta: dict) -> Optional[str]:
    if not isinstance(meta, dict) or not meta:
        return None
    vis = meta.get("visibility")
    if isinstance(vis, str) and vis.strip():
        return vis.strip().lower()
    level = meta.get("visibility_level")
    if level not in (None, ""):
        try:
            return VISIBILITY_LEVELS.get(int(level))
        except (TypeError, ValueError):
            pass
    for key in ("private", "is_private", "isPrivate"):
        if isinstance(meta.get(key), bool):
            return "private" if meta[key] else "public"
    return None


def _git_env() -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ASKPASS": "", "GIT_LFS_SKIP_SMUDGE": "1"}


# hooks off, no transports, no LFS smudge: the archive is data, never code to run
GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never", "-c", "core.fsmonitor=false",
            "-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
            "-c", "core.autocrlf=false"]


def git(git_dir: Path, *args: str, env: Optional[dict] = None, check: bool = True) -> str:
    try:
        proc = subprocess.run(["git", *GIT_SAFE, "--git-dir", str(git_dir), *args], capture_output=True,
                              env={**_git_env(), **(env or {})}, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        raise MigrationError("git is not installed") from None
    except subprocess.TimeoutExpired:
        raise MigrationError(f"git {args[0]} timed out") from None
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise MigrationError(f"git {args[0]} failed: {err[-1] if err else proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")


def git_from_bundle(bundle: Path, dest: Path) -> Path:
    """A bare repo holding the bundle's refs (unbundle + update-ref: no transport is involved)."""
    subprocess.run(["git", "init", "-q", "--bare", str(dest)], capture_output=True, env=_git_env(), check=True,
                   timeout=120)
    heads = git(dest, "bundle", "unbundle", str(bundle))
    head_sha = ""
    branches = []
    for line in heads.splitlines():
        sha, _, ref = line.strip().partition(" ")
        if ref == "HEAD":
            head_sha = sha
        elif ref.startswith("refs/"):
            git(dest, "update-ref", ref, sha)
            if ref.startswith("refs/heads/"):
                branches.append((ref, sha))
    target = next((ref for ref, sha in branches if sha == head_sha), branches[0][0] if branches else "")
    if target:
        git(dest, "symbolic-ref", "HEAD", target)
    return dest


def resolve_commit(git_dir: Path, branches: List[str]) -> Tuple[str, str, str]:
    """(branch used, commit sha, note). Tries each named branch, then HEAD."""
    note = ""
    for b in [b for b in branches if b]:
        if not SAFE_BRANCH.fullmatch(b):
            note = note or f"branch name '{b[:60]}' ignored (not a plain branch name)"
            continue
        for ref in (f"refs/heads/{b}", b):
            sha = git(git_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip()
            if sha:
                return b, sha, note
        note = note or f"branch '{b}' is not in the archive"
    sha = git(git_dir, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False).strip()
    if sha:
        head = git(git_dir, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
        return head or "HEAD", sha, note
    any_ref = git(git_dir, "for-each-ref", "--count=1", "--format=%(objectname) %(refname:short)",
                  "refs/heads/", check=False).strip()
    if any_ref:
        sha, _, name = any_ref.partition(" ")
        if git(git_dir, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False).strip():
            return name, sha, note or "HEAD does not resolve"
    return "", "", note


def checkout(git_dir: Path, sha: str, dest: Path) -> None:
    """The commit's tree written into dest (read-tree + checkout-index into a private index; no hooks, no smudge)."""
    dest.mkdir(parents=True, exist_ok=True)
    index = {"GIT_INDEX_FILE": str(dest.parent / f"{dest.name}.index")}
    git(git_dir, "read-tree", sha, env=index)
    git(git_dir, "--work-tree", str(dest), "-c", "core.bare=false", "checkout-index", "-a", "-f",
        f"--prefix={dest}/", env=index)


def scc_loc(tree: Path) -> int:
    """Code lines as the profiler counts them: `scc --format json`, summing Code over languages."""
    exe = shutil.which("scc")
    if not exe:
        raise SccMissing("scc not installed")
    proc = subprocess.run([exe, "--format", "json", str(tree)], capture_output=True, text=True, timeout=SCC_TIMEOUT)
    if proc.returncode != 0:
        raise MigrationError("scc failed: " + (proc.stderr or proc.stdout or "")[-300:])
    data = json.loads(proc.stdout or "[]")
    return sum(int(r.get("Code") or r.get("code") or 0) for r in data) if isinstance(data, list) else 0


# ── the report ───────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_counts(fc_repo: Optional[dict]) -> dict:
    """counts_after, else counts_before, from the for_check snapshot (blank = unknown)."""
    fc_repo = fc_repo or {}
    for tag in ("after", "before"):
        counts = (fc_repo.get(tag) or {}).get("counts") or {}
        if counts:
            return counts
    return {}


class CheckReport:
    """The run's check-report.json. Thread safe: GitLab exports run concurrently."""

    def __init__(self, run_dir: Path, provider: str, *, rows: Optional[List[dict]] = None, profiler_file: str = "",
                 loc_tolerance: float = DEFAULT_TOLERANCE, pr_tolerance: float = DEFAULT_TOLERANCE,
                 expected: Iterable[str] = (), log: Callable[[str], None] = lambda _m: None,
                 loc_fn: Callable[[Path], int] = None):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / REPORT_NAME
        self.provider = provider
        self.rows = rows or []
        self.profiler_file = profiler_file
        self.loc_tol = float(loc_tolerance)
        self.pr_tol = float(pr_tolerance)
        self.log = log
        self.loc_fn = loc_fn or (lambda tree: scc_loc(tree))
        self.lock = threading.Lock()
        self._mlock = threading.Lock()
        self.entries: Dict[str, dict] = {}
        self.failed: Dict[str, str] = {}
        self.complete = False
        self.expected = list(dict.fromkeys(expected))
        self._matches = match_repos(self.expected, self.rows, provider) if self.rows else {}

    # previous report (resume): its entries stand until this run re-checks the repo
    def load_previous(self) -> None:
        try:
            old = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        for e in old.get("repos", []) if isinstance(old, dict) else []:
            if isinstance(e, dict) and e.get("repo"):
                self.entries[e["repo"]] = e

    def previous(self, repo: str, archive: str) -> Optional[dict]:
        e = self.entries.get(repo)
        return e if e and e.get("archive") == archive and not e.get("errors") else None

    def _match(self, repo: str) -> dict:
        if not self.rows:
            return {"row": None, "by": "", "reason": "no profiler output given"}
        with self._mlock:
            if repo not in self._matches:
                # a repo the targets did not name (one archive holding several): matched with the rest
                self.expected.append(repo)
                self._matches = match_repos(self.expected, self.rows, self.provider)
            return self._matches.get(repo) or {"row": None, "by": "", "reason": "no profiler row with this name"}

    def row_for(self, repo: str) -> Optional[dict]:
        idx = self._match(repo).get("row")
        return self.rows[idx] if idx is not None else None

    # ── recording ──
    def record_failure(self, repos: Iterable[str], error: str) -> None:
        with self.lock:
            for repo in repos:
                self.failed[repo] = error
            self._write()

    def check_archive(self, archive: Path, labels: List[str], fc_info: Optional[dict] = None,
                      work_root: Optional[Path] = None, reuse: bool = False) -> List[dict]:
        """Check every repo in one archive and write the report. Never raises."""
        entries: List[dict] = []
        try:
            entries = self._check_archive(Path(archive), labels, fc_info, work_root, reuse)
        except Exception as exc:  # the report is a by-product: it never fails the download
            entries = [self._blank(label, Path(archive).name, errors=[f"{type(exc).__name__}: {str(exc)[:300]}"])
                       for label in labels]
            for e in entries:
                e["valid"], e["valid_reason"] = None, "the check itself failed (see errors)"
        with self.lock:
            for e in entries:
                self.failed.pop(e["repo"], None)
                self.entries[e["repo"]] = e
            try:
                self._write()
            except Exception as exc:
                self.log(f"  warning: could not write {REPORT_NAME} ({type(exc).__name__}: {exc})")
        return entries

    def _blank(self, repo: str, archive: str, errors=None) -> dict:
        match = self._match(repo)
        row = self.rows[match["row"]] if match.get("row") is not None else None
        return {
            "repo": repo, "archive": archive,
            "profiler_repo": profiler_path(row, self.provider) if row else None,
            "profiler_match": match.get("by") or None,
            "profiler_match_reason": "" if row else match.get("reason", ""),
            "valid": None, "valid_reason": "",
            "visibility": None, "visibility_source": "", "visibility_reason": "",
            "prs": {"source_total": None, "in_archive": None, "merged": None, "with_metadata": None,
                    "count_match": None, "files": []},
            "loc": {"migration": None, "profiler": None, "diff_pct": None, "ok": None, "branch": None,
                    "commit": None, "reason": ""},
            "merged_prs": {"migration": None, "profiler": None, "profiler_field": None, "diff_pct": None,
                           "ok": None, "reason": ""},
            "mismatch_reasons": [], "notes": [], "errors": list(errors or []), "checked_at": _now(),
        }

    def _check_archive(self, archive: Path, labels: List[str], fc_info, work_root, reuse) -> List[dict]:
        if reuse:
            prev = [self.previous(label, archive.name) for label in labels]
            if all(prev):
                return [dict(p) for p in prev]
        fc_repos = {normalise(r.get("label", "")): r for r in (fc_info or {}).get("repos", [])}
        work_root = Path(work_root or self.run_dir)
        work_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=".check-", dir=work_root))
        try:
            try:
                kind, found, scan_errors = scan_archive(archive, labels, work)
            except (tarfile.TarError, OSError, EOFError, zipfile.BadZipFile) as exc:
                out = []
                for label in labels:
                    e = self._blank(label, archive.name)
                    e["valid"], e["valid_reason"] = False, f"archive does not open: {type(exc).__name__}: {exc}"
                    self._finish_entry(e, None, None)
                    out.append(e)
                return out
            names = list(labels) + [k for k in found if k not in labels]
            out = []
            for label in names:
                rd = found.get(label)
                e = self._blank(label, archive.name, errors=scan_errors)
                try:
                    self._fill(e, rd, kind, fc_repos.get(normalise(label)), work)
                except Exception as exc:
                    e["errors"].append(f"{type(exc).__name__}: {str(exc)[:300]}")
                out.append(e)
                if rd is not None:          # free the disk as soon as each repo is done
                    for p in (rd.git, rd.bundle):
                        if p is not None:
                            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
            return out
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _fill(self, e: dict, rd: Optional[RepoData], kind: str, fc_repo: Optional[dict], work: Path) -> None:
        row = self.row_for(e["repo"])
        source = _source_counts(fc_repo)
        # 2. visibility
        vis = visibility_of(rd.meta) if rd else None
        if vis:
            e["visibility"], e["visibility_source"] = vis, "archive"
        elif source.get("visibility"):
            e["visibility"], e["visibility_source"] = str(source["visibility"]).lower(), "source snapshot"
        else:
            e["visibility_reason"] = "no repository record in the archive and no source snapshot"
        # 3. PRs
        if rd is not None:
            stats = pr_stats(rd.prs)
            e["prs"].update(stats, files=list(rd.pr_files))
            if not rd.pr_files:
                e["notes"].append("no PR record file in the archive: 0 PRs counted")
        total = _number(source.get("prs"))
        e["prs"]["source_total"] = total
        if total is not None and e["prs"]["in_archive"] is not None:
            e["prs"]["count_match"] = e["prs"]["in_archive"] == total
            if not e["prs"]["count_match"]:
                e["notes"].append(f"archive has {e['prs']['in_archive']} PR record(s), the source reported {total}")
        # 1. valid, and 4. LOC from the same checkout
        git_dir = None
        if rd is None:
            e["valid"], e["valid_reason"] = False, f"repo not found in the archive ({kind or 'unknown layout'})"
        elif rd.bundle is not None:
            try:
                git_dir = git_from_bundle(rd.bundle, work / f"bundle-{safe_name(e['repo'])}.git")
            except Exception as exc:
                e["valid"], e["valid_reason"] = False, f"project.bundle unreadable: {str(exc)[:200]}"
        elif rd.git is not None:
            git_dir = rd.git
        else:
            e["valid"] = False
            e["valid_reason"] = {"github": "no git data in the archive (repositories/<owner>/<repo>.git missing)",
                                 "gitlab": "no project.bundle in the archive",
                                 }.get(kind, "no git data in the archive")
        if git_dir is not None:
            branches = []
            if row:
                branches = [row.get("measured_branch", ""), row.get("default_branch", "")]
            if rd and rd.meta:
                mb = rd.meta.get("mainbranch")
                branches.append(rd.meta.get("default_branch") or (mb.get("name") if isinstance(mb, dict) else ""))
            try:
                branch, sha, note = resolve_commit(git_dir, [b for b in branches if b])
            except MigrationError as exc:
                branch, sha, note = "", "", str(exc)
            if not sha:
                e["valid"], e["valid_reason"] = False, ("git cannot read a commit from the archive's git data"
                                                        + (f" ({note})" if note else ""))
            else:
                e["valid"], e["valid_reason"] = True, ""
                if note:
                    e["notes"].append(note)
                e["loc"]["branch"], e["loc"]["commit"] = branch, sha
                if row is not None:
                    self._measure(e, git_dir, sha, work)
        self._finish_entry(e, row, rd)

    def _measure(self, e: dict, git_dir: Path, sha: str, work: Path) -> None:
        tree = work / f"tree-{safe_name(e['repo'])}"
        try:
            checkout(git_dir, sha, tree)
            e["loc"]["migration"] = self.loc_fn(tree)
        except SccMissing as exc:
            e["loc"]["reason"] = str(exc)
        except Exception as exc:
            e["loc"]["reason"] = f"could not measure LOC: {type(exc).__name__}: {str(exc)[:200]}"
        finally:
            shutil.rmtree(tree, ignore_errors=True)
            (tree.parent / f"{tree.name}.index").unlink(missing_ok=True)

    def _finish_entry(self, e: dict, row: Optional[dict], rd: Optional[RepoData]) -> None:
        """The profiler comparisons and the mismatch verdict, from what is recorded in the entry."""
        if row is None:
            row = self.row_for(e["repo"])
        reasons = []
        if e["valid"] is False:
            reasons.append(f"invalid: {e['valid_reason']}")
        if row is not None:
            loc = compare(e["loc"]["migration"], row.get("loc"), self.loc_tol)
            e["loc"].update(profiler=loc["profiler"], diff_pct=loc["diff_pct"], ok=loc["ok"])
            if loc["ok"] is None and not e["loc"]["reason"]:
                e["loc"]["reason"] = loc["reason"]
            field = "merged_prs"
            if row.get("merged_prs", "") == "":
                field = "local_merges"
                e["notes"].append("profiler merged_prs is blank: compared with local_merges (merge commits in "
                                  "the history), which is not the same thing as merged PRs")
            mp = compare(e["prs"]["merged"], row.get(field), self.pr_tol)
            e["merged_prs"].update(migration=mp["migration"], profiler=mp["profiler"], profiler_field=field,
                                   diff_pct=mp["diff_pct"], ok=mp["ok"], reason=mp["reason"])
            if loc["ok"] is False:
                reasons.append(f"loc {abs(loc['diff_pct'])}% below profiler ({loc['migration']} vs {loc['profiler']})")
            if mp["ok"] is False:
                reasons.append(f"merged PRs {abs(mp['diff_pct'])}% below profiler {field} "
                               f"({mp['migration']} vs {mp['profiler']})")
        e["mismatch_reasons"] = reasons
        e["notes"] = list(dict.fromkeys(e["notes"]))

    # ── final pass ──
    def finish(self, extras_root: Optional[Path] = None) -> dict:
        """Fill what only the run's extras know (visibility, Bitbucket PR listing), set complete, write."""
        with self.lock:
            for e in self.entries.values():
                try:
                    self._from_extras(e, extras_root)
                    self._finish_entry(e, None, None)
                except Exception as exc:
                    e["errors"].append(f"final pass: {type(exc).__name__}: {str(exc)[:200]}")
            self.complete = True
            return self._write()

    def _from_extras(self, e: dict, extras_root: Optional[Path]) -> None:
        if not extras_root or not Path(extras_root).is_dir():
            return
        dirs = [Path(extras_root) / safe_name(e["repo"]), Path(extras_root) / safe_name(_bare(e["repo"]))]
        d = next((p for p in dirs if p.is_dir()), None)
        if d is None:
            return
        if not e.get("visibility"):
            try:
                vis = visibility_of(json.loads((d / "repo-metadata.json").read_text()))
            except (OSError, ValueError):
                vis = None
            if vis:
                e["visibility"], e["visibility_source"], e["visibility_reason"] = vis, "extras repo-metadata.json", ""
        if not e["prs"].get("files") and (d / "pullrequests.json").is_file():
            try:
                prs = _records(json.loads((d / "pullrequests.json").read_text()))
            except (OSError, ValueError) as exc:
                e["errors"].append(f"extras pullrequests.json: {exc}")
                return
            e["prs"].update(pr_stats(prs), files=[f"extras/{d.name}/pullrequests.json"])
            e["notes"] = [n for n in e["notes"] if not n.startswith("no PR record file")]
            total = e["prs"].get("source_total")
            if total is not None:
                e["prs"]["count_match"] = e["prs"]["in_archive"] == total

    # ── output ──
    def to_dict(self) -> dict:
        entries = list(self.entries.values())
        matched_rows = set()
        for e in entries:
            m = self._match(e["repo"])
            if m.get("row") is not None:
                matched_rows.add(m["row"])
        not_migrated: List[str] = []
        if self.rows:
            if self.complete:
                not_migrated = [profiler_path(r, self.provider) for i, r in enumerate(self.rows)
                                if i not in matched_rows]
            else:   # while running only the failures are known; the rest may still come
                for repo in self.failed:
                    m = self._match(repo)
                    if m.get("row") is not None and m["row"] not in matched_rows:
                        not_migrated.append(profiler_path(self.rows[m["row"]], self.provider))
        failed_unmatched = [] if self.rows else list(self.failed)
        return {
            "mismatch": [e["repo"] for e in entries if e.get("mismatch_reasons")],
            "not_migrated": not_migrated + failed_unmatched,
            "not_in_profiler": [e["repo"] for e in entries if self.rows and e.get("profiler_repo") is None],
            "generated_at": _now(),
            "run": self.run_dir.name,
            "complete": self.complete,
            "provider": self.provider,
            "profiler_file": self.profiler_file or None,
            "tolerances": {"loc_pct": self.loc_tol, "pr_pct": self.pr_tol},
            "failed_downloads": [{"repo": r, "error": err} for r, err in self.failed.items()],
            "repos": entries,
        }

    def _write(self) -> dict:
        data = self.to_dict()
        tmp = self.path.with_name(self.path.name + ".part")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        os.replace(tmp, self.path)
        return data


def one_line(e: dict) -> str:
    """A log line for one repo's checks."""
    parts = [e.get("repo", "")]
    if e.get("valid"):
        parts.append("valid")
    elif e.get("valid") is False:
        parts.append(f"INVALID ({e.get('valid_reason', '')})")
    else:
        parts.append("validity unknown")
    parts.append(e.get("visibility") or "visibility unknown")
    prs = e.get("prs") or {}
    if prs.get("in_archive") is not None:
        src = f" of {prs['source_total']} on the source" if prs.get("source_total") is not None else ""
        parts.append(f"{prs['in_archive']} PR(s){src}, {prs.get('merged')} merged")
    loc = e.get("loc") or {}
    if loc.get("migration") is not None:
        parts.append(f"loc {loc['migration']}" + (f" vs {loc['profiler']}" if loc.get("profiler") is not None else ""))
    elif loc.get("reason"):
        parts.append(f"loc: {loc['reason']}")
    if e.get("mismatch_reasons"):
        parts.append("MISMATCH: " + "; ".join(e["mismatch_reasons"]))
    return " · ".join(parts)


def summary(path: Path) -> Optional[dict]:
    """The banner's view of a report on disk: the lists, without the per-repo detail."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    reasons = {e.get("repo"): e.get("mismatch_reasons", []) for e in data.get("repos", []) if isinstance(e, dict)}
    return {k: data.get(k) for k in ("mismatch", "not_migrated", "not_in_profiler", "complete", "run",
                                     "profiler_file", "tolerances")} | {
        "reasons": {r: reasons.get(r, []) for r in data.get("mismatch") or []}}
