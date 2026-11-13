"""Job orchestration for the migration downloader.

One `run_job()` call executes the whole documented flow for a batch of repos
and writes every archive under `outputs/migration-downloader/<run-id>/`,
alongside a `manifest.json` describing the run.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urlparse

import bootstrap  # noqa: F401  (sets sys.path)

import for_check  # noqa: E402
from datalabs_paths import (  # noqa: E402
    ensure_outputs,
    env,
    github_token,
    gitlab_token,
    load_env,
    outputs_for,
)

from migration_api import (  # noqa: E402
    ArchiveResult,
    GitHubMigration,
    GitLabExport,
    MigrationError,
    brief_error,
    human,
    mask,
    safe_name,
    sha256,
)

import bitbucket  # noqa: E402,F401  (registers the bitbucket collectors)
import bitbucket_archive  # noqa: E402
import redact  # noqa: E402
import ratelimit  # noqa: E402
from safety import MIN_FREE_MB, check_disk, http_url, require_valid_target, trusted_api_base, valid_target  # noqa: E402
from bitbucket import BITBUCKET_CLOUD_BASE, BitbucketDCExport, cloud_headers  # noqa: E402
from supplementary import REGISTRY, Ctx, resolve_names  # noqa: E402
import supplementary  # noqa: E402

COMPONENT = "migration-downloader"
PROVIDERS = {"github", "gitlab", "bitbucket", "bitbucket-dc"}
load_env()


def _strip_url(target: str) -> str:
    """The repo path from a clone/web URL, else the target as typed."""
    target = target.strip()
    path = urlparse(target).path if target.startswith(("http://", "https://")) else target
    path = path.strip("/")
    return path[:-4] if path.endswith(".git") else path


def _parse_owner_repo(target: str) -> str:
    """owner/repo from a URL, .git name or plain pair; auto scope needs the owner."""
    path = urlparse(target).path if target.startswith(("http://", "https://")) else target
    path = path.strip().strip("/")
    path = path[:-4] if path.endswith(".git") else path
    if path.count("/") != 1 or not all(path.split("/")):
        raise MigrationError(
            f"'{target}' should be owner/repo (the owner tells personal repos from an org's)")
    return path


def _int(data: dict, key: str, default: int) -> int:
    try:
        return int(data.get(key) or default)
    except (TypeError, ValueError):
        raise MigrationError(f"{key} must be a whole number") from None


def _max_items(raw) -> int:
    """Blank -> no limit (the default); 0 -> no limit; anything else is the cap."""
    if raw in (None, ""):
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        raise MigrationError("max_items must be a whole number") from None


def _split(raw) -> List[str]:
    return [t.strip() for chunk in str(raw or "").splitlines() for t in chunk.split(",") if t.strip()]


@dataclass
class JobSpec:
    provider: str = "github"          # github | gitlab
    scope: str = "auto"               # github only: auto | user | org
    org: str = ""                     # github org scope
    targets: List[str] = field(default_factory=list)
    token: str = ""                   # blank -> from .env
    api_base: str = ""                # blank -> provider default
    api_version: str = ""             # github only
    lock_repositories: bool = False   # github only
    single_archive: bool = False      # github only: one migration for all repos
    poll_interval: int = 15
    timeout: int = 3600
    verify_checksum: bool = True
    run_name: str = ""
    # resume: {target label -> migration/export id already known}
    known_ids: dict = field(default_factory=dict)
    skip_done: bool = False           # don't re-download targets already on disk
    concurrency: int = 1              # gitlab: projects exported in parallel
    for_check: bool = True            # write <archive>.for-check.csv (source-side counts/refs/file list) for Garmr
    # supplementary collectors (see supplementary.py / bitbucket.py)
    extras: List[str] = field(default_factory=list)   # names, or all / everything
    skip_extras: List[str] = field(default_factory=list)  # names unticked from `extras` (opt out)
    max_items: int = 0                # cap per unbounded listing (runs, commits, PRs…)
    # github migration options
    exclude: List[str] = field(default_factory=list)  # metadata, git_data, attachments, releases, owner_projects
    org_metadata_only: bool = False
    unlock_repos: bool = False        # github: release the lock after download
    delete_archive: bool = False      # github: delete the archive from GitHub after download
    # gitlab project export options
    upload_url: str = ""              # GitLab pushes the archive here instead of us downloading it
    upload_method: str = ""           # PUT (default) | POST
    description: str = ""
    # bitbucket data center export job
    dc_action: str = "export"         # export | preview | cancel | none
    dc_job_id: str = ""               # for cancel
    # bitbucket cloud: the full git history by git clone --mirror, packed as the repo's archive
    mirror: bool = True

    @classmethod
    def from_form(cls, data: dict) -> "JobSpec":
        def flag(key: str) -> bool:
            return str(data.get(key, "")).lower() in {"1", "true", "on", "yes"}

        raw = str(data.get("targets", ""))
        targets = [t.strip() for chunk in raw.splitlines() for t in chunk.split(",") if t.strip()]
        return cls(
            provider=(data.get("provider") or "github").strip().lower(),
            scope=(data.get("scope") or "auto").strip().lower(),
            org=(data.get("org") or "").strip(),
            targets=targets,
            token=(data.get("token") or "").strip(),
            api_base=(data.get("api_base") or "").strip(),
            api_version=(data.get("api_version") or "").strip(),
            lock_repositories=flag("lock_repositories"),
            single_archive=flag("single_archive"),
            poll_interval=max(5, _int(data, "poll_interval", 15)),
            concurrency=min(16, max(1, _int(data, "concurrency", 1))),
            timeout=max(60, _int(data, "timeout", 3600)),
            verify_checksum=flag("verify_checksum"),
            run_name=(data.get("run_name") or "").strip(),
            for_check=str(data.get("for_check", "1")).lower() not in {"0", "false", "off", "no"},
            extras=_split(data.get("extras", "")),
            skip_extras=_split(data.get("skip_extras", "")),
            max_items=_max_items(data.get("max_items")),
            exclude=_split(data.get("exclude", "")),
            org_metadata_only=flag("org_metadata_only"),
            unlock_repos=flag("unlock_repos"),
            delete_archive=flag("delete_archive"),
            upload_url=(data.get("upload_url") or "").strip(),
            upload_method=(data.get("upload_method") or "").strip().upper(),
            description=(data.get("description") or "").strip(),
            dc_action=(data.get("dc_action") or "export").strip().lower(),
            dc_job_id=(data.get("dc_job_id") or "").strip(),
            mirror=str(data.get("mirror", "1")).lower() not in {"0", "false", "off", "no"},
        )

    def resolved_token(self) -> str:
        if self.token:
            return self.token
        return {
            "github": github_token,
            "gitlab": gitlab_token,
            "bitbucket": lambda: env("BITBUCKET_TOKEN", "BB_TOKEN"),
            "bitbucket-dc": lambda: env("BITBUCKET_DC_TOKEN", "BITBUCKET_TOKEN"),
        }.get(self.provider, lambda: "")()

    def token_env_hint(self) -> str:
        return {
            "github": "GITHUB_TOKEN / GH_TOKEN",
            "gitlab": "GITLAB_TOKEN",
            "bitbucket": "BITBUCKET_TOKEN / BB_TOKEN",
            "bitbucket-dc": "BITBUCKET_DC_TOKEN",
        }.get(self.provider, "")

    def validate(self) -> None:
        if self.provider not in PROVIDERS:
            raise MigrationError(f"provider must be one of {sorted(PROVIDERS)}")
        check_run_name(self.run_name)
        cancel_only = self.provider == "bitbucket-dc" and self.dc_action == "cancel"
        if not self.targets and not cancel_only:
            raise MigrationError("give at least one repository / project")
        if self.provider == "github":
            if self.scope not in {"auto", "user", "org"}:
                raise MigrationError("scope must be auto, user or org")
            if self.scope == "org" and not self.org:
                raise MigrationError("an organization name is required for org scope")
            if self.scope == "auto":
                for target in self.targets:
                    _parse_owner_repo(target)     # every target must say who owns it
        for target in self.targets:
            require_valid_target(_strip_url(target))
        if self.provider == "bitbucket" and not self.extras and not self.mirror:
            raise MigrationError("Bitbucket Cloud has no export archive: keep the git mirror on, or pass extras (e.g. all, pullrequests)")
        if self.provider == "bitbucket-dc":
            if not self.api_base:
                raise MigrationError("Bitbucket Data Center needs --api-base (its base URL)")
            if self.dc_action not in {"export", "preview", "cancel", "none"}:
                raise MigrationError("dc_action must be export, preview, cancel or none")
            if cancel_only and not self.dc_job_id:
                raise MigrationError("dc_action cancel needs dc_job_id")
        if self.extras:
            resolve_names(self.provider, self.extras)   # fail fast on a typo
        if self.skip_extras:
            resolve_names(self.provider, self.skip_extras)
        if self.upload_url and self.provider != "gitlab":
            raise MigrationError("upload_url only applies to GitLab")
        if self.upload_url:
            http_url(self.upload_url)
        if self.upload_method not in {"", "PUT", "POST"}:
            raise MigrationError("upload_method must be PUT or POST")
        if self.scope not in {"auto", "user", "org"}:
            raise MigrationError("scope must be auto, user or org")
        if self.org and not (valid_target(self.org) and "/" not in self.org):
            raise MigrationError("org is not a valid organization name")
        if self.api_base:
            parts = urlparse(self.api_base)
            if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
                raise MigrationError("api_base must be an http(s) URL")
        if self.api_version and not re.fullmatch(r"[0-9A-Za-z._-]{1,32}", self.api_version):
            raise MigrationError("api_version looks invalid (expected e.g. 2022-11-28)")
        if self.dc_job_id and not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", self.dc_job_id):
            raise MigrationError("dc_job_id looks invalid")
        if self.unlock_repos and not self.lock_repositories:
            raise MigrationError("unlock_repos only makes sense with lock_repositories")
        if not self.token and not trusted_api_base(self.provider, self.api_base):
            raise MigrationError(
                "paste a token to use a custom API base: the token stored in .env is only sent to the "
                "default host (list self-hosted hosts in MD_TRUSTED_API_HOSTS to allow it)")
        if not self.resolved_token():
            raise MigrationError(
                f"no token supplied and none found in .env ({self.token_env_hint()})")


def existing_archive(run_dir: Path, stem: str, ids=()) -> Optional[Path]:
    """A finished `{stem}[-{id}].tar.gz` in the run dir, else None.

    Archives are named with the migration/export id, which a crash between the
    download and the manifest write never recorded, so a numeric suffix is
    matched too (exact pattern, so `acme-api` never claims `acme-api-v2-9.tar.gz`)."""
    pattern = re.compile(rf"^{re.escape(stem)}(?:-\d+)?\.tar\.gz$")
    names = [f"{stem}-{i}.tar.gz" for i in ids if i] + [f"{stem}.tar.gz"]
    try:
        found = [p for p in run_dir.iterdir() if p.is_file() and p.stat().st_size and pattern.match(p.name)]
    except OSError:
        return None
    found.sort(key=lambda p: (p.name not in names, -p.stat().st_mtime))
    return found[0] if found else None


def _upload_host(url: str) -> str:
    """hostname[:port] only: `netloc` would include user:pass@, which then lands in the log and manifest."""
    parts = urlparse(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    host = parts.hostname or "remote"
    return f"{host}:{port}" if port else host


def _already_finished(client: GitLabExport, project: str, log) -> bool:
    """On resume, an export that is already `finished` needs no fresh POST."""
    try:
        state = client.status(project).get("export_status", "")
    except MigrationError:
        return False
    if state == GitLabExport.TERMINAL_OK:
        log("  export already finished — step 2 skipped")
        return True
    return False


def check_run_name(run_name: str) -> None:
    """`safe_name` keeps dots, so '..' / '.' / '.jobs' would climb out of the
    run folder or land in the job store; a leading dot is never a run."""
    if run_name and safe_name(run_name).startswith("."):
        raise MigrationError("run_name must not start with a dot")


def run_dir(spec: JobSpec) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    check_run_name(spec.run_name)
    label = safe_name(spec.run_name) if spec.run_name else f"{spec.provider}-{stamp}"
    return ensure_outputs(COMPONENT, label)


def run_job(
    spec: JobSpec,
    log: Callable[[str], None] = print,
    cancelled: Optional[Callable[[], bool]] = None,
) -> dict:
    """Execute the full documented flow. Returns a manifest dict."""
    spec.validate()
    token = spec.resolved_token()
    dest_dir = run_dir(spec)
    check_disk(dest_dir, MIN_FREE_MB, what="this run")
    ratelimit.bind(cancelled, log)      # waits for rate limits show up in the run log
    started = time.time()

    log(f"provider   : {spec.provider}")
    log(f"token      : {mask(token)}{'' if spec.token else '  (from .env)'}")
    log(f"output dir : {dest_dir}")
    log(f"targets    : {len(spec.targets)}")
    log("")

    results: List[ArchiveResult] = []
    if spec.known_ids:
        log(f"resuming with {len(spec.known_ids)} known id(s) — step 2 skipped for those")
        log("")
    export_jobs: List[dict] = []
    if spec.provider == "github":
        results = _run_github(spec, token, dest_dir, log, cancelled)
    elif spec.provider == "gitlab":
        results = _run_gitlab(spec, token, dest_dir, log, cancelled)
    elif spec.provider == "bitbucket-dc":
        export_jobs = _run_bitbucket_dc(spec, token, dest_dir, log, cancelled)
    elif spec.provider == "bitbucket" and spec.mirror:
        results = _run_bitbucket_mirrors(spec, token, dest_dir, log, cancelled)

    manifest_path = dest_dir / "manifest.json"
    previous = _load_manifest(manifest_path) if spec.skip_done else None

    def assemble(extras_so_far: List[dict], complete: bool) -> dict:
        m = _manifest(spec, started, dest_dir, results, export_jobs, extras_so_far)
        if previous:
            _merge_resumed(m, previous)
        m["complete"] = complete
        _finish_counts(m)
        return m

    def checkpoint(extras_so_far: List[dict]) -> None:
        # after every target, so a crash or kill keeps what was collected and a resume can skip it
        _write_manifest(manifest_path, assemble(extras_so_far, complete=False))

    archives = {r.target: r.path for r in results if r.path and not r.error}
    try:
        extras = (_run_extras(spec, token, dest_dir, log, cancelled, checkpoint=checkpoint,
                              previous=(previous or {}).get("extras", []), archives=archives)
                  if spec.extras else [])
    finally:
        shutil.rmtree(dest_dir / ".mirror-work", ignore_errors=True)   # mirrors unpacked for the per-commit extras

    if spec.verify_checksum:
        for r in results:
            if r.path and r.path.exists():
                log(f"sha256 {r.target} …")
                r.digest = sha256(r.path)

    for r in results:
        info = r.meta.pop("for_check", None)
        if spec.for_check and info and r.path and r.path.exists():
            _write_for_check(info, r, log)

    manifest = assemble(extras, complete=True)
    _write_manifest(manifest_path, manifest)

    log("")
    log(f"done: {manifest['ok']} archive(s) downloaded, {manifest['archives_failed']} failed")
    for r in results:
        if r.path:
            log(f"  ✓ {r.path.name}  {human(r.bytes)}")
        elif r.meta.get("uploaded_to") and not r.error:
            log(f"  ✓ {r.target}: uploaded by GitLab to {r.meta['uploaded_to']}")
        else:
            log(f"  ✗ {r.target}: {r.error}")
    if extras:
        good = sum(1 for e in extras if not e["error"] and not e.get("item_errors"))
        log(f"extras: {good}/{len(extras)} collector run(s) complete")
        for e in extras:
            if e["error"]:
                log(f"  ✗ {e['target']} · {e['extra']}: {e['error']}")
            elif e.get("item_errors"):
                log(f"  ◐ {e['target']} · {e['extra']}: {e['item_errors']} item(s) could not be fetched")
    if manifest["issues"]:
        log(f"issues: {len(manifest['issues'])}")
        for item in manifest["issues"]:
            log(f"  ! {item['target'] or item['kind']} · {item['step']}: {item['message']}")
    log(f"manifest: {dest_dir / 'manifest.json'}")
    return manifest


def _manifest(spec: JobSpec, started: float, dest_dir: Path, results: List[ArchiveResult], export_jobs: List[dict],
              extras: List[dict]) -> dict:
    return {
        "component": COMPONENT,
        "provider": spec.provider,
        "scope": spec.scope if spec.provider == "github" else "project",
        "org": spec.org,
        "api_base": spec.api_base,
        "started_utc": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "elapsed_seconds": round(time.time() - started, 1),
        "output_dir": str(dest_dir),
        "archives": [r.as_dict() for r in results],
        "export_jobs": export_jobs,
        "extras": list(extras),
        "ok": sum(1 for r in results if r.as_dict()["ok"]),
        "archives_failed": sum(1 for r in results if r.error),
        "jobs_failed": sum(1 for j in export_jobs if j.get("error")),
    }


def _finish_counts(m: dict) -> None:
    extras = m.get("extras", [])
    m["extras_failed"] = sum(1 for e in extras if e.get("error"))
    # a collector that ran but lost some of its items (one commit's statuses, one PR diff) makes the run partial too
    m["extras_incomplete"] = sum(1 for e in extras if not e.get("error") and e.get("item_errors"))
    m["archives_incomplete"] = sum(1 for a in m.get("archives", []) if not a.get("error") and a.get("incomplete"))
    m["failed"] = (m["archives_failed"] + m["archives_incomplete"] + m["extras_failed"] + m["extras_incomplete"]
                   + m["jobs_failed"])
    m["issues"] = issues_from_manifest(m)


def _write_manifest(path: Path, manifest: dict) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(path)


def _load_manifest(path: Path) -> Optional[dict]:
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return old if isinstance(old, dict) else None


def _merge_resumed(manifest: dict, old: dict) -> None:
    """A resume only runs what was outstanding; writing its manifest as-is would
    drop the earlier archives, their sha256 and the original start time."""
    fresh = {a["target"]: a for a in manifest["archives"]}
    merged = []
    for a in old.get("archives", []):
        new = fresh.pop(a.get("target"), None)
        # a retry that failed again must not erase an earlier good download
        if new and new.get("ok") and a.get("ok") and not new.get("sha256"):
            new["sha256"] = a.get("sha256", "")
        merged.append(a if (new is None or (a.get("ok") and not new.get("ok"))) else new)
    manifest["archives"] = merged + list(fresh.values())
    manifest["ok"] = sum(1 for a in manifest["archives"] if a.get("ok"))
    manifest["archives_failed"] = sum(1 for a in manifest["archives"] if a.get("error"))
    if old.get("started_utc"):
        manifest["started_utc"] = old["started_utc"]
    manifest["elapsed_seconds"] = round(manifest["elapsed_seconds"] + float(old.get("elapsed_seconds") or 0), 1)
    # extras: the new run's record for a (target, collector) replaces the old one; the rest are kept
    fresh_extras = {(e.get("target"), e.get("extra")) for e in manifest["extras"]}
    manifest["extras"] = [e for e in old.get("extras", [])
                          if (e.get("target"), e.get("extra")) not in fresh_extras] + manifest["extras"]
    if not manifest["export_jobs"]:
        manifest["export_jobs"] = old.get("export_jobs", [])
        manifest["jobs_failed"] = sum(1 for j in manifest["export_jobs"] if j.get("error"))


# ── issues: what went wrong, in plain terms ──────────────────────────────────

_HINTS = [
    # (all of these must appear in the lower-cased message, hint)
    (("http 401", "api.bitbucket.org"), "Bitbucket rejected the token. A personal API token (id.atlassian.com) must "
                                        "be pasted as your-atlassian-email:token; only repository, project or "
                                        "workspace access tokens are pasted on their own. Also check it has not "
                                        "expired."),
    (("http 403", "api.bitbucket.org", "privilege scopes"),
     "The Bitbucket token is missing a scope this step needs. See the README's Bitbucket Cloud table; scopes cannot "
     "be added to an existing token, so create a new one with all 8."),
    (("http 555", "api.bitbucket.org"), "Bitbucket gave up computing this diff or patch (too large or too slow). "
                                        "The other items were collected."),
    (("http 401",), "The host rejected the token. Check it is pasted in full, has not expired or been revoked, "
                    "and belongs to this host."),
    (("http 422", "/user/migrations"), "/user/migrations cannot export an org's repos. Use scope auto (the "
                                       "default) so org repos go through the org's own migration endpoint."),
    (("http 403", "/orgs/", "/migrations"), "Org exports need an org Owner (or Migrator) and the admin:org scope. "
                                            "If the org enforces SAML SSO, authorize the token for it first."),
    (("http 403", "/migrations"), "The token lacks the repo scope, or the account cannot export these repositories."),
    (("http 404", "/migrations"), "The repository was not found: check the spelling and that the token can see it."),
    (("dependabot",), "Needs the security_events scope and Dependabot alerts enabled on the repo."),
    (("code-scanning",), "Needs security_events, and code scanning set up (private repos need GitHub Advanced Security)."),
    (("secret-scanning",), "Needs security_events/repo scope, and secret scanning enabled (private repos need "
                           "GitHub Advanced Security)."),
    (("/hooks",), "Listing webhooks needs admin access to the repository."),
    (("protection",), "Reading branch protection needs admin access to the repository."),
    (("http 403", "projectsv2"), "Projects (v2) need the read:project scope."),
    (("http 403", "packages"), "Packages need the read:packages scope."),
    (("http 404",), "Not found, or the token's user has no access: often a missing permission or a feature that "
                    "is switched off for this repository."),
    (("http 403",), "Forbidden: the token is missing a scope, or the account lacks the role this call needs."),
    (("rate limit", "429"), "The host throttled the token. The run waits and retries by itself; run again later "
                            "if this persists."),
    (("limited to",), "A listing was cut off at the item limit. Raise Max items (0 = no limit) under Download options."),
    (("timed out",), "The export took longer than the timeout. Raise the timeout; large repos can take over an hour."),
    (("network error",), "Could not reach the host. Check connectivity and the host / API base URL."),
    (("only", "mb free"), "Not enough free disk space for this run."),
    (("cancelled",), "You cancelled the run."),
    (("not installed",), "A required program is missing on this machine."),
]


def explain(message: str) -> str:
    lowered = message.lower()
    for needles, hint in _HINTS:
        if all(n in lowered for n in needles):
            return hint
    return ""


def issues_from_manifest(manifest: dict) -> List[dict]:
    """Everything that went wrong in a run as a flat list. Also works on manifests
    written before `issues` existed, so old runs get the same summary."""
    out: List[dict] = []

    def add(kind: str, target: str, step: str, message: str) -> None:
        out.append({"kind": kind, "target": target, "step": step, "message": message,
                    "hint": explain(f"{step} {message}")})

    for a in manifest.get("archives", []):
        if a.get("error"):
            add("archive", a.get("target", ""), "export + download", a["error"])
        for part in a.get("incomplete") or []:
            add("incomplete", a.get("target", ""), f"archive: {part.get('part', '')}", part.get("error", ""))
    for j in manifest.get("export_jobs", []):
        if j.get("error"):
            add("export-job", ", ".join(j.get("targets", [])), f"export job ({j.get('action', '')})", j["error"])
    for e in manifest.get("extras", []):
        if e.get("error"):
            add("extra", e.get("target", ""), e.get("extra", ""), e["error"])
        elif e.get("item_errors"):
            examples = e.get("item_error_examples") or []
            first = f" First: {examples[0]['what']}: {examples[0]['error']}" if examples else ""
            add("incomplete", e.get("target", ""), e.get("extra", ""),
                f"{e['item_errors']} item(s) could not be fetched; the rest were collected.{first}")
        for note in e.get("truncated", []):
            add("limit", e.get("target", ""), e.get("extra", ""), note)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner_repo(spec: JobSpec, name: str):
    if spec.scope == "org":
        return spec.org, name.split("/")[-1]
    owner, _, repo = name.partition("/")
    return (owner, repo) if repo else (owner, owner)


def _write_for_check(info: dict, result: ArchiveResult, log) -> None:
    """Write <archive>.for-check.csv. Whatever goes wrong here must never fail the download."""
    try:
        files, ocount, obytes = None, 0, 0
        try:
            files, ocount, obytes = for_check.list_archive(result.path)
        except Exception as exc:
            info["errors"].append(f"file listing: {type(exc).__name__}: {exc}")
        info["meta"]["content_length"] = result.meta.get("content_length", "")
        rows = for_check.build_rows(info, archive=result.path, digest=result.digest, files=files,
                                    ocount=ocount, obytes=obytes)
        path = for_check.write_csv(for_check.for_check_path(result.path), rows)
        log(f"  for-check: {path.name} ({len(rows)} rows)")
    except Exception as exc:
        log(f"  warning: could not write for-check.csv ({type(exc).__name__}: {exc})")


def _github_client(spec: JobSpec, token: str, log) -> GitHubMigration:
    return GitHubMigration(
        token,
        scope=spec.scope,
        org=spec.org,
        api_base=spec.api_base or "https://api.github.com",
        api_version=spec.api_version,
        lock_repositories=spec.lock_repositories,
        exclude=spec.exclude,
        org_metadata_only=spec.org_metadata_only,
        log=log,
    )


def _github_groups(spec, token, log) -> list:
    """[(scope, org, targets)]. `auto` sends the token owner's repos through the
    user migration API and each other owner's through that org's."""
    if spec.scope != "auto":
        return [(spec.scope, spec.org, spec.targets)]
    login = _github_login(spec, token, log)
    personal: List[str] = []
    by_org: dict = {}
    for target in spec.targets:
        owner = _parse_owner_repo(target).split("/")[0]
        if owner.lower() == login.lower():
            personal.append(target)
        else:
            by_org.setdefault(owner, []).append(target)
    groups = [("user", "", personal)] if personal else []
    groups += [("org", owner, targets) for owner, targets in by_org.items()]
    log("owners     : " + ", ".join(
        ([f"{login} (personal)"] if personal else []) + [f"{o} (org)" for o in by_org]))
    return groups


def _github_login(spec, token, log) -> str:
    try:
        return _github_client(replace(spec, scope="user"), token, log).whoami()
    except MigrationError as exc:
        raise MigrationError(
            "cannot tell personal repos from org repos: token check failed "
            f"({brief_error(exc)})") from None


def _run_github(spec, token, dest_dir, log, cancelled) -> List[ArchiveResult]:
    results: List[ArchiveResult] = []
    for scope, org, targets in _github_groups(spec, token, log):
        group = replace(spec, scope=scope, org=org, targets=targets)
        results += _run_github_scope(group, token, dest_dir, log, cancelled)
    return results


def _run_github_scope(spec, token, dest_dir, log, cancelled) -> List[ArchiveResult]:
    client = _github_client(spec, token, log)
    try:
        log(f"authenticated as {client.whoami()}")
    except MigrationError as exc:
        log(f"warning: token check failed ({brief_error(exc)})")

    batches = [spec.targets] if spec.single_archive else [[t] for t in spec.targets]
    results: List[ArchiveResult] = []

    for index, batch in enumerate(batches, 1):
        label = ", ".join(batch)
        result = ArchiveResult(target=label)
        log("")
        log(f"[{index}/{len(batches)}] {label}")
        try:
            if cancelled and cancelled():
                raise MigrationError("cancelled")
            fc = None
            if spec.for_check:
                pairs = [_owner_repo(spec, n) for n in batch]
                fc = {"archive_label": f"{pairs[0][0]}/{pairs[0][1]}" if len(pairs) == 1 else "*",
                      "requested": [f"{o}/{r}" for o, r in pairs], "exported": [], "errors": [],
                      "repos": [{"label": f"{o}/{r}", "owner": o, "repo": r} for o, r in pairs],
                      "meta": {"provider": "github", "host": client.api_base, "scope": spec.scope, "org": spec.org,
                               "api_version": client.api_version, "started_at": _now(),
                               "opt_lock_repositories": str(bool(client.lock_repositories)).lower(),
                               "opt_excludes": "none"}}
                for rp in fc["repos"]:
                    log(f"  for-check: snapshot before {rp['label']}")
                    rp["before"] = for_check.snapshot("github", rp["label"], headers=client._headers,
                                                      api_base=client.api_base, token=token,
                                                      owner=rp["owner"], repo=rp["repo"])
            migration_id = spec.known_ids.get(label, "")
            data = None
            if migration_id:
                log(f"  reusing migration id {migration_id}")
                try:
                    data = client.wait(migration_id, interval=spec.poll_interval,
                                       timeout=spec.timeout, cancelled=cancelled)
                except MigrationError as exc:
                    # a failed or deleted migration never recovers, and re-polling it
                    # would fail forever; any other error (timeout, cancel) is retryable
                    if "ended in state" not in str(exc) and "HTTP 404" not in str(exc):
                        raise
                    log(f"  known migration is unusable ({brief_error(exc)}) — starting a new one")
                    migration_id = ""
            if not migration_id:
                migration_id = client.start(batch)
            result.migration_id = migration_id
            if data is None:
                data = client.wait(
                    migration_id,
                    interval=spec.poll_interval,
                    timeout=spec.timeout,
                    cancelled=cancelled,
                )
            result.state = data.get("state", "")
            if fc:
                for rp in fc["repos"]:
                    rp["after"] = for_check.snapshot("github", rp["label"], headers=client._headers,
                                                     api_base=client.api_base, token=token,
                                                     owner=rp["owner"], repo=rp["repo"])
                try:
                    fc["exported"] = [str(x) for x in client.repositories(migration_id) if x]
                except Exception as exc:
                    fc["errors"].append(f"exported repos: {type(exc).__name__}: {exc}")
                fc["meta"].update(migration_id=migration_id, state=result.state, finished_at=_now())
                result.meta["for_check"] = fc
            stem = safe_name(batch[0] if len(batch) == 1 else f"{spec.org or 'user'}-batch")
            dest = dest_dir / f"{stem}-{migration_id}.tar.gz"
            if spec.skip_done and dest.is_file() and dest.stat().st_size:
                log(f"  already downloaded ({human(dest.stat().st_size)}) — skipping")
                result.skipped = True
            else:
                client.download(migration_id, dest)
                result.meta["content_length"] = client.download_info.get("content_length", "")
            result.path = dest
            result.bytes = dest.stat().st_size
            log(f"  saved {dest.name} ({human(result.bytes)})")
            _github_cleanup(spec, client, migration_id, batch, log, dest,
                            result.meta.get("content_length"))
        except MigrationError as exc:
            result.error = brief_error(exc)
            log(f"  ERROR {result.error}")
        results.append(result)
    return results


def _download_is_complete(dest: Path, expected) -> bool:
    """A file of exactly the size the server announced, that opens as a tarball."""
    try:
        size = dest.stat().st_size
    except OSError:
        return False
    try:
        announced = int(expected)
    except (TypeError, ValueError):
        return False
    return size > 0 and size == announced and tarfile.is_tarfile(dest)


def _github_cleanup(spec, client, migration_id, batch, log, dest: Path, expected_bytes) -> None:
    """Opt-in, and only after the archive is verified: unlocking is harmless, but
    deleting the archive from GitHub is not undoable."""
    if spec.unlock_repos:
        for repo in batch:
            try:
                client.unlock_repo(migration_id, repo)
            except MigrationError as exc:
                log(f"  warning: unlock {repo} failed ({brief_error(exc)})")
    if spec.delete_archive:
        if not _download_is_complete(dest, expected_bytes):
            log("  warning: not deleting the archive from GitHub: could not confirm the download is "
                "complete (size does not match what GitHub announced, or it is not a readable tarball)")
            return
        try:
            client.delete_archive(migration_id)
        except MigrationError as exc:
            log(f"  warning: delete archive failed ({brief_error(exc)})")


def _run_gitlab(spec, token, dest_dir, log, cancelled) -> List[ArchiveResult]:
    client = GitLabExport(token, api_base=spec.api_base or "https://gitlab.com", log=log)
    try:
        log(f"authenticated as {client.whoami()}")
    except MigrationError as exc:
        log(f"warning: token check failed ({brief_error(exc)})")

    def one(index, raw, plog) -> ArchiveResult:
        client = GitLabExport(token, api_base=spec.api_base or "https://gitlab.com", log=plog)
        project = client.normalise(raw)
        result = ArchiveResult(target=project)
        plog("")
        plog(f"[{index}/{len(spec.targets)}] {project}")
        # id-suffixed, like GitHub's naming — two distinct projects can
        # normalise to the same safe_name() stem (e.g. "team-a/proj" and
        # "team/a-proj" both -> "team-a-proj"), which would otherwise let one
        # silently overwrite the other's archive. Before the id is known
        # (first attempt), fall back to the unsuffixed name.
        stem = safe_name(project)
        known_id = spec.known_ids.get(project, "")
        precheck_dest = existing_archive(dest_dir, stem, [known_id]) if spec.skip_done else None
        try:
            if cancelled and cancelled():
                raise MigrationError("cancelled")
            if precheck_dest:
                # resume: the archive is already on disk, don't re-export it
                plog(f"  already downloaded ({human(precheck_dest.stat().st_size)}) — skipping")
                result.skipped = True
                result.path = precheck_dest
                result.bytes = precheck_dest.stat().st_size
                return result
            fc = None
            if spec.for_check:
                plog(f"  for-check: snapshot before {project}")
                fc = {"archive_label": project, "requested": [project], "exported": [project], "errors": [],
                      "repos": [{"label": project}],
                      "meta": {"provider": "gitlab", "host": client.api_base, "scope": "project", "org": "",
                               "api_version": "v4", "started_at": _now(), "opt_excludes": "none"}}
                fc["repos"][0]["before"] = for_check.snapshot("gitlab", project, headers=client._headers,
                                                              api_base=client.api_base, token=token)
            if known_id and _already_finished(client, project, log):
                pass
            else:
                client.start(project, upload_url=spec.upload_url,
                             upload_method=spec.upload_method, description=spec.description)
            data = client.wait(
                project,
                interval=spec.poll_interval,
                timeout=spec.timeout,
                cancelled=cancelled,
            )
            result.state = data.get("export_status", "")
            project_id = data.get("id")
            result.migration_id = str(project_id) if project_id else None
            if fc:
                fc["repos"][0]["after"] = for_check.snapshot("gitlab", project, headers=client._headers,
                                                             api_base=client.api_base, token=token)
                fc["meta"].update(migration_id=result.migration_id or "", state=result.state, finished_at=_now())
                result.meta["for_check"] = fc
            if spec.upload_url:
                # host only: a presigned URL's query string is a credential
                result.meta["uploaded_to"] = _upload_host(spec.upload_url)
                plog(f"  export finished; GitLab uploads it to {result.meta['uploaded_to']}")
                return result
            dest = dest_dir / (f"{stem}-{project_id}.tar.gz" if project_id else f"{stem}.tar.gz")
            client.download(project, dest, project_id=project_id)
            result.meta["content_length"] = client.download_info.get("content_length", "")
            result.path = dest
            result.bytes = dest.stat().st_size
            plog(f"  saved {dest.name} ({human(result.bytes)})")
        except MigrationError as exc:
            result.error = brief_error(exc)
            plog(f"  ERROR {result.error}")
        return result

    workers = max(1, min(spec.concurrency, len(spec.targets)))
    if workers == 1:
        return [one(i, raw, log) for i, raw in enumerate(spec.targets, 1)]

    lock = threading.Lock()
    log(f"exporting {workers} projects at a time")

    def work(arg) -> ArchiveResult:
        i, raw = arg

        def tagged(message: str) -> None:
            with lock:
                log(f"[{i}] {message}" if message else message)

        ratelimit.bind(cancelled, tagged)   # rate-limit state is shared, but its log/cancel binding is per thread
        return one(i, raw, tagged)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(work, enumerate(spec.targets, 1)))


def _run_bitbucket_mirrors(spec, token, dest_dir, log, cancelled) -> List[ArchiveResult]:
    """Bitbucket Cloud: each repo's full git history by `git clone --mirror` with the run's token, packed as
    <workspace>-<repo>.tar.gz (holding <repo>.git/) and recorded as the target's archive, so the bundle ships it
    together with that repo's extras."""
    api_base = spec.api_base or BITBUCKET_CLOUD_BASE
    results: List[ArchiveResult] = []
    for index, raw in enumerate(spec.targets, 1):
        target = _strip_url(raw).strip("/")
        result = ArchiveResult(target=target, meta={"kind": "git-mirror"})
        log("")
        log(f"[{index}/{len(spec.targets)}] {target}: git mirror")
        stem = safe_name(target)
        try:
            if cancelled and cancelled():
                raise MigrationError("cancelled")
            done = existing_archive(dest_dir, stem) if spec.skip_done else None
            if done:
                log(f"  already cloned ({human(done.stat().st_size)}) — skipping")
                result.skipped, result.path, result.bytes = True, done, done.stat().st_size
                results.append(result)
                continue
            check_disk(dest_dir, MIN_FREE_MB, what=f"the mirror of {target}")
            headers = cloud_headers(token)
            fc = None
            if spec.for_check:
                log(f"  for-check: snapshot before {target}")
                fc = {"archive_label": target, "requested": [target], "exported": [target], "errors": [],
                      "repos": [{"label": target}],
                      "meta": {"provider": "bitbucket", "host": api_base, "scope": "repository", "org": "",
                               "api_version": "2.0", "started_at": _now(), "opt_excludes": "none"}}
                fc["repos"][0]["before"] = for_check.snapshot("bitbucket", target, headers=headers,
                                                              api_base=api_base, token=token)
            core: dict = {}

            def add_metadata(work: Path, bare: Path) -> None:
                user, password = bitbucket.git_credentials(token)
                ctx = Ctx(provider="bitbucket", target=target, token=token, api_base=api_base.rstrip("/"),
                          headers=headers, out=work, log=log, cancelled=cancelled)
                try:
                    core.update(bitbucket_archive.build_core(ctx, bare, user=user, password=password))
                except MigrationError as exc:
                    if "cancelled" in str(exc):
                        raise
                    core["errors"] = [{"part": "metadata", "error": brief_error(exc)}]
                except Exception as exc:  # a surprise in the metadata must not cost the repo its git history
                    core["errors"] = [{"part": "metadata", "error": redact.scrub(f"{type(exc).__name__}: {exc}")[:600]}]

            dest = bitbucket.mirror_archive(target, token, api_base, dest_dir, log, before_pack=add_metadata)
            result.path, result.bytes, result.state = dest, dest.stat().st_size, "archived"
            result.meta["incomplete"] = core.get("errors", [])
            if fc:
                fc["repos"][0]["after"] = for_check.snapshot("bitbucket", target, headers=headers,
                                                             api_base=api_base, token=token)
                fc["meta"].update(state=result.state, finished_at=_now())
                result.meta["for_check"] = fc
            log(f"  saved {dest.name} ({human(result.bytes)})")
            for part in result.meta["incomplete"]:
                log(f"  ! archive part {part['part']} missing: {part['error']}")
        except MigrationError as exc:
            result.error = brief_error(exc)
            log(f"  ERROR {result.error}")
        results.append(result)
    return results


