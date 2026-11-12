"""Bitbucket Cloud archive at GitHub/GitLab parity: the git mirror plus the repo's PR history and settings in one
tar.gz, extras that run by default and can be unticked, and per-commit extras computed from the mirror.

Offline: git is the real git against a local bare repo standing in for bitbucket.org; the REST API is FakeBB."""

import json
import subprocess
import tarfile
from pathlib import Path

import pytest

import bitbucket
import bitbucket_archive
import cli
import for_check
import jobstore
import runner
import supplementary
from errors import MigrationError
from test_bitbucket_review import FakeBB

WS_REPO = "/repositories/ws/shop"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
                               "GIT_COMMITTER_EMAIL": "a@x", "HOME": str(cwd or "/tmp"), "PATH": "/usr/bin:/bin"}).stdout


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    import datalabs_paths
    monkeypatch.setattr(datalabs_paths, "OUTPUTS_ROOT", tmp_path / "out")
    monkeypatch.setattr(jobstore, "JOBS_DIR", tmp_path / "out" / runner.COMPONENT / ".jobs")
    jobstore.JOBS_DIR.mkdir(parents=True)
    return tmp_path / "out" / runner.COMPONENT


@pytest.fixture
def remote(tmp_path):
    """main (one commit), feature (+ b.txt), tag v1; returns (bare path, {name: sha})."""
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    (work / "a.txt").write_text("1\n")
    git("add", ".", cwd=work)
    git("commit", "-qm", "first", cwd=work)
    main = git("rev-parse", "HEAD", cwd=work).strip()
    git("checkout", "-qb", "feature", cwd=work)
    (work / "b.txt").write_text("2\n3\n")
    git("add", ".", cwd=work)
    git("commit", "-qm", "second", cwd=work)
    git("tag", "v1", cwd=work)
    feature = git("rev-parse", "HEAD", cwd=work).strip()
    bare = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(work), str(bare))
    return bare, {"main": main, "feature": feature}


@pytest.fixture
def fake_git(monkeypatch, remote):
    """Clones come from the local remote; `git lfs` is absent unless a test says otherwise."""
    bare, _ = remote
    seen = []
    real = supplementary.run_git
    state = {"lfs": False}

    def run_git(args, *, env, cwd=None):
        seen.append(list(args))
        clean = {k: v for k, v in env.items() if not k.startswith("GIT_CONFIG")}
        if args[0] == "lfs":
            if not state["lfs"]:
                return subprocess.CompletedProcess(args, 1, "", "git: 'lfs' is not a git command.")
            return subprocess.CompletedProcess(args, 0, "git-lfs/3.0\n" if args[1] == "version" else "", "")
        if args[:2] == ["clone", "--mirror"]:
            return real(args[:3] + [str(bare)] + args[4:], env=clean, cwd=cwd)
        return real(args, env=clean, cwd=cwd)

    monkeypatch.setattr(supplementary, "run_git", run_git)
    return seen, state


def pr(pid, source, state="MERGED", tasks=0):
    return {"id": pid, "title": f"PR {pid}", "state": state, "task_count": tasks, "comment_count": 2,
            "author": {"uuid": "{u1}", "display_name": "Ann", "nickname": "ann"},
            "reviewers": [{"uuid": "{u2}", "display_name": "Bob", "nickname": "bob"}],
            "participants": [{"user": {"uuid": "{u2}", "display_name": "Bob", "nickname": "bob"}, "approved": True}],
            "source": {"commit": {"hash": source}, "branch": {"name": "feature"}},
            "destination": {"commit": {"hash": "0" * 12}, "branch": {"name": "main"}}}


def activity():
    bob = {"uuid": "{u2}", "display_name": "Bob", "nickname": "bob"}
    cara = {"uuid": "{u3}", "display_name": "Cara", "nickname": "cara"}
    return [
        {"pull_request": {"id": 1}, "approval": {"user": bob, "date": "2026-01-02"}},
        {"pull_request": {"id": 1}, "changes_requested": {"user": cara, "date": "2026-01-01"}},
        {"pull_request": {"id": 1}, "comment": {"id": 10, "user": cara, "content": {"raw": "nit"},
                                                 "inline": {"path": "b.txt", "to": 1}}},
        {"pull_request": {"id": 2}, "comment": {"id": 11, "user": bob, "content": {"raw": "lgtm"}}},
        {"pull_request": {"id": 2}, "update": {"state": "MERGED", "author": bob, "date": "2026-01-03"}},
    ]


