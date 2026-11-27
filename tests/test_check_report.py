"""check-report.json: profiler parsing, name matching, the tolerance rule, the per-archive checks on synthetic
GitHub / GitLab / Bitbucket archives, and the runner wiring. No network; scc is stubbed (it need not be installed)."""

import csv
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import check_report as cr
import for_check as fc
import migration_api as mig
import runner

GIT_ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

FIELDS = ["org", "subgroup", "project_name", "repo", "merged_prs", "local_merges", "loc", "default_branch",
          "measured_branch", "total_commits"]


# ── helpers ──────────────────────────────────────────────────────────────────

def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True)


def make_repo(path: Path, files: dict, extra_branch: dict = None) -> Path:
    """A git repo with one commit on main (and optionally one more branch); returns its .git dir."""
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", str(path))
    for name, text in files.items():
        (path / name).write_text(text)
    git("add", ".", cwd=path)
    git("commit", "-qm", "one", cwd=path)
    if extra_branch:
        git("checkout", "-q", "-b", "dev", cwd=path)
        for name, text in extra_branch.items():
            (path / name).write_text(text)
        git("add", ".", cwd=path)
        git("commit", "-qm", "two", cwd=path)
        git("checkout", "-q", "main", cwd=path)
    return path / ".git"


def add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    tar.addfile(ti, io.BytesIO(data))


def add_json(tar, name, obj):
    add_bytes(tar, name, json.dumps(obj).encode())


def github_archive(tmp: Path, repos: dict, name="acme-widgets-42.tar.gz") -> Path:
    """repos: {"owner/repo": {"private": bool, "prs": [...], "files": {...}}} -> one migration archive."""
    archive = tmp / name
    repo_records, prs = [], []
    with tarfile.open(archive, "w:gz") as tar:
        for full, spec in repos.items():
            owner, repo = full.split("/")
            if spec.get("files") is not None:
                gitdir = make_repo(tmp / "src" / full, spec["files"], spec.get("dev"))
                tar.add(gitdir, arcname=f"repositories/{owner}/{repo}.git")
            repo_records.append({"type": "repository", "url": f"https://github.com/{full}", "name": repo,
                                 "private": spec.get("private", True), "default_branch": "main"})
            for n, pr in enumerate(spec.get("prs", []), 1):
                prs.append({"type": "pull_request", "url": f"https://github.com/{full}/pull/{n}",
                            "repository": f"https://github.com/{full}", "title": f"PR {n}", **pr})
        add_json(tar, "schema.json", {"version": "1.0.1"})
        add_json(tar, "repositories_000001.json", repo_records)
        add_json(tar, "pull_requests_000001.json", prs)
        # review / event files are never PR records, whatever they hold
        add_json(tar, "pull_request_reviews_000001.json", [{"pull_request": p["url"]} for p in prs] * 3)
        add_json(tar, "issue_events_000001.json", [{"x": 1}] * 50)
    return archive


def gitlab_archive(tmp: Path, mrs: list, visibility_level=10, files=None) -> Path:
    src = make_repo(tmp / "src-gl", files or {"a.py": "print(1)\n"}).parent
    bundle = tmp / "project.bundle"
    git("bundle", "create", str(bundle), "--all", cwd=src)
    archive = tmp / "grp-sub-proj-77.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        add_bytes(tar, "VERSION", b"0.2.4")
        tar.add(bundle, arcname="project.bundle")
        add_json(tar, "tree/project.json", {"description": "", "visibility_level": visibility_level})
        add_bytes(tar, "tree/project/merge_requests.ndjson", "\n".join(json.dumps(m) for m in mrs).encode())
        add_bytes(tar, "tree/project/issues.ndjson", b'{"iid": 1, "title": "not a PR"}\n')
    return archive


def bitbucket_archive(tmp: Path, prs: list, private=True, files=None) -> Path:
    gitdir = make_repo(tmp / "src-bb" / "repo", files or {"a.py": "x = 1\n"})
    archive = tmp / "ws-repo.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(gitdir, arcname="repo.git")
        add_json(tar, "repositories_000001.json", [{"full_name": "ws/repo", "is_private": private,
                                                    "mainbranch": {"name": "main"}}])
        add_json(tar, "pull_requests_000001.json", prs)
        # the activity feed: ~5 entries per PR, which must never be counted as PRs
        add_json(tar, "raw/pullrequest_activity.json", [{"pull_request": {"id": p["id"]}} for p in prs] * 5)
        add_json(tar, "issue_events_000001.json", [{"id": i} for i in range(40)])
    return archive


