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
from migration_api import GitHubMigration, GitLabExport, MigrationError, mask  # noqa: E402
from runner import COMPONENT, JobSpec, list_runs, run_job  # noqa: E402
from supplementary import HEAVY, REGISTRY  # noqa: E402

import jobstore  # noqa: E402
import redact  # noqa: E402

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


def _env_hint(provider: str) -> str:
    token = JobSpec(provider=provider).resolved_token()
    return mask(token) if token else ""


@app.get("/")
def index():
    return render_template(
        "index.html",
        env_file=str(ENV_FILE),
        outputs_dir=str(outputs_for(COMPONENT)),
        github_env=mask(github_token()) if github_token() else "",
        gitlab_env=mask(gitlab_token()) if gitlab_token() else "",
        bitbucket_env=_env_hint("bitbucket"),
        bitbucket_dc_env=_env_hint("bitbucket-dc"),
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


@app.get("/api/jobs/<job_id>/log")
def job_log(job_id: str):
    """The run log as a downloadable text file, with secrets redacted.

    Safe to attach to a bug report: tokens, auth headers and credential-bearing
    URLs are scrubbed, the home directory is collapsed to `~`, and the log
    itself only ever contained API URLs, states, sizes and file names — never
    repository content.
    """
    job = jobstore.get(job_id)
    if job is None:
        abort(404)
    spec = job.get("spec", {})
    manifest = job.get("manifest") or {}
    host = spec.get("api_base") or {
        "github": "https://api.github.com",
        "bitbucket": "https://api.bitbucket.org/2.0",
    }.get(spec.get("provider"), "https://gitlab.com")
    header = [
        "# migration-downloader run log",
        f"# run id     : {job['id']}",
        f"# started    : {job.get('created_utc', '')}",
        f"# finished   : {job.get('updated_utc', '')}",
        f"# state      : {job.get('state', '')}",
        f"# provider   : {spec.get('provider', '')}"
        + (f" ({spec.get('scope')} scope)" if spec.get("provider") == "github" else ""),
        f"# host       : {host}",
        f"# targets    : {len(spec.get('targets', []))}",
        f"# archives   : {manifest.get('ok', 0)} ok, {manifest.get('failed', 0)} failed",
        f"# token      : {spec.get('token_hint', '')}"
        + (" (from .env)" if spec.get("token_from_env") else ""),
        "#",
        "# secrets are redacted and no repository content is included.",
        "",
    ]
    body = header + redact.scrub_lines(job.get("log", []))
    if manifest.get("archives"):
        body += ["", "# --- archives " + "-" * 46]
        for a in manifest["archives"]:
            state = "ok" if a.get("ok") else f"FAILED: {a.get('error', '')}"
            body.append(f"#   {a.get('target')}  id={a.get('migration_id') or '-'}  "
                        f"{a.get('size') or '-'}  sha256={a.get('sha256') or '-'}  {state}")
    payload = redact.scrub("\n".join(body) + "\n")
    return send_file(
        io.BytesIO(payload.encode()),
        mimetype="text/plain",
        as_attachment=True,
        download_name=f"migration-downloader-{job['id']}.log",
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


@app.post("/api/discover")
def discover():
    """List what the token can see: orgs/groups, or repos/projects.

    POST, not GET, so the PAT never travels in a URL or a server access log.
    `api_base` is honoured throughout, so this works against GitHub Enterprise
    and a self-hosted GitLab exactly as it does against the public hosts.
    """
    data = request.get_json(force=True, silent=True) or {}
    provider = (data.get("provider") or "github").strip().lower()
    kind = (data.get("kind") or "repos").strip().lower()       # repos | owners
    scope = (data.get("scope") or "user").strip().lower()
    owner = (data.get("owner") or "").strip()                  # org / group
    api_base = (data.get("api_base") or "").strip()
    token = (data.get("token") or "").strip() or (
        github_token() if provider == "github" else gitlab_token())
    if not token:
        return jsonify(error=f"no {provider} token supplied and none found in .env"), 400
    try:
        if provider == "github":
            client = GitHubMigration(token, scope=scope, org=owner or "x",
                                     api_base=api_base or "https://api.github.com")
            items = client.list_orgs() if kind == "owners" else client.list_repos(
                org=owner if scope == "org" else "")
        else:
            client = GitLabExport(token, api_base=api_base or "https://gitlab.com")
            items = client.list_groups() if kind == "owners" else client.list_projects(group=owner)
    except MigrationError as exc:
        return jsonify(error=str(exc).splitlines()[0]), 400
    return jsonify(items=items, count=len(items))


@app.get("/api/extras")
def extras():
    """Collector names per provider, for the UI's Extras picker."""
    return jsonify(extras={
        provider: [{"name": name, "heavy": name in HEAVY.get(provider, set())}
                   for name in sorted(names, key=lambda n: (n in HEAVY.get(provider, set()), n))]
        for provider, names in REGISTRY.items()})


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