def _run_bitbucket_dc(spec, token, dest_dir, log, cancelled) -> List[dict]:
    """Preview / start / cancel the Data Center export job. The tar stays on the
    server's shared home, so there is nothing to download."""
    client = BitbucketDCExport(token, spec.api_base, log=log)
    if spec.dc_action == "none":
        return []
    job: dict = {"action": spec.dc_action, "targets": spec.targets}
    try:
        if spec.dc_action == "cancel":
            job["job_id"] = spec.dc_job_id
            job["response"] = client.cancel(spec.dc_job_id)
        elif spec.dc_action == "preview":
            preview = client.preview(spec.targets)
            (dest_dir / "export-preview.json").write_text(json.dumps(preview, indent=2))
            job["preview"] = "export-preview.json"
        else:
            job_id = client.start(spec.targets)
            job["job_id"] = job_id
            data = client.wait(job_id, interval=spec.poll_interval, timeout=spec.timeout,
                               cancelled=cancelled)
            job["state"] = data.get("state", "")
            messages = client.messages(job_id)
            (dest_dir / "export-messages.json").write_text(json.dumps(messages, indent=2))
            job["messages"] = len(messages)
            log(f"  tar is on the server: $BITBUCKET_SHARED_HOME/data/migration/export/"
                f"Bitbucket_export_{job_id}.tar")
    except MigrationError as exc:
        job["error"] = brief_error(exc)
        log(f"  ERROR {job['error']}")
    return [job]