def write_profiler(path: Path, rows: list) -> Path:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS + ["error"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS + ["error"]})
    return path


def report(tmp, provider="github", rows=(), loc=None, **kw):
    return cr.CheckReport(tmp, provider, rows=list(rows), profiler_file="summary.csv" if rows else "",
                          loc_fn=loc or (lambda tree: count_lines(tree)), **kw)


def count_lines(tree: Path) -> int:
    """A stand-in for scc: every non-blank line of every file in the checkout."""
    return sum(1 for p in tree.rglob("*") if p.is_file() for line in p.read_text().splitlines() if line.strip())


# ── profiler parsing ─────────────────────────────────────────────────────────

def test_profiler_csv_with_bom_and_mixed_case_headers(tmp_path):
    p = tmp_path / "summary.csv"
    p.write_bytes("﻿Org,Subgroup,Repo,Merged_PRs,LOC\nacme,,widgets,10,1000\n,,,,\n".encode())
    rows = cr.load_profiler(p)
    assert rows == [{"org": "acme", "subgroup": "", "repo": "widgets", "merged_prs": "10", "loc": "1000"}]


def test_profiler_zip_reads_summary_csv_without_extracting(tmp_path):
    z = tmp_path / "profiler-run.zip"
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("../summary.csv", "org,repo\nevil,escape\n")          # path traversal: ignored
        zf.writestr("run-1/api/x/summary.csv", "org,repo\ndeep,one\n")   # deeper than the run's own
        zf.writestr("run-1/summary.csv", "org,repo,loc\nacme,widgets,5\n")
        zf.writestr("run-1/git/acme__widgets/scc.json", "{}")
    rows = cr.load_profiler(z)
    assert [r["repo"] for r in rows] == ["widgets"]
    assert not (tmp_path / "run-1").exists() and not (tmp_path.parent / "summary.csv").exists()


def test_profiler_zip_bomb_and_missing_summary_are_refused(tmp_path):
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("run/summary.csv", "org,repo\n" + "a" * 5_000_000)
    with pytest.raises(mig.MigrationError, match="zip bomb"):
        cr.load_profiler(bomb)
    empty = tmp_path / "other.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(mig.MigrationError, match="no summary.csv"):
        cr.load_profiler(empty)


def test_profiler_xlsx_without_openpyxl_says_so(tmp_path, monkeypatch):
    p = tmp_path / "summary.xlsx"
    p.write_bytes(b"PK\x03\x04 not really")
    monkeypatch.setitem(sys.modules, "openpyxl", None)          # import openpyxl -> ImportError
    with pytest.raises(mig.MigrationError, match="openpyxl"):
        cr.load_profiler(p)


