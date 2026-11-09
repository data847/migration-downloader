"""Bitbucket Cloud problems found reviewing a real 22-repo run: data lost or hidden, runs that cannot recover.

Everything is offline. `fake_bb` serves Bitbucket-shaped answers from a route table; downloads are faked separately.
"""

import json
from pathlib import Path

import pytest

import bitbucket
import jobstore
import runner
import supplementary
from errors import MigrationError

API = "https://api.bitbucket.org/2.0"


class FakeBB:
    """url substring -> answer. An answer is a dict/list (200), an Exception (raised), or a callable(url)."""

    def __init__(self, routes, files=None):
        self.routes = routes
        self.files = files or {}
        self.calls = []

    def page(self, url, *, headers, timeout=60):
        self.calls.append(url)
        for needle, answer in self.routes.items():
            if needle in url:
                if callable(answer):
                    answer = answer(url)
                if isinstance(answer, Exception):
                    raise answer
                return answer, None, {}
        raise MigrationError(f"HTTP 404 GET {url}\n{{}}")

    def request(self, url, *, method="GET", headers=None, payload=None, timeout=60):
        return 200, self.page(url, headers=headers)[0]

    def download(self, url, dest, *, headers, log, timeout=120, info=None):
        self.calls.append(url)
        for needle, body in self.files.items():
            if needle in url:
                if isinstance(body, Exception):
                    raise body
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body)
                return dest
        raise MigrationError(f"HTTP 404 downloading {url}\n")


@pytest.fixture
def fake_bb(monkeypatch):
    def install(routes, files=None):
        fake = FakeBB(routes, files)
        monkeypatch.setattr(supplementary, "_json_page", fake.page)
        monkeypatch.setattr(supplementary, "_json_request", fake.request)
        monkeypatch.setattr(supplementary, "_download", fake.download)
        return fake
    return install


def ctx_for(tmp_path, target="ws/repo", shared=None):
    return supplementary.Ctx(provider="bitbucket", target=target, token="me@x.com:tok", api_base=API,
                             headers={"Authorization": "Basic x"}, out=tmp_path / target.replace("/", "-"),
                             log=lambda m: None, shared=shared if shared is not None else {})


def run(name, ctx):
    ctx.out.mkdir(parents=True, exist_ok=True)
    return supplementary.REGISTRY["bitbucket"][name](ctx)


def saved(ctx, name):
    return json.loads((ctx.out / f"{name}.json").read_text())


# ── listings ────────────────────────────────────────────────────────────────


def test_a_listing_without_next_links_is_followed_by_page_number(tmp_path, fake_bb):
    def pipelines(url):
        page = 2 if "page=2" in url else 1
        return {"page": page, "pagelen": 2, "size": 3,
                "values": [{"uuid": "{a}"}, {"uuid": "{b}"}] if page == 1 else [{"uuid": "{c}"}]}
    fake_bb({"/pipelines?": pipelines, "/steps": {"values": []}, "/pipelines_config": {"values": []}})
    ctx = ctx_for(tmp_path)
    run("pipelines", ctx)
    assert [p["uuid"] for p in saved(ctx, "pipelines")] == ["{a}", "{b}", "{c}"]


def test_a_maintenance_page_is_an_error_not_an_empty_list(tmp_path, fake_bb):
    fake_bb({"/refs": {"raw": "<html>Bitbucket is down for maintenance</html>"}})
    with pytest.raises(MigrationError, match="expected JSON"):
        run("refs", ctx_for(tmp_path))


def test_per_item_listings_follow_every_page(tmp_path, fake_bb):
    def variables(url):
        if "page=2" in url:
            return {"values": [{"key": "V11"}]}
        return {"values": [{"key": f"V{i}"} for i in range(1, 11)], "next": f"{API}/repositories/ws/repo/pipelines_config/variables?page=2"}
    fake_bb({"/pipelines?": {"values": []}, "/pipelines_config/variables": variables, "/pipelines_config": {}})
    ctx = ctx_for(tmp_path)
    run("pipelines", ctx)
    assert len(saved(ctx, "pipeline-variables")["values"]) == 11


def test_pipeline_and_step_uuids_are_encoded_in_paths(tmp_path, fake_bb):
    fake = fake_bb({"/steps": {"values": [{"uuid": "{s1}"}]}, "/pipelines?": {"values": [{"uuid": "{p1}"}]}},
                   files={"/log": b"build ok"})
    run("pipeline-logs", ctx_for(tmp_path))
    assert any("/pipelines/%7Bp1%7D/steps/%7Bs1%7D/log" in c for c in fake.calls)
    assert not any("{p1}" in c for c in fake.calls)


