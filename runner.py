"""Job orchestration for the migration downloader.

One `run_job()` call executes the whole documented flow for a batch of repos
and writes every archive under `outputs/migration-downloader/<run-id>/`,
alongside a `manifest.json` describing the run.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

import bootstrap  # noqa: F401  (sets sys.path)

from datalabs_paths import (  # noqa: E402
    ensure_outputs,
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
    human,
    mask,
    safe_name,
    sha256,
)

COMPONENT = "migration-downloader"
load_env()


@dataclass
class JobSpec:
    provider: str = "github"          # github | gitlab
    scope: str = "user"               # github only: user | org
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

    @classmethod
    def from_form(cls, data: dict) -> "JobSpec":
        def flag(key: str) -> bool:
            return str(data.get(key, "")).lower() in {"1", "true", "on", "yes"}

        raw = str(data.get("targets", ""))
        targets = [t.strip() for chunk in raw.splitlines() for t in chunk.split(",") if t.strip()]
        return cls(
            provider=(data.get("provider") or "github").strip().lower(),
            scope=(data.get("scope") or "user").strip().lower(),
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
        )

    def resolved_token(self) -> str:
        if self.token:
            return self.token
        return github_token() if self.provider == "github" else gitlab_token()

    def validate(self) -> None:
        if self.provider not in {"github", "gitlab"}:
            raise MigrationError("provider must be 'github' or 'gitlab'")
        if not self.targets:
            raise MigrationError("give at least one repository / project")
        if self.provider == "github" and self.scope == "org" and not self.org:
            raise MigrationError("an organization name is required for org scope")
        if not self.resolved_token():
            key = "GITHUB_TOKEN / GH_TOKEN" if self.provider == "github" else "GITLAB_TOKEN"
            raise MigrationError(f"no token supplied and none found in .env ({key})")


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
    if spec.provider == "github":
        results = _run_github(spec, token, dest_dir, log, cancelled)
    else:
        results = _run_gitlab(spec, token, dest_dir, log, cancelled)

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
        "ok": sum(1 for r in results if r.path and not r.error),
        "failed": sum(1 for r in results if r.error),
    }
    (dest_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    log("")
    log(f"done: {manifest['ok']} archive(s) downloaded, {manifest['failed']} failed")
    for r in results:
        if r.path:
            log(f"  ✓ {r.path.name}  {human(r.bytes)}")
        else:
            log(f"  ✗ {r.target}: {r.error}")
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
        log=log,
    )


def _run_github(spec, token, dest_dir, log, cancelled) -> List[ArchiveResult]:
    client = _github_client(spec, token, log)
    try:
        log(f"authenticated as {client.whoami()}")
    except MigrationError as exc:
        log(f"warning: token check failed ({exc.args[0].splitlines()[0]})")

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
        except MigrationError as exc:
            result.error = str(exc).splitlines()[0]
            log(f"  ERROR {result.error}")
        results.append(result)
    return results


def _run_gitlab(spec, token, dest_dir, log, cancelled) -> List[ArchiveResult]:
    client = GitLabExport(token, api_base=spec.api_base or "https://gitlab.com", log=log)
    try:
        log(f"authenticated as {client.whoami()}")
    except MigrationError as exc:
        log(f"warning: token check failed ({exc.args[0].splitlines()[0]})")

    results: List[ArchiveResult] = []
    for index, raw in enumerate(spec.targets, 1):
        project = client.normalise(raw)
        result = ArchiveResult(target=project)
        log("")
        log(f"[{index}/{len(spec.targets)}] {project}")
        # id-suffixed, like GitHub's naming — two distinct projects can
        # normalise to the same safe_name() stem (e.g. "team-a/proj" and
        # "team/a-proj" both -> "team-a-proj"), which would otherwise let one
        # silently overwrite the other's archive. Before the id is known
        # (first attempt), fall back to the unsuffixed name.
        stem = safe_name(project)
        known_id = spec.known_ids.get(project, "")
        precheck_dest = dest_dir / (f"{stem}-{known_id}.tar.gz" if known_id else f"{stem}.tar.gz")
        try:
            if cancelled and cancelled():
                raise MigrationError("cancelled")
            if spec.skip_done and precheck_dest.is_file() and precheck_dest.stat().st_size:
                # resume: the archive is already on disk, don't re-export it
                log(f"  already downloaded ({human(precheck_dest.stat().st_size)}) — skipping")
                result.skipped = True
                result.path = precheck_dest
                result.bytes = precheck_dest.stat().st_size
                results.append(result)
                continue
            if known_id and _already_finished(client, project, log):
                pass
            else:
                client.start(project)
            data = client.wait(
                project,
                interval=spec.poll_interval,
                timeout=spec.timeout,
                cancelled=cancelled,
            )
            result.state = data.get("export_status", "")
            project_id = data.get("id")
            result.migration_id = str(project_id) if project_id else None
            dest = dest_dir / (f"{stem}-{project_id}.tar.gz" if project_id else f"{stem}.tar.gz")
            client.download(project, dest, project_id=project_id)
            result.path = dest
            result.bytes = dest.stat().st_size
            log(f"  saved {dest.name} ({human(result.bytes)})")
        except MigrationError as exc:
            result.error = str(exc).splitlines()[0]
            log(f"  ERROR {result.error}")
        results.append(result)
    return results


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