def test_profiler_xlsx_summary_sheet(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.title = "notes"
    ws = wb.create_sheet("summary")
    ws.append(["org", "subgroup", "repo", "loc", "merged_prs"])
    ws.append(["acme", None, "widgets", 1200, 4])
    p = tmp_path / "summary.xlsx"
    wb.save(p)
    assert cr.load_profiler(p) == [{"org": "acme", "subgroup": "", "repo": "widgets", "loc": "1200",
                                    "merged_prs": "4"}]


def test_concurrent_checks_all_land_in_the_report(tmp_path):
    import threading
    archives = []
    for i in range(6):
        d = tmp_path / f"a{i}"
        d.mkdir()
        archives.append(github_archive(d, {f"acme/r{i}": {"files": {"a.py": "1\n"}}}, name=f"r{i}.tar.gz"))
    rep = report(tmp_path)
    threads = [threading.Thread(target=rep.check_archive, args=(a, [f"acme/r{i}"])) for i, a in enumerate(archives)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads((tmp_path / cr.REPORT_NAME).read_text())
    assert sorted(e["repo"] for e in data["repos"]) == [f"acme/r{i}" for i in range(6)]
    assert all(e["valid"] for e in data["repos"])


def test_profiler_without_repo_column_or_wrong_type_is_refused(tmp_path):
    bad = tmp_path / "summary.csv"
    bad.write_text("name,loc\nx,1\n")
    with pytest.raises(mig.MigrationError, match="'repo' column"):
        cr.load_profiler(bad)
    other = tmp_path / "summary.json"
    other.write_text("{}")
    with pytest.raises(mig.MigrationError, match="summary.csv"):
        cr.load_profiler(other)


# ── autoselect ───────────────────────────────────────────────────────────────

ROWS = [{"org": "acme", "subgroup": "", "repo": "widgets"},
        {"org": "grp", "subgroup": "sub/inner", "repo": "proj"},
        {"org": "ws", "subgroup": "Project Name", "repo": "repo"},
        {"org": "ACME", "subgroup": "", "repo": "Widgets"}]       # a duplicate in another case


def test_targets_from_profiler_per_provider():
    assert cr.targets_from_profiler(ROWS, "github") == ["acme/widgets", "grp/proj", "ws/repo"]
    assert cr.targets_from_profiler(ROWS, "gitlab") == ["acme/widgets", "grp/sub/inner/proj", "ws/Project Name/repo"]
    # Bitbucket's subgroup is the project *name*, never part of the path
    assert cr.targets_from_profiler(ROWS, "bitbucket") == ["acme/widgets", "grp/proj", "ws/repo"]
    assert cr.targets_from_profiler(ROWS, "github", scope="org", org="acme") == ["widgets"]


def test_runner_autoselects_profiler_repos_when_no_targets(tmp_path):
    p = write_profiler(tmp_path / "summary.csv", [{"org": "acme", "repo": "a"}, {"org": "acme", "repo": "b"}])
    spec = runner.JobSpec(provider="github", token="t", profiler_path=str(p))
    spec.validate()
    assert spec.targets == ["acme/a", "acme/b"]
    explicit = runner.JobSpec(provider="github", token="t", profiler_path=str(p), targets=["acme/c"])
    explicit.validate()
    assert explicit.targets == ["acme/c"]                 # explicit targets still win


def test_spec_validation_of_profiler_and_tolerances(tmp_path):
    with pytest.raises(mig.MigrationError, match="not found"):
        runner.JobSpec(provider="github", token="t", targets=["a/b"], profiler_path=str(tmp_path / "x.csv")).validate()
    with pytest.raises(mig.MigrationError, match="between 0 and 100"):
        runner.JobSpec(provider="github", token="t", targets=["a/b"], loc_tolerance_pct=101).validate()
    with pytest.raises(mig.MigrationError, match="between 0 and 100"):
        runner.JobSpec(provider="github", token="t", targets=["a/b"], pr_tolerance_pct=-1).validate()
    with pytest.raises(mig.MigrationError, match="profiler upload"):
        runner.JobSpec.from_form({"targets": "a/b", "profiler_upload": "../../etc/passwd"})
    with pytest.raises(mig.MigrationError, match="number"):
        runner.JobSpec.from_form({"targets": "a/b", "loc_tolerance": "five"})
    spec = runner.JobSpec.from_form({"targets": "a/b", "loc_tolerance": "7.5"})
    assert spec.loc_tolerance_pct == 7.5 and spec.pr_tolerance_pct == 5.0 and spec.profiler_path == ""


# ── name matching ────────────────────────────────────────────────────────────

def test_normalise_strips_suffixes_and_known_id_only():
    assert cr.normalise("https://github.com/Acme/Widgets.git") == "acme/widgets"
    assert cr.normalise("acme-widgets-42.tar.gz", ids=["42"]) == "acme-widgets"
    assert cr.normalise("acme-widgets-2.tar.gz") == "acme-widgets-2"          # an unknown number is the name


def test_match_exact_tiers():
    rows = [{"org": "acme", "repo": "widgets"}, {"org": "grp", "subgroup": "a/b", "repo": "proj"},
            {"org": "other", "repo": "solo"}]
    m = cr.match_repos(["Acme/Widgets", "grp/a/b/proj", "someone/solo", "acme/widgets-v2"], rows, "gitlab")
    assert m["Acme/Widgets"] == {"row": 0, "by": "path", "reason": ""}
    assert m["grp/a/b/proj"]["row"] == 1 and m["grp/a/b/proj"]["by"] == "path"
    assert m["someone/solo"]["row"] == 2 and m["someone/solo"]["by"] == "name"
    assert m["acme/widgets-v2"]["row"] is None                                # never fuzzy


def test_match_owner_repo_tier_for_a_subgroup_the_profiler_left_out():
    m = cr.match_repos(["grp/sub/proj"], [{"org": "grp", "repo": "proj"}], "gitlab")
    assert m["grp/sub/proj"]["row"] == 0 and m["grp/sub/proj"]["by"] == "owner/repo"


def test_bare_name_collisions_are_left_unmatched():
    rows = [{"org": "a", "repo": "api"}, {"org": "b", "repo": "api"}]
    m = cr.match_repos(["c/api"], rows, "github")
    assert m["c/api"]["row"] is None and "ambiguous" in m["c/api"]["reason"]
    # two repos of the run sharing a name: neither takes the single profiler row by name
    m = cr.match_repos(["x/tool", "y/tool"], [{"org": "z", "repo": "tool"}], "github")
    assert m["x/tool"]["row"] is None and m["y/tool"]["row"] is None
    # an exact path still matches even when the bare name is shared
    m = cr.match_repos(["a/api", "c/api"], rows, "github")
    assert m["a/api"]["row"] == 0 and m["c/api"]["row"] is None


# ── tolerance rule ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("migration, profiler, ok", [
    (1200, 1000, True),      # profiler lower than the migration: OK
    (1000, 1000, True),
    (950, 1000, True),       # exactly 5% below: OK
    (949, 1000, False),      # 5.1% below: mismatch
    (96, 100, True),
    (94.9, 100, False),
])
def test_tolerance_rule(migration, profiler, ok):
    r = cr.compare(migration, profiler, 5)
    assert r["ok"] is ok
    assert r["diff_pct"] == round((migration - profiler) / profiler * 100, 2)