# ── items that fail are counted, not hidden ─────────────────────────────────


def test_a_failed_commit_is_counted_and_the_others_kept(tmp_path, fake_bb):
    fake_bb({"/commit/bad/statuses": MigrationError("HTTP 502 GET x"), "/statuses": {"values": [{"state": "OK"}]},
             "/commits": {"values": [{"hash": "good"}, {"hash": "bad"}]}})
    ctx = ctx_for(tmp_path)
    run("commit-statuses", ctx)
    out = saved(ctx, "commit-statuses")
    assert out["good"] == {"values": [{"state": "OK"}]} and "error" in out["bad"]
    assert [e["what"] for e in ctx.item_errors] == ["commit bad statuses"]


def test_pr_diff_failures_are_counted(tmp_path, fake_bb):
    fake_bb({"/pullrequests?": {"values": [{"id": 7}]}, "/diffstat": {"values": []}},
            files={"/7/diff": MigrationError("HTTP 555 GET diff timeout"), "/7/patch": b"patch"})
    ctx = ctx_for(tmp_path)
    run("pr-diffs", ctx)
    assert [e["what"] for e in ctx.item_errors] == ["PR 7 diff"]


# ── one failure loses only its own item ─────────────────────────────────────


def test_failing_repo_webhooks_do_not_skip_workspace_webhooks(tmp_path, fake_bb):
    fake = fake_bb({"/repositories/ws/repo/hooks": MigrationError("HTTP 403 GET hooks: admin needed"),
                    "/workspaces/ws/hooks": {"values": [{"uuid": "{h}"}]}})
    ctx = ctx_for(tmp_path)
    files = run("hooks", ctx)
    assert files == ["workspace-hooks-ws.json"]
    assert [e["what"] for e in ctx.item_errors] == ["repository webhooks"]


def test_workspace_webhooks_are_tried_once_even_when_they_fail(tmp_path, fake_bb):
    fake = fake_bb({"/workspaces/ws/hooks": MigrationError("HTTP 403 workspace admin needed"), "/hooks": {"values": []}})
    shared = {}
    run("hooks", ctx_for(tmp_path, "ws/a", shared))
    run("hooks", ctx_for(tmp_path, "ws/b", shared))
    assert sum("/workspaces/ws/hooks" in c for c in fake.calls) == 1


def test_snippet_files_come_from_the_snippet_itself(tmp_path, fake_bb):
    fake_bb({"/snippets/ws/s1/commits": {"values": []},
             "/snippets/ws/s1": {"id": "s1", "files": {"notes.txt": {"links": {}}}},
             "/snippets/ws": {"values": [{"id": "s1", "title": "t"}]}},
            files={"/snippets/ws/s1/files/notes.txt": b"hello"})
    ctx = ctx_for(tmp_path)
    run("snippets", ctx)
    detail = saved(ctx, "snippet-details-ws")
    assert detail["s1"]["files"] == {"notes.txt": "snippets-ws/s1/notes.txt"}
    assert (ctx.out / "snippets-ws" / "s1" / "notes.txt").read_bytes() == b"hello"


def test_failed_snippets_are_not_retried_for_every_repo(tmp_path, fake_bb):
    fake = fake_bb({"/snippets/ws": MigrationError("HTTP 403 snippets")})
    shared = {}
    with pytest.raises(MigrationError):
        run("snippets", ctx_for(tmp_path, "ws/a", shared))
    assert run("snippets", ctx_for(tmp_path, "ws/b", shared)) == []
    assert sum("/snippets/ws" in c for c in fake.calls) == 1


def test_pipeline_logs_find_their_own_steps(tmp_path, fake_bb):
    fake_bb({"/steps": {"values": [{"uuid": "{s}"}]}, "/pipelines?": {"values": [{"uuid": "{p}"}]}},
            files={"/log": b"ok"})
    ctx = ctx_for(tmp_path)     # no `pipelines` collector ran before it, so no steps are shared
    written = run("pipeline-logs", ctx)
    assert len(written) == 1 and (ctx.out / written[0]).read_bytes() == b"ok"