def routes(shas, **override):
    table = {
        f"{WS_REPO}/pullrequests/activity": {"values": activity()},
        f"{WS_REPO}/pullrequests/1/tasks": {"values": [{"id": 5, "content": {"raw": "fix"}}]},
        f"{WS_REPO}/pullrequests/1/activity": {"values": activity()[:3]},
        f"{WS_REPO}/pullrequests/2/activity": {"values": activity()[3:]},
        f"{WS_REPO}/pullrequests?": lambda url: {"values": [pr(1, shas["feature"][:12], tasks=1),
                                                            pr(2, "deadbeef0000", state="DECLINED")], "size": 2},
        f"{WS_REPO}/branch-restrictions": {"values": [{"kind": "push", "pattern": "main"}]},
        f"{WS_REPO}/branching-model": {"development": {"name": "main"}},
        f"{WS_REPO}/downloads": {"values": [{"name": "app.zip", "size": 3}]},
        f"{WS_REPO}/hooks": {"values": [{"url": "https://ci.example.com/hook", "events": ["repo:push"]}]},
        f"{WS_REPO}/pipelines_config/schedules": {"values": [{"uuid": "{s1}", "cron_pattern": "0 0 * * *"}]},
        f"{WS_REPO}/pipelines": {"values": [{"uuid": "{p1}", "build_number": 1, "state": {"name": "COMPLETED"}}]},
        f"{WS_REPO}/environments": {"values": [{"uuid": "{e1}", "name": "prod"}]},
        f"{WS_REPO}/deployments": {"values": [{"uuid": "{d1}", "state": {"name": "COMPLETED"}}]},
        "/workspaces/ws/projects/PRJ": {"key": "PRJ", "name": "Project"},
        "/workspaces/ws": {"slug": "ws", "name": "Workspace"},
        WS_REPO: {"full_name": "ws/shop", "project": {"key": "PRJ"}, "mainbranch": {"name": "main"}, "size": 1234},
    }
    for key, value in override.items():
        table[key.replace("@", WS_REPO)] = value
    return {k: table[k] for k in sorted(table, key=lambda k: -len(k))}     # longest needle wins


@pytest.fixture
def fake_bb(monkeypatch):
    def install(table):
        fake = FakeBB(table)
        monkeypatch.setattr(supplementary, "_json_page", fake.page)
        monkeypatch.setattr(supplementary, "_json_request", fake.request)
        monkeypatch.setattr(supplementary, "_download", fake.download)
        return fake
    return install


def unpack(archive: Path, into: Path) -> Path:
    with tarfile.open(archive) as t:
        t.extractall(into, filter="data")
    return into


def load(root: Path, name: str):
    return json.loads((root / name).read_text())


def run(outputs, name="r1", **kw):
    spec = runner.JobSpec(provider="bitbucket", token="me@x.com:ATATT-secret", targets=["ws/shop"], run_name=name,
                          for_check=False, verify_checksum=False, **kw)
    return runner.run_job(spec, log=lambda s: None)


# ── the archive ──────────────────────────────────────────────────────────────