@pytest.mark.parametrize("profiler", [0, "0", "", None])
def test_profiler_zero_or_blank_is_not_comparable(profiler):
    r = cr.compare(10, profiler, 5)
    assert r["ok"] is None and r["diff_pct"] is None and "not comparable" in r["reason"]


def test_tolerance_zero_and_hundred():
    assert cr.compare(999, 1000, 0)["ok"] is False
    assert cr.compare(0, 1000, 100)["ok"] is True


# ── per-archive checks ───────────────────────────────────────────────────────

def test_github_single_archive_reports_each_repo(tmp_path):
    archive = github_archive(tmp_path, {
        "acme/widgets": {"private": True, "files": {"a.py": "1\n2\n3\n"},
                         "prs": [{"merged_at": "2024-01-01T00:00:00Z"}, {"merged_at": None}, {"merged_at": None}]},
        "acme/gadgets": {"private": False, "files": {"b.py": "1\n"}, "prs": [{"merged_at": "2024-01-02"}]},
    })
    rows = [{"org": "acme", "repo": "widgets", "loc": "3", "merged_prs": "1", "measured_branch": "main"},
            {"org": "acme", "repo": "gadgets", "loc": "100", "merged_prs": "1"}]
    rep = report(tmp_path, rows=rows, expected=["acme/widgets", "acme/gadgets"])
    fc_info = {"repos": [{"label": "acme/widgets", "after": {"counts": {"prs": 3}}},
                         {"label": "acme/gadgets", "after": {"counts": {"prs": 2}}}]}
    entries = {e["repo"]: e for e in rep.check_archive(archive, ["acme/widgets", "acme/gadgets"], fc_info)}
    w, g = entries["acme/widgets"], entries["acme/gadgets"]
    assert w["valid"] is True and w["visibility"] == "private" and w["visibility_source"] == "archive"
    assert w["prs"] == {"source_total": 3, "in_archive": 3, "merged": 1, "with_metadata": 3, "count_match": True,
                        "files": ["pull_requests_000001.json"]}
    assert w["loc"]["migration"] == 3 and w["loc"]["ok"] is True and w["loc"]["branch"] == "main"
    assert w["merged_prs"]["ok"] is True and w["mismatch_reasons"] == []
    assert g["visibility"] == "public" and g["prs"]["in_archive"] == 1 and g["prs"]["count_match"] is False
    assert g["loc"]["ok"] is False and g["mismatch_reasons"] == ["loc 99.0% below profiler (1 vs 100)"]
    data = json.loads((tmp_path / cr.REPORT_NAME).read_text())
    assert list(data)[0] == "mismatch" and data["mismatch"] == ["acme/gadgets"]
    assert not list(tmp_path.glob(".check-*")) and not (tmp_path / (cr.REPORT_NAME + ".part")).exists()


def test_loc_is_measured_on_the_profilers_measured_branch(tmp_path):
    archive = github_archive(tmp_path, {"acme/widgets": {"files": {"a.py": "1\n"}, "dev": {"b.py": "1\n2\n3\n4\n"}}})
    rows = [{"org": "acme", "repo": "widgets", "loc": "5", "measured_branch": "dev", "default_branch": "main"}]
    e = report(tmp_path, rows=rows).check_archive(archive, ["acme/widgets"])[0]
    assert e["loc"]["branch"] == "dev" and e["loc"]["migration"] == 5 and e["loc"]["ok"] is True


