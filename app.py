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
import os
import sys
import threading
import traceback
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

import bootstrap  # noqa: F401  (sets sys.path)

from datalabs_paths import ENV_FILE, github_token, gitlab_token, outputs_for  # noqa: E402
from migration_api import GitHubMigration, GitLabExport, MigrationError, brief_error, mask  # noqa: E402
from runner import COMPONENT, JobSpec, issues_from_manifest, list_runs, run_job  # noqa: E402
from supplementary import HEAVY, REGISTRY  # noqa: E402
import bundle  # noqa: E402
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
    manifest = job["manifest"]
    issues = []
    if manifest:
        issues = manifest.get("issues")
        if issues is None:
            issues = issues_from_manifest(manifest)
    return jsonify(
        id=job["id"],
        state=job["state"],
        error=job["error"],
        issues=issues,
        manifest=manifest,
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
        f"# archives   : {manifest.get('ok', 0)} ok, "
        f"{manifest.get('archives_failed', manifest.get('failed', 0))} failed",
        f"# extras     : {manifest.get('extras_failed', 0)} collector run(s) failed",
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
    """One page of what the token can see: orgs/groups, or repos/projects.

    The picker asks for page 1, 2, 3... as the user scrolls, so there is no
    ceiling on how many repos can be listed. The page number is an integer the
    server turns into a URL itself; the browser never supplies one.

    POST, not GET, so the PAT never travels in a URL or a server access log.
    `api_base` is honoured throughout, so this works against GitHub Enterprise
    and a self-hosted GitLab exactly as it does against the public hosts.
    """
    data = request.get_json(force=True, silent=True) or {}
    provider = (data.get("provider") or "github").strip().lower()
    kind = (data.get("kind") or "repos").strip().lower()       # repos | owners
    scope = (data.get("scope") or "auto").strip().lower()
    owner = (data.get("owner") or "").strip()                  # org / group
    api_base = (data.get("api_base") or "").strip()
    try:
        page = max(1, int(data.get("page") or 1))
    except (TypeError, ValueError):
        return jsonify(error="page must be a number"), 400
    token = (data.get("token") or "").strip() or (
        github_token() if provider == "github" else gitlab_token())
    if not token:
        return jsonify(error=f"no {provider} token supplied and none found in .env"), 400
    try:
        if provider == "github":
            client = GitHubMigration(token, scope="org" if scope == "org" else "user",
                                     org=owner or "x",
                                     api_base=api_base or "https://api.github.com")
            if kind == "owners":
                result = client.orgs_page(page)
            else:
                result = client.repos_page(page, org=owner if scope == "org" else "",
                                           everything=scope == "auto")
        else:
            client = GitLabExport(token, api_base=api_base or "https://gitlab.com")
            result = (client.groups_page(page) if kind == "owners"
                      else client.projects_page(page, group=owner))
    except MigrationError as exc:
        return jsonify(error=brief_error(exc).splitlines()[0]), 400
    return jsonify(items=result["items"], count=len(result["items"]), page=page,
                   has_more=result["has_more"], notes=result["notes"])


@app.get("/api/extras")
def extras():
    """Collector names per provider (light first), for the Download options dialog."""
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
    """The whole run as a zip of zips: one zip per target (archive, for-check CSV
    and extras) plus run-files.zip. Built in a temp file, removed once sent."""
    path = _run_root(run)
    try:
        built = bundle.build(path)
    except MigrationError as exc:
        status = 404 if "nothing to download" in str(exc) else 507
        return jsonify(error=brief_error(exc)), status
    size = built.stat().st_size

    def stream():
        # a generator's `finally` runs when it is exhausted *and* when the client
        # disconnects (the server closes it); `send_file`'s close hook does not
        # fire for direct-passthrough responses, which would leak the temp file
        try:
            with built.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    yield chunk
        finally:
            built.unlink(missing_ok=True)

    response = Response(stream(), mimetype="application/zip")
    response.headers.set("Content-Disposition", "attachment", filename=f"{run}.zip")
    response.headers["Content-Length"] = str(size)
    return response


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