def _extras_context(spec, token, log):
    """(headers, api_base, target normaliser) for the provider's collectors."""
    if spec.provider == "github":
        client = _github_client(replace(spec, scope="user") if spec.scope == "auto" else spec, token, log)
        return client._headers, client.api_base, client.normalise
    if spec.provider == "gitlab":
        client = GitLabExport(token, api_base=spec.api_base or "https://gitlab.com", log=log)
        return client._headers, client.api_base, client.normalise
    if spec.provider == "bitbucket":
        def normalise(target: str) -> str:
            target = target.strip().strip("/")
            if target.startswith(("http://", "https://")):
                target = urlparse(target).path.strip("/")
            return target[:-4] if target.endswith(".git") else target
        return cloud_headers(token), (spec.api_base or BITBUCKET_CLOUD_BASE).rstrip("/"), normalise
    return (BitbucketDCExport(token, spec.api_base)._headers, spec.api_base.rstrip("/"),
            lambda t: t.strip().strip("/"))


def _extras_org(spec, target: str, login: str) -> str:
    """The org owning a GitHub target (so org-level extras use org endpoints), else ''."""
    if spec.provider != "github":
        return ""
    if spec.scope == "org":
        return spec.org
    if spec.scope == "auto" and login:
        owner = target.split("/")[0]
        return "" if owner.lower() == login.lower() else owner
    return ""


