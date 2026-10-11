"""Resume: which targets are outstanding, which ids are reusable, and the manifest a resume leaves."""

import io
import json
import tarfile

import pytest

import jobstore
import runner
from errors import MigrationError


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    import datalabs_paths
    monkeypatch.setattr(datalabs_paths, "OUTPUTS_ROOT", tmp_path)
    monkeypatch.setattr(jobstore, "JOBS_DIR", tmp_path / runner.COMPONENT / ".jobs")
    jobstore.JOBS_DIR.mkdir(parents=True)
    monkeypatch.setattr(jobstore, "_jobs", {})
    monkeypatch.setattr(jobstore, "_dirty", {})
    monkeypatch.setattr(jobstore, "_plan_cache", {})
    return tmp_path / runner.COMPONENT


def make_job(spec, state="interrupted", log=(), manifest=None):
    return {"id": "j1", "spec": jobstore._public_spec(spec), "state": state, "updated_utc": "t",
            "manifest": manifest, "log": list(log)}


def spec_of(**kw):
    base = dict(provider="github", token="t", targets=["o/a", "o/b"], run_name="r1")
    return runner.JobSpec(**{**base, **kw})


def test_failed_migration_ids_are_not_carried_into_a_resume(outputs):
    manifest = {"output_dir": str(outputs / "r1"), "archives": [
        {"target": "o/a", "migration_id": "11", "ok": False, "error": "migration 11 ended in state 'failed'"},
        {"target": "o/b", "migration_id": "12", "ok": False, "error": "timed out after 60s waiting for migration 12"}]}
    plan = jobstore.resume_plan(make_job(spec_of(), "partial", manifest=manifest))
    assert plan["known_ids"] == {"o/b": "12"}          # the timeout is retryable, the failure is dead


def test_dead_id_in_the_log_is_dropped(outputs):
    log = ["[1/2] o/a", "  migration id = 11 (state pending)", "  ERROR migration 11 ended in state 'failed'",
           "[2/2] o/b", "  migration id = 12 (state pending)"]
    plan = jobstore.resume_plan(make_job(spec_of(), log=log))
    assert plan["known_ids"] == {"o/b": "12"}


class DeadThenLive:
    TERMINAL_OK = "exported"

    def __init__(self, *a, **kw):
        self.scope, self.org = "user", ""
        self.api_base, self.api_version, self.lock_repositories = "https://api.github.com", "v", False
        self.download_info, self.started, self._headers = {}, 0, {}

    def whoami(self):
        return "me"

    def normalise(self, r):
        return r

    def start(self, repos):
        self.started += 1
        return "99"

    def wait(self, mid, **kw):
        if mid == "11":
            raise MigrationError("HTTP 404 GET https://api.github.com/user/migrations/11\n{}")
        return {"state": "exported"}

    def download(self, mid, dest):
        with tarfile.open(dest, "w:gz") as t:
            ti = tarfile.TarInfo("x")
            ti.size = 1
            t.addfile(ti, io.BytesIO(b"x"))


def test_a_404_on_a_known_id_starts_a_new_migration(outputs, monkeypatch):
    monkeypatch.setattr(runner, "GitHubMigration", DeadThenLive)
    spec = spec_of(targets=["o/a"], known_ids={"o/a": "11"}, skip_done=True, for_check=False, verify_checksum=False)
    manifest = runner.run_job(spec, log=lambda s: None)
    assert manifest["ok"] == 1 and manifest["archives"][0]["migration_id"] == "99"


def test_archive_on_disk_without_a_manifest_settles_the_target(outputs):
    run = outputs / "r1"
    run.mkdir()
    (run / "o-a-123.tar.gz").write_bytes(b"data")           # id never reached the manifest
    (run / "o-b-v2-9.tar.gz").write_bytes(b"data")          # a different target's stem must not match o-b
    job = make_job(spec_of(), log=["output dir : " + str(run)])
    assert jobstore.resume_plan(job)["targets"] == ["o/b"]


def test_gitlab_resume_precheck_finds_the_id_suffixed_archive(outputs, monkeypatch):
    run = outputs / "r1"
    run.mkdir()
    (run / "g-p-77.tar.gz").write_bytes(b"data")

    class Boom:
        def __init__(self, *a, **k):
            self.api_base, self._headers, self.download_info = "https://gitlab.com", {}, {}

        def whoami(self):
            return "me"

        def normalise(self, p):
            return p

        def start(self, *a, **k):
            raise AssertionError("must not re-export a project already on disk")

    monkeypatch.setattr(runner, "GitLabExport", Boom)
    spec = spec_of(provider="gitlab", targets=["g/p"], skip_done=True, for_check=False, verify_checksum=False)
    manifest = runner.run_job(spec, log=lambda s: None)
    assert manifest["ok"] == 1 and manifest["archives"][0]["skipped"] is True