def test_the_archive_holds_the_mirror_and_the_parity_metadata(outputs, fake_git, fake_bb, remote):
    _, shas = remote
    fake_bb(routes(shas))
    manifest = run(outputs)
    assert manifest["failed"] == 0, manifest["issues"]
    root = unpack(outputs / "r1" / "ws-shop.tar.gz", outputs / "x")
    assert (root / "shop.git" / "HEAD").is_file()
    assert load(root, "repositories_000001.json")[0]["full_name"] == "ws/shop"
    orgs = load(root, "organizations_000001.json")
    assert {o.get("slug") or o.get("key") for o in orgs} == {"ws", "PRJ"}
    prs = load(root, "pull_requests_000001.json")
    assert [p["id"] for p in prs] == [1, 2]
    reviews = load(root, "pull_request_reviews_000001.json")
    assert sorted(r["state"] for r in reviews) == ["approved", "changes_requested"]
    assert all(r["pull_request_id"] == 1 for r in reviews)
    inline = load(root, "pull_request_review_comments_000001.json")
    plain = load(root, "issue_comments_000001.json")
    assert [c["comment"]["id"] for c in inline] == [10] and [c["comment"]["id"] for c in plain] == [11]
    assert load(root, "issue_events_000001.json")[0]["update"]["state"] == "MERGED"
    assert len(load(root, "raw/pullrequest_activity.json")) == 5
    assert load(root, "protected_branches_000001.json")["branch_restrictions"][0]["kind"] == "push"
    assert load(root, "releases_000001.json")[0]["name"] == "app.zip"
    assert load(root, "webhooks_000001.json")[0]["events"] == ["repo:push"]
    assert load(root, "pipelines_000001.json")[0]["build_number"] == 1
    assert load(root, "pipeline_schedules_000001.json")[0]["cron_pattern"] == "0 0 * * *"
    users = load(root, "users_000001.json")
    assert sorted(u["nickname"] for u in users) == ["ann", "bob", "cara"]
    schema = load(root, "schema.json")
    assert schema["format"] == "bitbucket-cloud-archive" and not schema["errors"]
    assert "issues" in schema["not_available"] and "deploy_keys" in schema["omitted_by_policy"]
    assert schema["pull_request_refs"] == {"written": 1, "source_commit_missing": [2]}
    # PR 1's source commit is in the mirror: refs/pull/1/head points at it, like a GitHub archive
    head = git("--git-dir", str(root / "shop.git"), "rev-parse", "refs/pull/1/head").strip()
    assert head == shas["feature"]


def test_the_core_costs_one_listing_pass_not_calls_per_pr(outputs, fake_git, fake_bb, remote):
    _, shas = remote
    fake = fake_bb(routes(shas))
    run(outputs)
    per_pr = [c for c in fake.calls if "/pullrequests/1" in c or "/pullrequests/2" in c]
    assert per_pr == []
    pr_list = [c for c in fake.calls if "/pullrequests?" in c][0]
    assert "fields=" in pr_list and "reviewers" in pr_list and "pagelen=50" in pr_list


def test_a_refused_activity_feed_falls_back_to_the_per_pr_feed(outputs, fake_git, fake_bb, remote):
    _, shas = remote
    fake_bb(routes(shas, **{"@/pullrequests/activity": MigrationError("HTTP 404 GET x\n{}")}))
    manifest = run(outputs)
    root = unpack(outputs / "r1" / "ws-shop.tar.gz", outputs / "x")
    assert len(load(root, "raw/pullrequest_activity.json")) == 5
    assert "per pull request" in " ".join(load(root, "schema.json")["notes"])
    assert manifest["failed"] == 0


def test_a_pr_listing_that_refuses_fields_is_retried_plain(outputs, fake_git, fake_bb, remote):
    _, shas = remote
    plain = {"values": [pr(1, shas["feature"][:12])]}
    fake_bb(routes(shas, **{"@/pullrequests?": lambda url: MigrationError("HTTP 400 GET x\n{}") if "fields=" in url
                            else plain}))
    run(outputs)
    root = unpack(outputs / "r1" / "ws-shop.tar.gz", outputs / "x")
    assert [p["id"] for p in load(root, "pull_requests_000001.json")] == [1]


def test_a_failed_part_is_reported_but_the_archive_still_ships(outputs, fake_git, fake_bb, remote):
    _, shas = remote
    fake_bb(routes(shas, **{"@/hooks": MigrationError("HTTP 403 GET hooks\n{}")}))
    manifest = run(outputs)
    root = unpack(outputs / "r1" / "ws-shop.tar.gz", outputs / "x")
    assert (root / "shop.git" / "HEAD").is_file()
    errors = load(root, "schema.json")["errors"]
    assert errors and errors[0]["part"] == "webhooks" and "403" in errors[0]["error"]
    archive = manifest["archives"][0]
    assert archive["ok"] and archive["incomplete"]
    assert any(i["step"] == "archive: webhooks" for i in manifest["issues"])
    assert manifest["failed"] == 1