def test_missing_branch_falls_back_and_says_so(tmp_path):
    archive = github_archive(tmp_path, {"acme/widgets": {"files": {"a.py": "1\n"}}})
    rows = [{"org": "acme", "repo": "widgets", "loc": "1", "measured_branch": "gone", "default_branch": "--evil"}]
    e = report(tmp_path, rows=rows).check_archive(archive, ["acme/widgets"])[0]
    assert e["valid"] is True and e["loc"]["branch"] == "main"
    assert any("'gone' is not in the archive" in n for n in e["notes"])


def test_gitlab_export_counts_merge_requests_and_reads_visibility_level(tmp_path):
    mrs = [{"iid": 1, "title": "a", "state": "merged", "merged_at": "x"},
           {"iid": 2, "title": "b", "state": "opened"},
           {"iid": 3, "title": "c", "state": "merged", "metrics": {"merged_at": "y"}},
           {"iid": 4, "title": "", "state": "closed"}]                              # no title: no metadata
    archive = gitlab_archive(tmp_path, mrs, visibility_level=10)
    rows = [{"org": "grp", "subgroup": "sub", "repo": "proj", "loc": "1", "merged_prs": "", "local_merges": "2"}]
    e = report(tmp_path, "gitlab", rows=rows).check_archive(archive, ["grp/sub/proj"])[0]
    assert e["valid"] is True and e["visibility"] == "internal"
    assert e["prs"]["in_archive"] == 4 and e["prs"]["merged"] == 2 and e["prs"]["with_metadata"] == 3
    assert e["prs"]["files"] == ["tree/project/merge_requests.ndjson"]
    assert e["merged_prs"]["profiler_field"] == "local_merges" and e["merged_prs"]["ok"] is True
    assert any("local_merges" in n for n in e["notes"])
    assert e["profiler_repo"] == "grp/sub/proj" and e["loc"]["migration"] == 1


@pytest.mark.parametrize("level, expected", [(0, "private"), (10, "internal"), (20, "public")])
def test_gitlab_visibility_levels(level, expected):
    assert cr.visibility_of({"visibility_level": level}) == expected


def test_visibility_variants():
    assert cr.visibility_of({"visibility": "Internal"}) == "internal"
    assert cr.visibility_of({"private": False}) == "public"
    assert cr.visibility_of({"is_private": True}) == "private"
    assert cr.visibility_of({}) is None and cr.visibility_of({"name": "x"}) is None


def test_bitbucket_mirror_ignores_the_activity_feed(tmp_path):
    prs = [{"id": i, "title": f"t{i}", "state": "MERGED" if i < 3 else "OPEN"} for i in range(1, 6)]
    archive = bitbucket_archive(tmp_path, prs, private=False)
    rows = [{"org": "ws", "subgroup": "Proj", "repo": "repo", "merged_prs": "2", "loc": "1"}]
    e = report(tmp_path, "bitbucket", rows=rows).check_archive(archive, ["ws/repo"],
                                                              {"repos": [{"label": "ws/repo",
                                                                          "before": {"counts": {"prs": 5}}}]})[0]
    assert e["prs"]["in_archive"] == 5 and e["prs"]["merged"] == 2 and e["prs"]["count_match"] is True
    assert e["visibility"] == "public" and e["valid"] is True and e["merged_prs"]["ok"] is True


def test_bitbucket_extras_fill_visibility_and_prs_in_the_final_pass(tmp_path):
    gitdir = make_repo(tmp_path / "src" / "repo", {"a.py": "1\n"})
    archive = tmp_path / "ws-repo.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(gitdir, arcname="repo.git")                  # metadata failed: git only
    rep = report(tmp_path, "bitbucket")
    e = rep.check_archive(archive, ["ws/repo"])[0]
    assert e["visibility"] is None and e["prs"]["in_archive"] == 0
    extras = tmp_path / "extras" / "ws-repo"
    extras.mkdir(parents=True)
    (extras / "repo-metadata.json").write_text(json.dumps({"is_private": True}))
    (extras / "pullrequests.json").write_text(json.dumps([{"id": 1, "title": "a", "state": "MERGED"}]))
    (extras / "pullrequest_activity.json").write_text(json.dumps([{"x": 1}] * 9))
    data = rep.finish(tmp_path / "extras")
    e = data["repos"][0]
    assert e["visibility"] == "private" and e["visibility_source"] == "extras repo-metadata.json"
    assert e["prs"]["in_archive"] == 1 and e["prs"]["merged"] == 1 and data["complete"] is True


