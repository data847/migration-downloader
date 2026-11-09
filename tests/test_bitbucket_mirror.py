"""Bitbucket Cloud: each repo's full git history by `git clone --mirror` with the run's own token, packed as the repo's
archive next to its metadata. Offline: git is the real git, but the "remote" is a local bare repository, reached by
replacing supplementary.run_git's clone source."""

import base64
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

import bitbucket
import bundle
import jobstore
import runner
import supplementary
from errors import MigrationError


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    import datalabs_paths
    monkeypatch.setattr(datalabs_paths, "OUTPUTS_ROOT", tmp_path / "out")
    monkeypatch.setattr(jobstore, "JOBS_DIR", tmp_path / "out" / runner.COMPONENT / ".jobs")
    jobstore.JOBS_DIR.mkdir(parents=True)
    return tmp_path / "out" / runner.COMPONENT


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
                        "GIT_COMMITTER_EMAIL": "a@x", "HOME": str(cwd or "/tmp"), "PATH": "/usr/bin:/bin"})


@pytest.fixture
def remote(tmp_path):
    """A small repository with two branches and a tag, as a bare repo standing in for bitbucket.org."""
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    (work / "a.txt").write_text("1")
    git("add", ".", cwd=work)
    git("commit", "-qm", "first", cwd=work)
    git("checkout", "-qb", "feature", cwd=work)
    (work / "b.txt").write_text("2")
    git("add", ".", cwd=work)
    git("commit", "-qm", "second", cwd=work)
    git("tag", "v1", cwd=work)
    bare = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(work), str(bare))
    return bare


@pytest.fixture
def fake_git(monkeypatch, remote):
    """run_git with the network clone pointed at the local remote; records the URL and credentials it was given."""
    seen = []
    real = supplementary.run_git

    def run_git(args, *, env, cwd=None):
        seen.append({"args": list(args), "env": dict(env)})
        url = args[3]
        if "denied" in url:
            return subprocess.CompletedProcess(args, 128, "", "remote: Unauthorized\nfatal: Authentication failed for 'https://bitbucket.org/ws/denied.git/'")
        return real(args[:3] + [str(remote)] + args[4:], env={k: v for k, v in env.items() if not k.startswith("GIT_CONFIG")}, cwd=cwd)

    monkeypatch.setattr(supplementary, "run_git", run_git)
    return seen


def test_git_credentials_follow_the_token_kind():
    assert bitbucket.git_credentials("me@x.com:ATATT123") == ("x-bitbucket-api-token-auth", "ATATT123")
    assert bitbucket.git_credentials("ATCTT-access") == ("x-token-auth", "ATCTT-access")


def test_clone_url_is_the_web_host_of_the_api():
    assert bitbucket.clone_url("", "ws/repo") == "https://bitbucket.org/ws/repo.git"
    assert bitbucket.clone_url("https://api.bitbucket.org/2.0", "ws/my repo") == "https://bitbucket.org/ws/my%20repo.git"
    with pytest.raises(MigrationError):
        bitbucket.clone_url("", "justone")


def test_a_run_mirrors_each_repo_with_the_same_token_and_ships_it_with_its_metadata(outputs, fake_git):
    spec = runner.JobSpec(provider="bitbucket", token="me@x.com:ATATT-secret", targets=["ws/shop", "ws/denied"],
                          run_name="r1", for_check=False, verify_checksum=False)
    manifest = runner.run_job(spec, log=lambda s: None)
    run = outputs / "r1"
    good = [a for a in manifest["archives"] if a["target"] == "ws/shop"][0]
    bad = [a for a in manifest["archives"] if a["target"] == "ws/denied"][0]
    archive = run / "ws-shop.tar.gz"
    assert archive.is_file() and good["path"].endswith("ws-shop.tar.gz") and not good.get("error")
    with tarfile.open(archive) as t:
        names = t.getnames()
    assert "shop.git/HEAD" in names and any(n.startswith("shop.git/refs") or n == "shop.git/packed-refs" for n in names)
    # every branch and tag came along (a mirror, not a single-branch clone)
    unpacked = run.parent / "unpacked"
    with tarfile.open(archive) as t:
        t.extractall(unpacked, filter="data")
    refs = subprocess.run(["git", "--git-dir", str(unpacked / "shop.git"), "for-each-ref", "--format=%(refname)"],
                          capture_output=True, text=True, check=True).stdout.split()
    assert {"refs/heads/main", "refs/heads/feature", "refs/tags/v1"} <= set(refs)
    # the token went to git through its environment only, as the API token user with the token alone as password
    call = fake_git[0]
    assert call["args"][:3] == ["clone", "--mirror", "--"] and call["args"][3] == "https://bitbucket.org/ws/shop.git"
    assert "ATATT-secret" not in " ".join(call["args"])
    header = call["env"]["GIT_CONFIG_VALUE_0"]
    assert base64.b64decode(header.split()[-1]).decode() == "x-bitbucket-api-token-auth:ATATT-secret"
    # a repo git cannot read fails on its own, with a hint, and leaves no half-written archive
    assert "Authentication failed" in bad["error"] and "read:repository" in bad["error"]
    assert not list(run.glob("*.part")) and not (run / ".mirror-work").exists()
    assert manifest["failed"] >= 1


def test_the_bundle_puts_the_mirror_and_its_extras_in_one_zip(outputs, fake_git, monkeypatch):
    monkeypatch.setitem(supplementary.REGISTRY["bitbucket"], "repo-metadata",
                        lambda ctx: [ctx.save_json("repo-metadata", {"full_name": "ws/shop"})])
    spec = runner.JobSpec(provider="bitbucket", token="tok", targets=["ws/shop"], run_name="r2", extras=["repo-metadata"],
                          for_check=False, verify_checksum=False)
    runner.run_job(spec, log=lambda s: None)
    plan = bundle.plan(outputs / "r2")
    arcs = sorted(a for _p, a in plan["ws-shop.zip"])
    assert arcs == ["extras/ws-shop/repo-metadata.json", "ws-shop.tar.gz"]


def test_a_resumed_run_does_not_clone_again(outputs, fake_git):
    spec = runner.JobSpec(provider="bitbucket", token="tok", targets=["ws/shop"], run_name="r3", for_check=False, verify_checksum=False)
    runner.run_job(spec, log=lambda s: None)
    calls = len(fake_git)
    spec.skip_done = True
    m = runner.run_job(spec, log=lambda s: None)
    assert len(fake_git) == calls and m["archives"][0]["skipped"]


def test_mirror_off_needs_extras():
    with pytest.raises(MigrationError, match="git mirror"):
        runner.JobSpec(provider="bitbucket", token="t", targets=["ws/r"], mirror=False).validate()
    runner.JobSpec(provider="bitbucket", token="t", targets=["ws/r"]).validate()       # the mirror alone is a run
    spec = runner.JobSpec.from_form({"provider": "bitbucket", "targets": "ws/r", "mirror": False})
    assert spec.mirror is False and runner.JobSpec.from_form({"provider": "bitbucket", "targets": "ws/r"}).mirror is True