def test_a_surprise_in_the_metadata_still_ships_the_git_history(outputs, fake_git, fake_bb, remote, monkeypatch):
    fake_bb(routes(remote[1]))

    def boom(*a, **kw):
        raise KeyError("values")

    monkeypatch.setattr(bitbucket_archive, "split_activity", boom)
    manifest = run(outputs)
    root = unpack(outputs / "r1" / "ws-shop.tar.gz", outputs / "x")
    assert (root / "shop.git" / "HEAD").is_file()
    assert manifest["archives"][0]["incomplete"][0]["part"] == "metadata"
    assert manifest["failed"] == 1


def test_pipelines_never_set_up_is_not_an_error(outputs, fake_git, fake_bb, remote):
    _, shas = remote
    gone = MigrationError("HTTP 404 GET pipelines\n{}")
    fake_bb(routes(shas, **{"@/pipelines": gone, "@/pipelines_config/schedules": gone}))
    manifest = run(outputs)
    root = unpack(outputs / "r1" / "ws-shop.tar.gz", outputs / "x")
    assert load(root, "pipelines_000001.json") == [] and not load(root, "schema.json")["errors"]
    assert manifest["failed"] == 0


def test_secrets_in_metadata_are_scrubbed(outputs, fake_git, fake_bb, remote):
    _, shas = remote
    fake_bb(routes(shas, **{"@/hooks": {"values": [{"url": "https://x", "secret": "s3cr3t-value"}]}}))
    run(outputs)
    root = unpack(outputs / "r1" / "ws-shop.tar.gz", outputs / "x")
    assert "s3cr3t-value" not in (root / "webhooks_000001.json").read_text()