def test_pull_requests_survive_a_refused_superseded_state(tmp_path, fake_bb):
    def prs(url):
        if "SUPERSEDED" in url:
            return MigrationError("HTTP 400 GET pullrequests\n{\"error\": {\"message\": \"Invalid state\"}}")
        return {"values": [{"id": 1}]}
    fake_bb({"/pullrequests?": prs, "/pullrequests/1": {"id": 1, "values": []}})
    ctx = ctx_for(tmp_path)
    run("pullrequests", ctx)
    assert [p["id"] for p in saved(ctx, "pullrequests")] == [1]


def test_per_commit_collectors_are_heavy():
    names = supplementary.resolve_names("bitbucket", ["all"])
    assert not set(runner.PER_COMMIT) & set(names)
    assert set(runner.PER_COMMIT) <= set(supplementary.resolve_names("bitbucket", ["everything"]))


def test_data_center_archive_does_not_ask_for_json(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(bitbucket, "_download", lambda url, dest, headers, log: seen.update(headers) or dest)
    bitbucket.BitbucketDCExport("t", "https://bb.example.com").repo_archive("PROJ/repo", tmp_path / "x.zip")
    assert seen["Accept"] == "*/*"


def test_bare_api_token_401_explains_the_email_form():
    hint = runner.explain("pullrequests HTTP 401 GET https://api.bitbucket.org/2.0/repositories/ws/r")
    assert "email:token" in hint


# ── the run's report, checkpoints and resume ────────────────────────────────


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


def bb_spec(**kw):
    base = dict(provider="bitbucket", mirror=False, token="me@x.com:tok", targets=["ws/a", "ws/b"], run_name="bbrun",
                extras=["refs", "commit-statuses"])
    return runner.JobSpec(**{**base, **kw})


def test_item_errors_make_the_run_partial_with_an_issue(outputs, fake_bb):
    fake_bb({"/commit/bad/statuses": MigrationError("HTTP 502 GET x"), "/statuses": {"values": []},
             "/commits": {"values": [{"hash": "ok"}, {"hash": "bad"}]}, "/refs": {"values": []}})
    m = runner.run_job(bb_spec(targets=["ws/a"]), log=lambda s: None)
    rec = next(e for e in m["extras"] if e["extra"] == "commit-statuses")
    assert rec["error"] == "" and rec["item_errors"] == 1
    assert m["extras_incomplete"] == 1 and m["failed"] == 1
    assert any(i["kind"] == "incomplete" and "1 item(s)" in i["message"] for i in m["issues"])


def test_a_run_killed_mid_way_keeps_a_manifest_of_what_it_finished(outputs, fake_bb, monkeypatch):
    fake_bb({"/refs": {"values": [{"name": "main"}]}, "/commits": {"values": []}})
    real = supplementary.REGISTRY["bitbucket"]["refs"]

    def dies_on_b(ctx):
        if ctx.target == "ws/b":
            raise KeyboardInterrupt     # the process is killed: nothing in the runner catches this
        return real(ctx)
    monkeypatch.setitem(supplementary.REGISTRY["bitbucket"], "refs", dies_on_b)
    with pytest.raises(KeyboardInterrupt):
        runner.run_job(bb_spec(), log=lambda s: None)
    m = json.loads((outputs / "bbrun" / "manifest.json").read_text())
    assert m["complete"] is False
    assert {(e["target"], e["extra"]) for e in m["extras"]} == {("ws/a", "refs"), ("ws/a", "commit-statuses")}


def test_a_resume_skips_what_finished_and_redoes_what_failed(outputs, fake_bb):
    fake = fake_bb({"/commit/bad/statuses": MigrationError("HTTP 502 GET x"), "/statuses": {"values": []},
                    "/commits": {"values": [{"hash": "ok"}, {"hash": "bad"}]}, "/refs": {"values": []}})
    runner.run_job(bb_spec(targets=["ws/a"]), log=lambda s: None)
    fake.calls.clear()
    fake.routes.pop("/commit/bad/statuses")
    lines = []
    m = runner.run_job(bb_spec(targets=["ws/a"], skip_done=True), log=lines.append)
    assert "  refs: already collected, skipped" in lines
    assert not any("/refs" in c for c in fake.calls)
    assert any("/commit/bad/statuses" in c for c in fake.calls)
    assert m["failed"] == 0 and m["complete"] is True
    assert len(m["extras"]) == 2


def test_log_header_says_unknown_without_a_manifest():
    import app
    assert "unknown" in app._header_counts({})[0]
    lines = app._header_counts({"ok": 0, "archives_failed": 0, "extras_failed": 1, "extras_incomplete": 2,
                                "complete": False})
    assert "2 incomplete" in lines[1] and "did not finish" in lines[1]