def test_single_archive_label_counts_as_done_for_each_member(outputs):
    manifest = {"output_dir": str(outputs / "r1"), "archives": [
        {"target": "o/a, o/b", "migration_id": "5", "ok": True}]}
    job = make_job(spec_of(single_archive=True), "partial", manifest=manifest)
    assert jobstore.resume_plan(job) is None


def test_single_archive_outstanding_when_label_not_done(outputs):
    plan = jobstore.resume_plan(make_job(spec_of(single_archive=True), log=["[1/1] o/a, o/b",
                                                                           "  migration id = 5 (state pending)"]))
    assert plan["targets"] == ["o/a", "o/b"] and plan["known_ids"] == {"o/a, o/b": "5"}


def test_resume_keeps_dc_and_for_check_fields_and_refuses_uploads(outputs):
    spec = spec_of(provider="bitbucket-dc", api_base="https://bb.example.com", dc_job_id="J", for_check=False,
                   extras=["archive"])
    resumed = jobstore.spec_for_resume(make_job(spec), token="t")
    assert resumed.dc_job_id == "J" and resumed.for_check is False
    up = spec_of(provider="gitlab", upload_url="https://s3.example.com/x?sig=abc", upload_method="POST")
    job = make_job(up)
    assert job["spec"]["upload_method"] == "POST" and "abc" not in json.dumps(job["spec"])
    assert jobstore.resume_plan(job) is None


def test_resume_endpoint_explains_the_upload_refusal_and_warns_about_env_token(outputs, monkeypatch):
    import app
    client = app.app.test_client()
    up = jobstore.create(spec_of(provider="gitlab", upload_url="https://s3.example.com/x?sig=abc"))
    jobstore.update(up, state="error")
    reply = client.post(f"/api/jobs/{up}/resume", json={})
    assert reply.status_code == 400 and "presigned" in reply.get_json()["error"]

    monkeypatch.setattr(runner, "github_token", lambda: "envtok")
    monkeypatch.setattr(app.threading, "Thread", lambda **k: type("T", (), {"start": lambda self: None})())
    pasted = jobstore.create(spec_of(run_name="r2"))
    jobstore.update(pasted, state="error")
    reply = client.post(f"/api/jobs/{pasted}/resume", json={"token": ""})
    assert reply.status_code == 200
    assert any("pasted token" in l for l in jobstore.get(reply.get_json()["id"])["log"])


def test_second_resume_of_the_same_run_is_refused(outputs, monkeypatch):
    import app
    monkeypatch.setattr(runner, "github_token", lambda: "envtok")
    monkeypatch.setattr(app.threading, "Thread", lambda **k: type("T", (), {"start": lambda self: None})())
    client = app.app.test_client()
    job = jobstore.create(spec_of())
    jobstore.update(job, state="error")
    assert client.post(f"/api/jobs/{job}/resume", json={}).status_code == 200
    assert client.post(f"/api/jobs/{job}/resume", json={}).status_code == 409


def test_resume_manifest_keeps_earlier_archives(outputs, monkeypatch):
    run = outputs / "r1"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({
        "started_utc": "2026-01-01T00:00:00+00:00", "elapsed_seconds": 10,
        "archives": [{"target": "o/a", "ok": True, "sha256": "abc", "started": 1},
                     {"target": "o/b", "ok": False, "error": "timed out"}],
        "extras": [], "export_jobs": []}))
    monkeypatch.setattr(runner, "GitHubMigration", DeadThenLive)
    spec = spec_of(targets=["o/b"], skip_done=True, for_check=False, verify_checksum=False)
    manifest = runner.run_job(spec, log=lambda s: None)
    by_target = {a["target"]: a for a in manifest["archives"]}
    assert by_target["o/a"]["sha256"] == "abc" and by_target["o/b"]["ok"]
    assert manifest["ok"] == 2 and manifest["archives_failed"] == 0 and manifest["failed"] == 0
    assert manifest["started_utc"] == "2026-01-01T00:00:00+00:00"
    assert json.loads((run / "manifest.json").read_text())["ok"] == 2


def test_resume_plan_is_cached_until_something_changes(outputs, monkeypatch):
    calls = []
    real = jobstore._compute_plan
    monkeypatch.setattr(jobstore, "_compute_plan", lambda *a: calls.append(1) or real(*a))
    job = make_job(spec_of())
    jobstore.resume_plan(job)
    jobstore.resume_plan(job)
    assert len(calls) == 1
    job["log"].append("more")
    jobstore.resume_plan(job)
    assert len(calls) == 2