def test_visibility_falls_back_to_the_source_snapshot(tmp_path):
    gitdir = make_repo(tmp_path / "src" / "repo", {"a.py": "1\n"})
    archive = tmp_path / "ws-repo.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(gitdir, arcname="repo.git")
    e = report(tmp_path, "bitbucket").check_archive(
        archive, ["ws/repo"], {"repos": [{"label": "ws/repo", "after": {"counts": {"visibility": "private"}}}]})[0]
    assert e["visibility"] == "private" and e["visibility_source"] == "source snapshot"


def test_invalid_archives_say_why(tmp_path):
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"this is not a tarball")
    rep = report(tmp_path)
    e = rep.check_archive(junk, ["acme/junk"])[0]
    assert e["valid"] is False and "does not open" in e["valid_reason"]
    assert e["mismatch_reasons"][0].startswith("invalid:")
    no_git = github_archive(tmp_path, {"acme/meta": {"files": None, "prs": [{"merged_at": None}]}}, name="m.tar.gz")
    e = rep.check_archive(no_git, ["acme/meta"])[0]
    assert e["valid"] is False and "no git data" in e["valid_reason"] and e["prs"]["in_archive"] == 1
    data = json.loads((tmp_path / cr.REPORT_NAME).read_text())
    assert data["mismatch"] == ["acme/junk", "acme/meta"]            # invalid -> mismatch, even with no profiler
    assert data["not_in_profiler"] == []


def test_the_archives_git_config_and_hooks_never_run(tmp_path):
    marker = tmp_path / "PWNED"
    gitdir = make_repo(tmp_path / "src" / "acme" / "widgets", {"a.py": "1\n", ".gitattributes": "* filter=evil\n"})
    with (gitdir / "config").open("a") as fh:
        fh.write(f'[core]\n\tfsmonitor = "touch {marker}"\n[filter "evil"]\n\tsmudge = "touch {marker}; cat"\n'
                 f'\trequired = true\n')
    hook = gitdir / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(gitdir, arcname="repositories/acme/widgets.git")
    e = report(tmp_path, rows=[{"org": "acme", "repo": "widgets", "loc": "1"}]).check_archive(archive, ["acme/widgets"])[0]
    assert e["valid"] is True and e["loc"]["migration"] == 2
    assert not marker.exists()


def test_scc_missing_is_null_not_a_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(cr.shutil, "which", lambda name: None)
    archive = github_archive(tmp_path, {"acme/widgets": {"files": {"a.py": "1\n"}}})
    rep = cr.CheckReport(tmp_path, "github", rows=[{"org": "acme", "repo": "widgets", "loc": "1000"}])
    e = rep.check_archive(archive, ["acme/widgets"])[0]
    assert e["loc"]["migration"] is None and e["loc"]["ok"] is None and e["loc"]["reason"] == "scc not installed"
    assert e["mismatch_reasons"] == []


def test_report_shape_lists_first_and_final_pass(tmp_path):
    archive = github_archive(tmp_path, {"acme/widgets": {"files": {"a.py": "1\n"}, "prs": [{"merged_at": "x"}]}})
    rows = [{"org": "acme", "repo": "widgets", "loc": "1", "merged_prs": "10"},
            {"org": "acme", "repo": "never", "loc": "5"},
            {"org": "acme", "repo": "broken", "loc": "5"}]
    rep = report(tmp_path, rows=rows, expected=["acme/widgets", "acme/broken", "acme/extra"])
    rep.check_archive(archive, ["acme/widgets"])
    rep.record_failure(["acme/broken"], "HTTP 404 GET /repos/acme/broken")
    mid = json.loads((tmp_path / cr.REPORT_NAME).read_text())
    assert list(mid)[:3] == ["mismatch", "not_migrated", "not_in_profiler"]
    assert mid["complete"] is False and mid["not_migrated"] == ["acme/broken"]     # pending ones are not listed yet
    data = rep.finish()
    assert data["mismatch"] == ["acme/widgets"] and data["complete"] is True
    assert sorted(data["not_migrated"]) == ["acme/broken", "acme/never"]
    assert data["tolerances"] == {"loc_pct": 5.0, "pr_pct": 5.0} and data["profiler_file"] == "summary.csv"
    e = data["repos"][0]
    assert e["merged_prs"] == {"migration": 1, "profiler": 10, "profiler_field": "merged_prs", "diff_pct": -90.0,
                               "ok": False, "reason": ""}
    assert e["mismatch_reasons"] == ["merged PRs 90.0% below profiler merged_prs (1 vs 10)"]
    summary = cr.summary(tmp_path / cr.REPORT_NAME)
    assert summary["mismatch"] == ["acme/widgets"] and summary["reasons"]["acme/widgets"]