def _already_collected(previous: dict, target: str, name: str, out: Path) -> Optional[dict]:
    """On a resume, a (target, collector) that finished cleanly last time and whose files are all still on disk."""
    rec = previous.get((target, name))
    if not rec or rec.get("error") or rec.get("item_errors") or rec.get("truncated"):
        return None
    if not all((out / f).is_file() for f in rec.get("files", [])):
        return None
    return rec


def _run_extras(spec, token, dest_dir, log, cancelled, checkpoint=None, previous=(), archives=None) -> List[dict]:
    skipped = set(resolve_names(spec.provider, spec.skip_extras))
    names = [n for n in resolve_names(spec.provider, spec.extras) if n not in skipped]
    if skipped:
        log(f"extras unticked: {', '.join(sorted(skipped))}")
    done_before = {(e.get("target"), e.get("extra")): e for e in previous} if spec.skip_done else {}
    if spec.provider == "bitbucket":
        _warn_per_commit_cost(names, log, mirrored=bool(archives))
    headers, api_base, normalise = _extras_context(spec, token, log)
    shared: dict = {"bb_archives": dict(archives or {})}
    records: List[dict] = []
    login = ""
    if spec.provider == "github" and spec.scope == "auto":
        try:
            login = _github_login(spec, token, log)
        except MigrationError as exc:
            log(f"warning: {exc}; owner-level extras will be queried as a user")
    log("")
    log(f"extras: {', '.join(names)}")
    for target in spec.targets:
        target = normalise(target)
        log(f"[extras] {target}")
        out = dest_dir / "extras" / safe_name(target)
        out.mkdir(parents=True, exist_ok=True)
        ctx = Ctx(provider=spec.provider, target=target, token=token, api_base=api_base,
                  headers=headers, out=out, log=log,
                  org=_extras_org(spec, target, login),
                  scope=spec.scope, max_items=spec.max_items, shared=shared, cancelled=cancelled)
        reached = False        # some call for this repo answered: a 404 then means "not there", not "no such repo"
        pending404: List[dict] = []
        for name in names:
            earlier = _already_collected(done_before, target, name, out.resolve())
            if earlier:
                log(f"  {name}: already collected, skipped")
                records.append(earlier)
                continue
            record = {"target": target, "extra": name, "files": [], "error": ""}
            ctx.truncated.clear()
            ctx.item_errors.clear()
            ctx.unavailable.clear()
            log(f"  {name} …")
            try:
                if cancelled and cancelled():
                    raise MigrationError("cancelled")
                record["files"] = REGISTRY[spec.provider][name](ctx)
            except MigrationError as exc:
                record["error"] = brief_error(exc)
                if supplementary.not_found(record["error"]) and name != "repo-metadata":
                    pending404.append(record)    # decided below: unavailable if the repo itself answered
                else:
                    log(f"    ERROR {record['error']}")
            except Exception as exc:  # one collector's surprise must not stop the other collectors and targets
                record["error"] = redact.scrub(f"{type(exc).__name__}: {exc}")[:600]
                log(f"    ERROR {record['error']}")
            if not record["error"]:
                reached = True
            if ctx.unavailable:
                record["unavailable_items"] = len(ctx.unavailable)
            if ctx.truncated:
                record["truncated"] = list(ctx.truncated)
            if ctx.item_errors:
                record["item_errors"] = len(ctx.item_errors)
                record["item_error_examples"] = [dict(e) for e in ctx.item_errors[:5]]
                log(f"    {len(ctx.item_errors)} item(s) could not be fetched")
            records.append(record)
        for record in pending404:
            if reached:      # the repo is there; it just has none of this (no wiki, no Pipelines, no branching model)
                record["unavailable"], record["error"] = True, ""
                log(f"  {record['extra']}: unavailable for this repo, not an error")
            else:            # every call 404'd: the repo is missing or this token cannot see it
                log(f"  {record['extra']}: ERROR {record['error']}")
        if checkpoint:
            checkpoint(records)
    return records


