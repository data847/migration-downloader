"""Job orchestration for the migration downloader.

One `run_job()` call executes the whole documented flow for a batch of repos
and writes every archive under `outputs/migration-downloader/<run-id>/`,
alongside a `manifest.json` describing the run.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urlparse

import bootstrap  # noqa: F401  (sets sys.path)

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
from bitbucket import BITBUCKET_CLOUD_BASE, BitbucketDCExport, cloud_headers  # noqa: E402
from supplementary import REGISTRY, Ctx, resolve_names  # noqa: E402

COMPONENT = "migration-downloader"
PROVIDERS = {"github", "gitlab", "bitbucket", "bitbucket-dc"}
load_env()


def _owner_repo(target: str) -> str:
    """owner/repo from a URL, .git name or plain pair; auto scope needs the owner."""
    path = urlparse(target).path if target.startswith(("http://", "https://")) else target
    path = path.strip().strip("/")
    path = path[:-4] if path.endswith(".git") else path
    if path.count("/") != 1 or not all(path.split("/")):
        raise MigrationError(
            f"'{target}' should be owner/repo (the owner tells personal repos from an org's)")
    return path


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
    # supplementary collectors (see supplementary.py / bitbucket.py)
    extras: List[str] = field(default_factory=list)   # names, or all / everything
    max_items: int = 200              # cap per unbounded listing (runs, commits, PRs…)
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
            poll_interval=max(5, int(data.get("poll_interval") or 15)),
            timeout=max(60, int(data.get("timeout") or 3600)),
            verify_checksum=flag("verify_checksum"),
            run_name=(data.get("run_name") or "").strip(),
            extras=_split(data.get("extras", "")),
            max_items=max(1, int(data.get("max_items") or 200)),
            exclude=_split(data.get("exclude", "")),
            org_metadata_only=flag("org_metadata_only"),
            unlock_repos=flag("unlock_repos"),
            delete_archive=flag("delete_archive"),
            upload_url=(data.get("upload_url") or "").strip(),
            upload_method=(data.get("upload_method") or "").strip().upper(),
            description=(data.get("description") or "").strip(),
            dc_action=(data.get("dc_action") or "export").strip().lower(),
            dc_job_id=(data.get("dc_job_id") or "").strip(),
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
                    _owner_repo(target)     # every target must say who owns it
        if self.provider == "bitbucket" and not self.extras:
            raise MigrationError("Bitbucket Cloud has no export archive; pass extras (e.g. all, pullrequests)")
        if self.provider == "bitbucket-dc":
            if not self.api_base:
                raise MigrationError("Bitbucket Data Center needs --api-base (its base URL)")
            if self.dc_action not in {"export", "preview", "cancel", "none"}:
                raise MigrationError("dc_action must be export, preview, cancel or none")
            if cancel_only and not self.dc_job_id:
                raise MigrationError("dc_action cancel needs dc_job_id")
        if self.extras:
            resolve_names(self.provider, self.extras)   # fail fast on a typo
        if self.upload_url and self.provider != "gitlab":
            raise MigrationError("upload_url only applies to GitLab")
        if self.unlock_repos and not self.lock_repositories:
            raise MigrationError("unlock_repos only makes sense with lock_repositories")
        if not self.resolved_token():
            raise MigrationError(
                f"no token supplied and none found in .env ({self.token_env_hint()})")


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


def run_dir(spec: JobSpec) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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

    extras = _run_extras(spec, token, dest_dir, log, cancelled) if spec.extras else []

    if spec.verify_checksum:
        for r in results:
            if r.path and r.path.exists():
                log(f"sha256 {r.target} …")
                r.digest = sha256(r.path)

    manifest = {
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
        "extras": extras,
        "ok": sum(1 for r in results if r.as_dict()["ok"]),
        "archives_failed": sum(1 for r in results if r.error),
        "extras_failed": sum(1 for e in extras if e["error"]),
        "jobs_failed": sum(1 for j in export_jobs if j.get("error")),
    }
    manifest["failed"] = manifest["archives_failed"] + manifest["extras_failed"] + manifest["jobs_failed"]
    (dest_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

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
        good = sum(1 for e in extras if not e["error"])
        log(f"extras: {good}/{len(extras)} collector run(s) succeeded")
        for e in extras:
            if e["error"]:
                log(f"  ✗ {e['target']} · {e['extra']}: {e['error']}")
    log(f"manifest: {dest_dir / 'manifest.json'}")
    return manifest


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
        owner = _owner_repo(target).split("/")[0]
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
            migration_id = spec.known_ids.get(label, "")
            if migration_id:
                log(f"  reusing migration id {migration_id}")
            else:
                migration_id = client.start(batch)
            result.migration_id = migration_id
            data = client.wait(
                migration_id,
                interval=spec.poll_interval,
                timeout=spec.timeout,
                cancelled=cancelled,
            )
            result.state = data.get("state", "")
            stem = safe_name(batch[0] if len(batch) == 1 else f"{spec.org or 'user'}-batch")
            dest = dest_dir / f"{stem}-{migration_id}.tar.gz"
            if spec.skip_done and dest.is_file() and dest.stat().st_size:
                log(f"  already downloaded ({human(dest.stat().st_size)}) — skipping")
                result.skipped = True
            else:
                client.download(migration_id, dest)
            result.path = dest
            result.bytes = dest.stat().st_size
            log(f"  saved {dest.name} ({human(result.bytes)})")
            _github_cleanup(spec, client, migration_id, batch, log)
        except MigrationError as exc:
            result.error = brief_error(exc)
            log(f"  ERROR {result.error}")
        results.append(result)
    return results


def _github_cleanup(spec, client, migration_id, batch, log) -> None:
    """Opt-in, and only after the archive is safely on disk."""
    if spec.unlock_repos:
        for repo in batch:
            try:
                client.unlock_repo(migration_id, repo)
            except MigrationError as exc:
                log(f"  warning: unlock {repo} failed ({brief_error(exc)})")
    if spec.delete_archive:
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

    results: List[ArchiveResult] = []
    for index, raw in enumerate(spec.targets, 1):
        project = client.normalise(raw)
        result = ArchiveResult(target=project)
        log("")
        log(f"[{index}/{len(spec.targets)}] {project}")
        dest = dest_dir / f"{safe_name(project)}.tar.gz"
        try:
            if cancelled and cancelled():
                raise MigrationError("cancelled")
            if spec.skip_done and dest.is_file() and dest.stat().st_size:
                # resume: the archive is already on disk, don't re-export it
                log(f"  already downloaded ({human(dest.stat().st_size)}) — skipping")
                result.skipped = True
                result.path = dest
                result.bytes = dest.stat().st_size
                results.append(result)
                continue
            if spec.known_ids.get(project) and _already_finished(client, project, log):
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
            if spec.upload_url:
                # host only: a presigned URL's query string is a credential
                result.meta["uploaded_to"] = urlparse(spec.upload_url).netloc or "remote"
                log(f"  export finished; GitLab uploads it to {result.meta['uploaded_to']}")
                results.append(result)
                continue
            client.download(project, dest, project_id=project_id)
            result.path = dest
            result.bytes = dest.stat().st_size
            log(f"  saved {dest.name} ({human(result.bytes)})")
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


def _run_extras(spec, token, dest_dir, log, cancelled) -> List[dict]:
    names = resolve_names(spec.provider, spec.extras)
    headers, api_base, normalise = _extras_context(spec, token, log)
    shared: dict = {}
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
        for name in names:
            record = {"target": target, "extra": name, "files": [], "error": ""}
            log(f"  {name} …")
            try:
                if cancelled and cancelled():
                    raise MigrationError("cancelled")
                record["files"] = REGISTRY[spec.provider][name](ctx)
            except MigrationError as exc:
                record["error"] = brief_error(exc)
                log(f"    ERROR {record['error']}")
            records.append(record)
    return records


def list_runs(limit: int = 40) -> List[dict]:
    """Past runs, newest first, read back from their manifests."""
    root = outputs_for(COMPONENT)
    if not root.is_dir():
        return []
    runs = []
    for path in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
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