# ── LFS ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def lfs_remote(tmp_path, remote, monkeypatch):
    """The remote's history uses LFS (.gitattributes with filter=lfs)."""
    bare, shas = remote
    work = tmp_path / "lfs-work"
    git("clone", "-q", str(bare), str(work))
    (work / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    git("add", ".", cwd=work)
    git("commit", "-qm", "lfs", cwd=work)
    git("push", "-q", "origin", "HEAD", cwd=work)
    return bare, shas


def test_lfs_objects_are_fetched_when_the_history_uses_lfs(outputs, fake_git, fake_bb, lfs_remote):
    seen, state = fake_git
    state["lfs"] = True
    fake_bb(routes(lfs_remote[1]))
    manifest = run(outputs)
    assert ["lfs", "fetch", "--all", "origin"] in seen
    assert manifest["failed"] == 0


def test_lfs_without_git_lfs_installed_is_an_issue_not_a_silent_gap(outputs, fake_git, fake_bb, lfs_remote):
    fake_bb(routes(lfs_remote[1]))
    manifest = run(outputs)
    root = unpack(outputs / "r1" / "ws-shop.tar.gz", outputs / "x")
    errors = load(root, "schema.json")["errors"]
    assert errors[0]["part"] == "lfs" and "git-lfs is not installed" in errors[0]["error"]
    assert manifest["failed"] == 1


def test_no_lfs_fetch_when_the_history_does_not_use_it(outputs, fake_git, fake_bb, remote):
    seen, state = fake_git
    state["lfs"] = True
    fake_bb(routes(remote[1]))
    run(outputs)
    assert not any(a[:2] == ["lfs", "fetch"] for a in seen)


# ── extras from the mirror ───────────────────────────────────────────────────

def test_per_commit_extras_come_from_the_mirror_with_no_api_calls(outputs, fake_git, fake_bb, remote):
    _, shas = remote
    fake = fake_bb(routes(shas))
    manifest = run(outputs, extras=["commits", "commit-diffstat", "commit-patches"])
    assert manifest["failed"] == 0, manifest["issues"]
    assert not [c for c in fake.calls if "/commits" in c or "/diffstat/" in c or "/patch/" in c]
    out = outputs / "r1" / "extras" / "ws-shop"
    commits = load(out, "commits.json")
    assert {c["hash"] for c in commits} == {shas["main"], shas["feature"]}
    second = [c for c in commits if c["hash"] == shas["feature"]][0]
    assert second["parents"] == [{"hash": shas["main"]}] and second["message"].strip() == "second"
    diffstat = load(out, "commit-diffstat.json")[shas["feature"]]["values"]
    assert diffstat == [{"status": "added", "lines_added": 2, "lines_removed": 0, "old": None,
                         "new": {"path": "b.txt"}}]
    patch = (out / "commit-patches" / f"{shas['feature']}.patch").read_text()
    assert "+++ b/b.txt" in patch and "second" in patch
    assert not (outputs / "r1" / ".mirror-work").exists()


def test_without_a_mirror_the_per_commit_extras_still_use_the_api(outputs, fake_bb, remote):
    _, shas = remote
    fake = fake_bb(routes(shas, **{"@/commits": {"values": [{"hash": shas["main"]}]}}))
    run(outputs, extras=["commits"], mirror=False)
    assert [c for c in fake.calls if "/commits" in c]


# ── new extras ───────────────────────────────────────────────────────────────

def test_pr_tasks_are_fetched_only_for_prs_that_have_tasks(tmp_path, fake_bb, remote):
    fake = fake_bb(routes(remote[1]))
    ctx = supplementary.Ctx(provider="bitbucket", target="ws/shop", token="t", api_base=bitbucket.BITBUCKET_CLOUD_BASE,
                            headers={}, out=tmp_path / "o", log=lambda m: None)
    ctx.out.mkdir()
    supplementary.REGISTRY["bitbucket"]["pr-tasks"](ctx)
    assert [c for c in fake.calls if "/tasks" in c] and not [c for c in fake.calls if "/pullrequests/2/tasks" in c]
    assert json.loads((ctx.out / "pr-tasks.json").read_text())["1"]["values"][0]["id"] == 5


def test_deployments_collects_deployments_and_environments(tmp_path, fake_bb, remote):
    fake_bb(routes(remote[1]))
    ctx = supplementary.Ctx(provider="bitbucket", target="ws/shop", token="t", api_base=bitbucket.BITBUCKET_CLOUD_BASE,
                            headers={}, out=tmp_path / "o", log=lambda m: None)
    ctx.out.mkdir()
    supplementary.REGISTRY["bitbucket"]["deployments"](ctx)
    data = json.loads((ctx.out / "deployments.json").read_text())
    assert data["deployments"][0]["uuid"] == "{d1}" and data["environments"][0]["name"] == "prod"


def test_test_reports_skip_steps_without_reports_quietly(tmp_path, fake_bb, remote):
    fake_bb(routes(remote[1], **{
        "@/pipelines/%7Bp1%7D/steps": {"values": [{"uuid": "{s1}"}, {"uuid": "{s2}"}]},
        "@/pipelines/%7Bp1%7D/steps/%7Bs1%7D/test_reports/test_cases": {"values": [{"name": "t", "status": "PASSED"}]},
        "@/pipelines/%7Bp1%7D/steps/%7Bs1%7D/test_reports": {"number_of_test_cases": 1},
        "@/pipelines/%7Bp1%7D/steps/%7Bs2%7D": MigrationError("HTTP 404 GET test_reports\n{}"),
    }))
    ctx = supplementary.Ctx(provider="bitbucket", target="ws/shop", token="t", api_base=bitbucket.BITBUCKET_CLOUD_BASE,
                            headers={}, out=tmp_path / "o", log=lambda m: None)
    ctx.out.mkdir()
    supplementary.REGISTRY["bitbucket"]["test-reports"](ctx)
    data = json.loads((ctx.out / "test-reports.json").read_text())
    assert list(data) == ["{p1}/{s1}"] and data["{p1}/{s1}"]["test_cases"][0]["name"] == "t"
    assert not ctx.item_errors


def test_new_heavy_extras_are_marked_heavy():
    assert {"commit-comments", "test-reports"} <= supplementary.HEAVY["bitbucket"]
    assert {"pr-tasks", "deployments"} <= set(supplementary.REGISTRY["bitbucket"])


# ── opt out ──────────────────────────────────────────────────────────────────

@pytest.fixture
def captured(monkeypatch):
    specs = []

    def fake_run(spec, log=print):
        specs.append(spec)
        return {"failed": 0, "output_dir": "/tmp"}

    monkeypatch.setattr(cli, "run_job", fake_run)
    return specs


def test_cli_bitbucket_runs_every_extra_unless_told_otherwise(captured):
    cli.main(["bitbucket", "ws/shop"])
    assert captured[-1].extras == ["everything"] and captured[-1].skip_extras == []
    cli.main(["bitbucket", "ws/shop", "--skip-extras", "commit-statuses,pipeline-logs"])
    assert captured[-1].skip_extras == ["commit-statuses", "pipeline-logs"]
    cli.main(["bitbucket", "ws/shop", "--extras", "none"])
    assert captured[-1].extras == []
    cli.main(["github", "o/r"])
    assert captured[-1].extras == []          # GitHub/GitLab CLI unchanged: the archive alone unless asked


def test_skipped_extras_are_left_out_and_typos_are_refused(outputs, fake_git, fake_bb, remote):
    fake = fake_bb(routes(remote[1]))
    manifest = run(outputs, extras=["all"], skip_extras=["hooks", "pipelines", "downloads"])
    ran = {e["extra"] for e in manifest["extras"]}
    assert ran and not ran & {"hooks", "pipelines", "downloads"}
    with pytest.raises(MigrationError, match="unknown extra"):
        runner.JobSpec(provider="bitbucket", token="t", targets=["ws/r"], extras=["all"],
                       skip_extras=["hookz"]).validate()
    spec = runner.JobSpec.from_form({"provider": "bitbucket", "targets": "ws/r", "extras": "everything",
                                     "skip_extras": "hooks, pipelines"})
    assert spec.skip_extras == ["hooks", "pipelines"]


# ── for-check ────────────────────────────────────────────────────────────────

def test_for_check_csv_is_written_for_the_bitbucket_archive(outputs, fake_git, fake_bb, remote, monkeypatch):
    _, shas = remote
    fake = fake_bb(routes(shas))
    monkeypatch.setattr(for_check, "_get", lambda url, headers: (fake.page(url, headers=headers)[0], {}))
    users = []

    def ls_remote(url, user, token, run=None):
        users.append((url, user, token))
        return {"refs/heads/main": shas["main"], "refs/heads/feature": shas["feature"], "refs/tags/v1": shas["feature"]}

    monkeypatch.setattr(for_check, "ls_remote_refs", ls_remote)
    spec = runner.JobSpec(provider="bitbucket", token="me@x.com:ATATT-secret", targets=["ws/shop"], run_name="fc",
                          verify_checksum=False)
    runner.run_job(spec, log=lambda s: None)
    rows = (outputs / "fc" / "ws-shop.for-check.csv").read_text().splitlines()
    text = "\n".join(rows)
    assert "ws/shop,meta,provider,bitbucket" in text
    assert "ws/shop,counts_before,branches,2" in text and "ws/shop,counts_after,tags,1" in text
    assert "ws/shop,counts_before,prs,2" in text and "ws/shop,counts_before,default_branch,main" in text
    assert "ws/shop,ref_after,refs/heads/feature," + shas["feature"] in text
    assert "ws/shop,file,schema.json," in text
    assert users[0] == ("https://bitbucket.org/ws/shop.git", "x-bitbucket-api-token-auth", "ATATT-secret")


def test_mirror_objects_are_summarised_not_listed_one_by_one():
    assert for_check.OBJECTS.match("shop.git/objects/pack/pack-1.pack")
    assert for_check.OBJECTS.match("shop.git/lfs/objects/ab/cd/abcd")
    assert for_check.OBJECTS.match("repositories/o/r.git/objects/12/34")
    assert not for_check.OBJECTS.match("shop.git/HEAD")
