"""Local web UI for the GitHub / GitLab migration download flow.

Run:  ./run.sh                    (http://127.0.0.1:8765)
      docker compose up           (same, in a container)

Jobs run in background threads and are persisted by `jobstore`, so a browser
reload — or a server/container restart — never loses a run in flight: the page
re-attaches to it, and an interrupted run can be resumed from its migration id.

Archives land in outputs/migration-downloader/<run>/ per invariant #2.
"""

from __future__ import annotations

import argparse
import atexit
import io
import os
import sys
import threading
import traceback
import zipfile
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

import bootstrap  # noqa: F401  (sets sys.path)

from datalabs_paths import ENV_FILE, github_token, gitlab_token, outputs_for  # noqa: E402
from migration_api import MigrationError, mask  # noqa: E402
from runner import COMPONENT, JobSpec, list_runs, run_job  # noqa: E402

import jobstore  # noqa: E402

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
# pick up template edits without a restart
app.config["TEMPLATES_AUTO_RELOAD"] = True
atexit.register(jobstore.flush_all)


def _start(spec: JobSpec) -> str:
    job_id = jobstore.create(spec)
    threading.Thread(target=_worker, args=(job_id, spec), daemon=True).start()
    return job_id


def _worker(job_id: str, spec: JobSpec) -> None:
    log = lambda line: jobstore.append(job_id, line)  # noqa: E731
    cancelled = lambda: jobstore.cancelled(job_id)    # noqa: E731
    try:
        manifest = run_job(spec, log=log, cancelled=cancelled)
        state = "cancelled" if cancelled() else ("done" if not manifest["failed"] else "partial")
        jobstore.update(job_id, manifest=manifest, state=state)
    except MigrationError as exc:
        log(f"ERROR {exc}")
        jobstore.update(job_id, state="error", error=str(exc))
    except Exception:  # unexpected — traceback into the log, not the console
        log(traceback.format_exc())
        jobstore.update(job_id, state="error", error="unexpected failure; see log")


def _run_root(run: str) -> Path:
    """Resolve `<outputs>/migration-downloader/<run>`, refusing path escapes."""
    root = outputs_for(COMPONENT).resolve()
    path = (root / run).resolve()
    if root != path.parent or not path.is_dir():
        abort(404)
    return path


# ── routes ───────────────────────────────────────────────────────────────────


@app.get("/")
def index():
    return render_template(
        "index.html",
        env_file=str(ENV_FILE),
        outputs_dir=str(outputs_for(COMPONENT)),
        github_env=mask(github_token()) if github_token() else "",
        gitlab_env=mask(gitlab_token()) if gitlab_token() else "",
    )


@app.post("/api/jobs")
def create_job():
    spec = JobSpec.from_form(request.get_json(force=True, silent=True) or {})
    try:
        spec.validate()
    except MigrationError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(id=_start(spec))


@app.get("/api/jobs")
def jobs():
    return jsonify(jobs=jobstore.listing(), active=(jobstore.active() or {}).get("id", ""))


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    since = max(0, int(request.args.get("since", 0)))
    job = jobstore.get(job_id)
    if job is None:
        abort(404)
    return jsonify(
        id=job["id"],
        state=job["state"],
        error=job["error"],
        manifest=job["manifest"],
        spec=job["spec"],
        created_utc=job["created_utc"],
        lines=job["log"][since:],
        total_lines=len(job["log"]),
        resumable=bool(jobstore.resume_plan(job)),
    )


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    if jobstore.get(job_id) is None:
        abort(404)
    return jsonify(ok=jobstore.request_cancel(job_id))


@app.post("/api/jobs/<job_id>/resume")
def resume_job(job_id: str):
    job = jobstore.get(job_id)
    if job is None:
        abort(404)
    if job["state"] == "running":
        return jsonify(error="that run is still going"), 409
    if not jobstore.resume_plan(job):
        return jsonify(error="nothing left to resume — every archive is downloaded"), 400
    token = (request.get_json(force=True, silent=True) or {}).get("token", "")
    spec = jobstore.spec_for_resume(job, token=token)
    try:
        spec.validate()
    except MigrationError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(id=_start(spec))


@app.get("/api/runs")
def runs():
    return jsonify(runs=list_runs())


@app.get("/api/archive/<run>/<name>")
def archive(run: str, name: str):
    path = (_run_root(run) / name).resolve()
    if path.parent != _run_root(run) or not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


@app.get("/api/archive-zip/<run>")
def archive_zip(run: str):
    """Every archive in a run as one .zip (stored, not re-compressed)."""
    path = _run_root(run)
    members = sorted(p for p in path.iterdir() if p.is_file() and not p.name.endswith(".part"))
    if not members:
        abort(404)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        for member in members:
            zf.write(member, arcname=member.name)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name=f"{run}.zip")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration downloader web UI")
    parser.add_argument("--host", default=os.environ.get("MD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MD_PORT", 8765)))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    restored = jobstore.load_from_disk()
    print(f"migration-downloader UI  →  http://{args.host}:{args.port}")
    print(f"env file    : {ENV_FILE}")
    print(f"outputs dir : {outputs_for(COMPONENT)}")
    if restored:
        print(f"restored {restored} job record(s) from disk")
    # use_reloader off: the reloader's second process would double every job
    app.run(host=args.host, port=args.port, debug=args.debug,
            threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