def test_archive_repo_not_in_profiler_is_listed(tmp_path):
    archive = github_archive(tmp_path, {"acme/widgets": {"files": {"a.py": "1\n"}}})
    rep = report(tmp_path, rows=[{"org": "acme", "repo": "other", "loc": "1"}])
    rep.check_archive(archive, ["acme/widgets"])
    data = rep.finish()
    assert data["not_in_profiler"] == ["acme/widgets"] and data["not_migrated"] == ["acme/other"]
    assert data["repos"][0]["profiler_match_reason"] == "no profiler row with this name"


def test_a_check_exception_is_recorded_not_raised(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("surprise")
    monkeypatch.setattr(cr, "scan_archive", boom)
    archive = github_archive(tmp_path, {"acme/widgets": {"files": {"a.py": "1\n"}}})
    e = report(tmp_path).check_archive(archive, ["acme/widgets"])[0]
    assert e["valid"] is None and "RuntimeError: surprise" in e["errors"][0]


# ── runner wiring ────────────────────────────────────────────────────────────

class FakeGH:
    """A GitHub migration whose archive is a real synthetic one; `fail` repos 404."""
    TERMINAL_OK = "exported"
    fail = set()
    src = None

    def __init__(self, *a, **kw):
        self.scope, self.org = kw.get("scope", "user"), kw.get("org", "")
        self.api_base, self.api_version = "https://api.github.com", "2022-11-28"
        self.lock_repositories = False
        self.download_info = {}
        self.batch = []

    _headers = {"Authorization": "Bearer tok"}

    def whoami(self):
        return "acme"

    def start(self, repos):
        if any(r.split("/")[-1] in self.fail for r in repos):
            raise mig.MigrationError("HTTP 404 POST /user/migrations")
        self.batch = repos
        return "42"

    def wait(self, mid, **kw):
        return {"state": "exported"}

    def repositories(self, mid):
        return self.batch

    def download(self, mid, dest):
        full = self.batch[0] if "/" in self.batch[0] else f"{self.org}/{self.batch[0]}"
        build = FakeGH.src / dest.name
        build.mkdir(parents=True, exist_ok=True)
        built = github_archive(build, {full: {"files": {"a.py": "1\n2\n"},
                                                                "prs": [{"merged_at": "x"}]}}, name=dest.name)
        dest.write_bytes(built.read_bytes())
        self.download_info["content_length"] = dest.stat().st_size
        return dest


def _wire(monkeypatch, tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    FakeGH.src = tmp_path / "build"
    FakeGH.fail = {"broken"}
    monkeypatch.setattr(runner, "GitHubMigration", FakeGH)
    monkeypatch.setattr(runner, "ensure_outputs", lambda comp, label: out)
    monkeypatch.setattr(fc, "github_counts", lambda o, r, **k: {"prs": 1, "visibility": "private", "_errors": []})
    monkeypatch.setattr(fc, "ls_remote_refs", lambda *a, **k: {})
    monkeypatch.setattr(cr, "scc_loc", count_lines)
    return out


def test_run_job_writes_the_report_and_autoselects(tmp_path, monkeypatch):
    out = _wire(monkeypatch, tmp_path)
    prof = write_profiler(tmp_path / "summary.csv", [
        {"org": "acme", "repo": "widgets", "loc": "2", "merged_prs": "1", "measured_branch": "main"},
        {"org": "acme", "repo": "broken", "loc": "9"}])
    spec = runner.JobSpec(provider="github", token="tok", run_name="t", profiler_path=str(prof))
    lines = []
    m = runner.run_job(spec, log=lines.append)
    assert spec.targets == ["acme/widgets", "acme/broken"]
    assert m["ok"] == 1 and m["archives_failed"] == 1 and m["check_report"] == cr.REPORT_NAME
    data = json.loads((out / cr.REPORT_NAME).read_text())
    assert data["complete"] is True and data["mismatch"] == [] and data["not_migrated"] == ["acme/broken"]
    e = data["repos"][0]
    assert e["repo"] == "acme/widgets" and e["valid"] is True and e["profiler_repo"] == "acme/widgets"
    assert e["prs"]["source_total"] == 1 and e["prs"]["count_match"] is True and e["visibility"] == "private"
    assert e["loc"]["migration"] == 2 and e["loc"]["ok"] is True
    assert (out / "profiler-summary.csv").is_file()                 # the run keeps its input
    assert any(l.startswith("  check: acme/widgets · valid") for l in lines)
    assert any("not migrated (1): acme/broken" in l for l in lines)


def test_a_check_failure_never_fails_the_run(tmp_path, monkeypatch):
    out = _wire(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("checker exploded")
    monkeypatch.setattr(cr.CheckReport, "check_archive", boom)
    monkeypatch.setattr(cr.CheckReport, "finish", boom)
    m = runner.run_job(runner.JobSpec(provider="github", token="tok", run_name="t", targets=["acme/widgets"]),
                       log=lambda s: None)
    assert m["ok"] == 1 and m["failed"] == 0
    assert list(out.glob("*.tar.gz"))


def test_no_profiler_still_checks_validity_and_prs(tmp_path, monkeypatch):
    out = _wire(monkeypatch, tmp_path)
    runner.run_job(runner.JobSpec(provider="github", token="tok", run_name="t", targets=["acme/widgets"]),
                   log=lambda s: None)
    data = json.loads((out / cr.REPORT_NAME).read_text())
    assert data["profiler_file"] is None and data["not_in_profiler"] == [] and data["mismatch"] == []
    e = data["repos"][0]
    assert e["valid"] is True and e["prs"]["in_archive"] == 1 and e["loc"]["migration"] is None


def test_resume_keeps_the_profiler_and_tolerances(tmp_path, monkeypatch):
    import jobstore
    prof = write_profiler(tmp_path / "summary.csv", [{"org": "acme", "repo": "a"}])
    spec = runner.JobSpec(provider="github", token="tok", targets=["acme/a"], profiler_path=str(prof),
                          loc_tolerance_pct=3, pr_tolerance_pct=8)
    job = {"spec": jobstore._public_spec(spec), "log": [], "manifest": {}, "state": "error"}
    monkeypatch.setattr(jobstore, "resume_plan", lambda j: {"targets": ["acme/a"], "known_ids": {}, "run_name": "r"})
    again = jobstore.spec_for_resume(job, token="tok")
    assert again.profiler_path == str(prof) and again.loc_tolerance_pct == 3 and again.pr_tolerance_pct == 8


# ── web UI ───────────────────────────────────────────────────────────────────

def test_upload_endpoint_returns_targets_and_rejects_junk(tmp_path, monkeypatch):
    import base64
    import app
    monkeypatch.setattr(app, "outputs_for", lambda comp, *parts: tmp_path.joinpath(*parts))
    client = app.app.test_client()
    body = "org,subgroup,repo,loc\nacme,,widgets,1\ngrp,sub,proj,2\n".encode()
    r = client.post("/api/profiler", json={"filename": "summary.csv", "data": base64.b64encode(body).decode()})
    d = r.get_json()
    assert r.status_code == 200 and d["rows"] == 2
    assert d["targets"]["github"] == ["acme/widgets", "grp/proj"]
    assert d["targets"]["gitlab"] == ["acme/widgets", "grp/sub/proj"]
    assert (tmp_path / ".profiler-uploads" / d["upload"]).is_file()
    r = client.post("/api/profiler", json={"filename": "x.exe", "data": "aGk="})
    assert r.status_code == 400
    r = client.post("/api/profiler", json={"filename": "summary.csv", "data": base64.b64encode(b"a,b\n1,2\n").decode()})
    assert r.status_code == 400 and not list((tmp_path / ".profiler-uploads").glob("*-summary.csv"))[1:]


def test_check_report_route_cannot_escape_the_run_folder(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "outputs_for", lambda comp, *parts: tmp_path.joinpath(*parts))
    run = tmp_path / "run-1"
    run.mkdir()
    (run / cr.REPORT_NAME).write_text(json.dumps({"mismatch": ["x"], "repos": []}))
    client = app.app.test_client()
    r = client.get("/api/check-report/run-1")
    assert r.status_code == 200 and r.get_json()["mismatch"] == ["x"]
    for bad in ("..", ".jobs", "%2e%2e", "run-2"):
        assert client.get(f"/api/check-report/{bad}").status_code == 404


def test_page_has_the_profiler_upload_and_the_mismatch_banner():
    import app
    html = app.app.test_client().get("/").get_data(as_text=True)
    for needle in ('id="profiler"', 'id="loc_tol"', 'id="pr_tol"', 'id="check-banner"', "/api/check-report/",
                   "profiler_upload", "Mismatch: "):
        assert needle in html
