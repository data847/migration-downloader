"""Crash-tolerant job registry.

Every job's state and full log live in a JSON file under
`outputs/migration-downloader/.jobs/`, flushed as the run progresses. So:

* reloading the browser replays the log instead of losing it;
* restarting the server (or `docker compose up` again) still shows the run,
  marked `interrupted`, with the migration ids needed to resume it;
* resuming skips step 2 for targets whose migration already exists and skips
  step 4 for archives already on disk.

Tokens are never persisted — only the masked form, for display.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import bootstrap  # noqa: F401  (sets sys.path)

from datalabs_paths import ensure_outputs, outputs_for
from migration_api import mask, safe_name
from runner import COMPONENT, JobSpec

JOBS_DIR = ensure_outputs(COMPONENT, ".jobs")
FLUSH_SECONDS = 1.0
KEEP_JOBS = 60

_lock = threading.RLock()
_jobs: Dict[str, dict] = {}
_dirty: Dict[str, float] = {}


def _path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _public_spec(spec: JobSpec) -> dict:
    """The spec minus the secret — safe to write to disk and to the browser."""
    return {
        "provider": spec.provider,
        "scope": spec.scope,
        "org": spec.org,
        "targets": spec.targets,
        "api_base": spec.api_base,
        "api_version": spec.api_version,
        "lock_repositories": spec.lock_repositories,
        "single_archive": spec.single_archive,
        "poll_interval": spec.poll_interval,
        "timeout": spec.timeout,
        "verify_checksum": spec.verify_checksum,
        "run_name": spec.run_name,
        "token_hint": mask(spec.resolved_token()),
        "token_from_env": not spec.token,
    }


def create(spec: JobSpec) -> str:
    job_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "state": "running",
            "log": [],
            "manifest": None,
            "error": "",
            "cancel": False,
            "spec": _public_spec(spec),
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _flush(job_id, force=True)
    return job_id


def get(job_id: str) -> Optional[dict]:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def append(job_id: str, line: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["log"].append(line)
        _flush(job_id)


def update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(fields)
        job["updated_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _flush(job_id, force=True)


def cancelled(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        return bool(job and job["cancel"])


def request_cancel(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job["state"] != "running":
            return False
        job["cancel"] = True
        _flush(job_id, force=True)
        return True


def listing(limit: int = 25) -> List[dict]:
    """Newest first, log bodies stripped — for the sidebar / history table."""
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["created_utc"], reverse=True)[:limit]
        return [
            {
                "id": j["id"],
                "state": j["state"],
                "created_utc": j["created_utc"],
                "updated_utc": j["updated_utc"],
                "provider": j["spec"]["provider"],
                "targets": j["spec"]["targets"],
                "run_name": (j.get("manifest") or {}).get("output_dir", "").split("/")[-1],
                "lines": len(j["log"]),
                "resumable": bool(resume_plan(j)),
            }
            for j in jobs
        ]


def active() -> Optional[dict]:
    """The job a freshly loaded page should re-attach to, if any."""
    with _lock:
        running = [j for j in _jobs.values() if j["state"] == "running"]
        pool = running or [j for j in _jobs.values() if j["state"] == "interrupted"]
        if not pool:
            return None
        return dict(sorted(pool, key=lambda j: j["created_utc"])[-1])


# ── resume ───────────────────────────────────────────────────────────────────


def resume_plan(job: dict) -> Optional[dict]:
    """What is left to do for an interrupted / partial job."""
    if job["state"] not in {"interrupted", "partial", "error", "cancelled"}:
        return None
    manifest = job.get("manifest") or {}
    archives = manifest.get("archives", [])
    done = {a["target"] for a in archives if a.get("ok")}
    done |= _already_on_disk(job, manifest)
    known = {a["target"]: a["migration_id"] for a in archives if a.get("migration_id")}
    # ids also survive in the log when the crash happened before the manifest
    for line in job["log"]:
        if "migration id = " in line:
            known.setdefault(job["spec"]["targets"][0] if job["spec"]["targets"] else "",
                             line.split("migration id = ")[1].split()[0])
    outstanding = [t for t in job["spec"]["targets"] if t not in done]
    if not outstanding:
        return None
    return {"targets": outstanding, "known_ids": {k: v for k, v in known.items() if v},
            "run_name": manifest.get("output_dir", "").split("/")[-1] or job["spec"]["run_name"]}


def _already_on_disk(job: dict, manifest: dict) -> set:
    """Targets whose archive is already in the run directory.

    A crash between the download and the manifest write leaves a good tarball
    with no record of it; without this check the run would look resumable
    forever and a resume would re-export work that is already done.
    """
    run = (manifest.get("output_dir", "").split("/")[-1]
           or job.get("spec", {}).get("run_name", ""))
    if not run:
        return set()
    run_dir = outputs_for(COMPONENT, run)
    if not run_dir.is_dir():
        return set()
    names = {p.name for p in run_dir.iterdir() if p.is_file() and p.stat().st_size}
    ids = {a["target"]: a.get("migration_id") for a in manifest.get("archives", [])}
    settled = set()
    for target in job.get("spec", {}).get("targets", []):
        stem = safe_name(target)
        migration_id = ids.get(target)
        candidates = {f"{stem}.tar.gz"}
        if migration_id:
            candidates.add(f"{stem}-{migration_id}.tar.gz")
        if candidates & names:
            settled.add(target)
    return settled


def spec_for_resume(job: dict, token: str = "") -> JobSpec:
    plan = resume_plan(job) or {"targets": job["spec"]["targets"], "known_ids": {}, "run_name": ""}
    s = job["spec"]
    return JobSpec(
        provider=s["provider"],
        scope=s["scope"],
        org=s["org"],
        targets=plan["targets"],
        token=token,
        api_base=s["api_base"],
        api_version=s["api_version"],
        lock_repositories=s["lock_repositories"],
        single_archive=s["single_archive"],
        poll_interval=s["poll_interval"],
        timeout=s["timeout"],
        verify_checksum=s["verify_checksum"],
        run_name=plan["run_name"] or s["run_name"],
        known_ids=plan["known_ids"],
        skip_done=True,
    )


# ── disk ─────────────────────────────────────────────────────────────────────


def _flush(job_id: str, force: bool = False) -> None:
    now = time.time()
    if not force and now - _dirty.get(job_id, 0) < FLUSH_SECONDS:
        return
    _dirty[job_id] = now
    job = _jobs.get(job_id)
    if job is None:
        return
    payload = {k: v for k, v in job.items() if k != "cancel"}
    tmp = _path(job_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(_path(job_id))


def flush_all() -> None:
    with _lock:
        for job_id in list(_jobs):
            _flush(job_id, force=True)


def load_from_disk() -> int:
    """Re-hydrate jobs at startup; anything still 'running' died with the process."""
    restored = 0
    for path in sorted(JOBS_DIR.glob("*.json"))[-KEEP_JOBS:]:
        try:
            job = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        job.setdefault("cancel", False)
        job.setdefault("log", [])
        job.setdefault("spec", {})
        if job.get("state") == "running":
            job["state"] = "interrupted"
            job["log"].append("")
            job["log"].append("— server stopped while this run was in flight —")
            job["log"].append("  the archive keeps building on GitHub/GitLab; press Resume to")
            job["log"].append("  poll it again and download (already-finished parts are skipped)")
        with _lock:
            _jobs[job["id"]] = job
            _flush(job["id"], force=True)
        restored += 1
    return restored
