import csv
import io
import tarfile
from pathlib import Path

import pytest

import for_check as fc
import migration_api as mig
import runner


class Hdr(dict):
    def get(self, k, d=None):
        return super().get(k, d)


# ---------------------------------------------------------------- counts

GQL = {"repository": {
    "defaultBranchRef": {"name": "main", "target": {"history": {"totalCount": 607}}},
    "pullRequests": {"totalCount": 294}, "issues": {"totalCount": 12}, "labels": {"totalCount": 9},
    "milestones": {"totalCount": 2}, "releases": {"totalCount": 3}, "branches": {"totalCount": 7},
    "tags": {"totalCount": 4}, "diskUsage": 1234, "hasWikiEnabled": True, "hasIssuesEnabled": True}}


def test_github_counts_graphql_plus_rest(monkeypatch):
    monkeypatch.setattr(fc, "_graphql", lambda *a, **k: GQL)

    def fake_get(url, headers):
        if "issues/comments" in url:
            return [{}], Hdr({"Link": '<https://x/y?per_page=1&page=57>; rel="last"'})
        if "collaborators" in url:
            raise mig.MigrationError("HTTP 403 GET collaborators")
        return [{}, {}], Hdr()
    monkeypatch.setattr(fc, "_get", fake_get)
    c = fc.github_counts("acme", "widgets", headers={}, api_base="https://api.github.com")
    assert c["prs"] == 294 and c["issues"] == 12 and c["labels"] == 9 and c["milestones"] == 2
    assert c["releases"] == 3 and c["branches"] == 7 and c["tags"] == 4 and c["commits"] == 607
    assert c["default_branch"] == "main" and c["size_kb"] == 1234
    assert c["issue_comments"] == 57            # Link rel=last page with per_page=1
    assert c["review_comments"] == 2            # no Link header: length of the single page
    assert c["collaborators"] == ""             # no admin access: unknown, never a guess
    assert c["has_wiki"] == "true"


def test_github_counts_graphql_failure_leaves_unknown(monkeypatch):
    def boom(*a, **k):
        raise mig.MigrationError("HTTP 401")
    monkeypatch.setattr(fc, "_graphql", boom)
    monkeypatch.setattr(fc, "_get", lambda url, headers: ([], Hdr()))
    c = fc.github_counts("acme", "widgets", headers={}, api_base="https://api.github.com")
    assert c["prs"] == "" and c["commits"] == ""
    assert "graphql" in c["_errors"][0]


def test_graphql_url_for_enterprise():
    assert fc.graphql_url("https://api.github.com") == "https://api.github.com/graphql"
    assert fc.graphql_url("https://ghe.corp/api/v3") == "https://ghe.corp/api/graphql"


def test_web_base():
    assert fc.github_web_base("https://api.github.com") == "https://github.com"
    assert fc.github_web_base("https://ghe.corp/api/v3") == "https://ghe.corp"


def test_gitlab_counts_use_x_total(monkeypatch):
    def fake_get(url, headers):
        if "statistics=true" in url:
            return {"default_branch": "develop", "statistics": {"commit_count": 88}, "wiki_enabled": True}, Hdr()
        total = {"issues": "5", "merge_requests": "9", "labels": "4", "milestones": "1", "releases": "0",
                 "repository/branches": "3", "repository/tags": "2", "members/all": "6"}
        for k, v in total.items():
            if f"/{k}?" in url:
                return [{}], Hdr({"X-Total": v})
        return [], Hdr()
    monkeypatch.setattr(fc, "_get", fake_get)
    c = fc.gitlab_counts("grp/proj", headers={}, api_base="https://gitlab.com")
    assert (c["issues"], c["prs"], c["labels"], c["milestones"], c["releases"]) == (5, 9, 4, 1, 0)
    assert (c["branches"], c["tags"], c["commits"], c["collaborators"]) == (3, 2, 88, 6)
    assert c["default_branch"] == "develop"


# ---------------------------------------------------------------- refs

def test_ls_remote_refs_parsed_and_filtered():
    out = ("a" * 40 + "\trefs/heads/main\n" + "b" * 40 + "\trefs/tags/v1\n" + "c" * 40 + "\trefs/pull/7/head\n"
           + "d" * 40 + "\trefs/pull/7/merge\n" + "e" * 40 + "\trefs/merge-requests/3/head\n"
           + "f" * 40 + "\trefs/pipelines/9\n")

    class P:
        returncode, stdout, stderr = 0, out, ""
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"], captured["env"] = cmd, kw["env"]
        return P()
    refs = fc.ls_remote_refs("https://github.com/acme/widgets.git", "x-access-token", "SECRETTOKEN", run=fake_run)
    assert set(refs) == {"refs/heads/main", "refs/tags/v1", "refs/pull/7/head", "refs/merge-requests/3/head"}
    assert "SECRETTOKEN" not in " ".join(captured["cmd"])           # token never on the command line
    assert any("Authorization: Basic" in v for k, v in captured["env"].items() if k.startswith("GIT_CONFIG_VALUE"))


def test_ls_remote_failure_raises():
    class P:
        returncode, stdout, stderr = 128, "", "fatal: could not read Username"
    with pytest.raises(mig.MigrationError):
        fc.ls_remote_refs("https://x/y.git", "u", "t", run=lambda *a, **k: P())


# ---------------------------------------------------------------- archive listing