# collectors that make one call per commit, and roughly how many calls Bitbucket allows an hour
PER_COMMIT = ("commit-comments", "commit-diffstat")
FROM_MIRROR = ("commit-diffstat",)     # computed from the repo's mirror when the run has one
BITBUCKET_CALLS_PER_HOUR = 1000


def _warn_per_commit_cost(names: List[str], log, mirrored: bool = False) -> None:
    chosen = [n for n in names if n in PER_COMMIT and not (mirrored and n in FROM_MIRROR)]
    if chosen:
        log(f"note: {', '.join(chosen)} make one API call per commit, per collector. Bitbucket allows about "
            f"{BITBUCKET_CALLS_PER_HOUR} calls an hour, so a repo with 5,000 commits needs about "
            f"{5000 * len(chosen) // BITBUCKET_CALLS_PER_HOUR} hours for these alone; the run waits out the limit "
            f"rather than failing. Untick them for a faster run.")


def list_runs(limit: int = 40) -> List[dict]:
    """Past runs, newest first, read back from their manifests."""
    root = outputs_for(COMPONENT)
    if not root.is_dir():
        return []
    runs = []
    for path in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        if path.name.startswith("."):       # .jobs is the job store, not a run
            continue
        manifest = path / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text())
        except json.JSONDecodeError:
            continue
        data["name"] = path.name
        runs.append(data)
    return runs