def _tar(tmp_path):
    p = tmp_path / "a.tar.gz"
    with tarfile.open(p, "w:gz") as t:
        for name, data in {"./schema.json": b"{}", "./pull_requests_000001.json": b"[1]",
                           "./repositories/o/r.git/HEAD": b"ref", "./repositories/o/r.git/objects/ab/cd": b"xxxx",
                           "./repositories/o/r.git/objects/pack/p.pack": b"yyyyyy"}.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return p


def test_list_archive_aggregates_git_objects(tmp_path):
    files, ocount, obytes = fc.list_archive(_tar(tmp_path))
    assert files == {"schema.json": 2, "pull_requests_000001.json": 3, "repositories/o/r.git/HEAD": 3}
    assert (ocount, obytes) == (2, 10)


# ---------------------------------------------------------------- csv

def test_rows_roundtrip_and_header(tmp_path):
    rows = [fc.row("o/r", "meta", "provider", "github"), fc.row("o/r", "counts_before", "prs", 3),
            fc.row("o/r", "counts_before", "collaborators", "")]
    p = tmp_path / "x.for-check.csv"
    fc.write_csv(p, rows)
    assert p.read_text().splitlines()[0] == "repo,section,key,value"
    got = list(csv.DictReader(p.open()))
    assert got[1] == {"repo": "o/r", "section": "counts_before", "key": "prs", "value": "3"}
    assert got[2]["value"] == ""


def test_for_check_path_beside_archive():
    assert fc.for_check_path(Path("/o/acme-widgets-123.tar.gz")).name == "acme-widgets-123.for-check.csv"


# ---------------------------------------------------------------- runner integration

class FakeGH:
    TERMINAL_OK = "exported"

    def __init__(self, *a, **kw):
        self.scope, self.org = kw.get("scope", "user"), kw.get("org", "")
        self.api_base, self.api_version = "https://api.github.com", "2022-11-28"
        self.lock_repositories = False
        self.download_info = {}
        self.token = "tok"

    _headers = {"Authorization": "Bearer tok"}

    def whoami(self):
        return "me"

    def normalise(self, repo):
        return repo

    def start(self, repos):
        return "42"

    def wait(self, mid, **kw):
        return {"state": "exported"}

    def repositories(self, mid):
        return ["acme/widgets"]

    def download(self, mid, dest):
        with tarfile.open(dest, "w:gz") as t:
            ti = tarfile.TarInfo("schema.json")
            ti.size = 2
            t.addfile(ti, io.BytesIO(b"{}"))
        self.download_info["content_length"] = dest.stat().st_size
        return dest


def _spec():
    return runner.JobSpec(provider="github", scope="user", targets=["acme/widgets"], token="tok",
                          poll_interval=5, run_name="t")


def _wire(monkeypatch, tmp_path, counts=None):
    monkeypatch.setattr(runner, "GitHubMigration", FakeGH)
    monkeypatch.setattr(runner, "ensure_outputs", lambda comp, label: tmp_path)
    monkeypatch.setattr(fc, "github_counts", counts or (lambda o, r, **k: {
        "prs": 3, "issues": 1, "commits": 10, "default_branch": "main", "_errors": []}))
    monkeypatch.setattr(fc, "ls_remote_refs", lambda *a, **k: {"refs/heads/main": "a" * 40,
                                                                "refs/pull/1/head": "b" * 40})


def read_rows(p):
    return list(csv.DictReader(p.open()))


def by(rows, sec):
    return {r["key"]: r["value"] for r in rows if r["section"] == sec}


def test_runner_writes_for_check_next_to_archive(tmp_path, monkeypatch):
    _wire(monkeypatch, tmp_path)
    m = runner.run_job(_spec(), log=lambda s: None)
    assert m["failed"] == 0
    f = tmp_path / "acme-widgets-42.for-check.csv"
    assert f.exists()
    rows = read_rows(f)
    meta = by(rows, "meta")
    assert meta["provider"] == "github" and meta["migration_id"] == "42"
    assert meta["archive_bytes"] == str((tmp_path / "acme-widgets-42.tar.gz").stat().st_size)
    assert len(meta["archive_sha256"]) == 64
    assert meta["content_length"] == meta["archive_bytes"]
    assert by(rows, "counts_before")["prs"] == "3" and by(rows, "counts_after")["prs"] == "3"
    assert by(rows, "export_repo") == {"acme/widgets": ""} and by(rows, "requested_repo") == {"acme/widgets": ""}
    assert by(rows, "ref_before")["refs/heads/main"] == "a" * 40
    assert by(rows, "ref_after")["refs/pull/1/head"] == "b" * 40
    assert by(rows, "file")["schema.json"] == "2"
    assert all(r["repo"] == "acme/widgets" for r in rows)


def test_failure_while_collecting_never_fails_the_download(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("api down")
    _wire(monkeypatch, tmp_path, counts=boom)
    monkeypatch.setattr(fc, "ls_remote_refs", boom)
    m = runner.run_job(_spec(), log=lambda s: None)
    assert m["failed"] == 0 and m["ok"] == 1
    rows = read_rows(tmp_path / "acme-widgets-42.for-check.csv")
    assert any(r["section"] == "error" for r in rows)


def test_disabled_writes_no_file(tmp_path, monkeypatch):
    _wire(monkeypatch, tmp_path)
    spec = _spec()
    spec.for_check = False
    runner.run_job(spec, log=lambda s: None)
    assert not list(tmp_path.glob("*.for-check.csv"))
