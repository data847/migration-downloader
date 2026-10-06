"""Rate limits, paging, redaction, safety checks, the nested zip and the issue summary.

Everything is offline: `conftest.py` makes any real connection attempt fail, and
the fakes below stand in for urllib's opener, `_json_page`, and the clock.
"""

import io
import json
import os
import tarfile
import time
import urllib.error
import zipfile
from pathlib import Path

import pytest

import bitbucket
import bundle
import migration_api as mig
import ratelimit
import redact
import runner
import safety
import supplementary
from errors import MigrationError


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeResp:
    def __init__(self, body=b"{}", status=200, headers=None):
        self._body, self.status, self.headers = body, status, dict(headers or {})

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, headers=None, body='{"message": "nope"}'):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", dict(headers or {}),
                                  io.BytesIO(body.encode()))


@pytest.fixture
def opener(monkeypatch):
    """Script what the next urllib calls do; returns (calls list, waits list)."""
    calls, waits = [], []

    def install(*script):
        def fake_open(req, timeout=60):
            calls.append((req.get_method(), req.full_url))
            item = script[min(len(calls) - 1, len(script) - 1)]
            if isinstance(item, Exception):
                raise item
            return item
        monkeypatch.setattr(mig._opener, "open", fake_open)
        return calls

    monkeypatch.setattr(ratelimit, "wait", lambda seconds, reason: waits.append(seconds))
    install.waits = waits
    return install


# ── rate limits and retries ──────────────────────────────────────────────────


class TestRateLimit:
    def test_429_waits_retry_after_then_succeeds(self, opener):
        calls = opener(http_error(429, {"Retry-After": "7"}), FakeResp(b'{"ok": 1}'))
        assert mig._json_request("https://api.github.com/x", headers={})[1] == {"ok": 1}
        assert len(calls) == 2 and opener.waits == [7.0]

    def test_primary_limit_waits_until_reset(self, opener):
        reset = str(int(time.time()) + 30)
        opener(http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}), FakeResp())
        mig._json_request("https://api.github.com/x", headers={})
        assert 25 <= opener.waits[0] <= 33

    def test_secondary_limit_in_body_backs_off(self, opener):
        opener(http_error(403, body='{"message": "You have exceeded a secondary rate limit."}'), FakeResp())
        mig._json_request("https://api.github.com/x", headers={})
        assert opener.waits == [ratelimit.SECONDARY_BACKOFF]

    def test_real_permission_403_is_not_retried_and_keeps_its_message(self, opener):
        calls = opener(http_error(403, body='{"message": "Resource not accessible by personal access token"}'))
        with pytest.raises(MigrationError) as err:
            mig._json_request("https://api.github.com/x", headers={})
        assert len(calls) == 1 and opener.waits == []
        assert "not accessible by personal access token" in str(err.value)

    def test_get_retries_server_errors_a_few_times_then_gives_up(self, opener):
        calls = opener(http_error(503))
        with pytest.raises(MigrationError):
            mig._json_request("https://api.github.com/x", headers={})
        assert len(calls) == ratelimit.TRANSIENT_ATTEMPTS + 1
        assert opener.waits == [2, 4, 8]

    def test_post_is_never_repeated_after_a_server_error(self, opener):
        calls = opener(http_error(502))
        with pytest.raises(MigrationError):
            mig._json_request("https://api.github.com/orgs/o/migrations", method="POST", headers={}, payload={})
        assert len(calls) == 1          # a repeat could start a second migration

    def test_post_does_wait_out_a_429(self, opener):
        calls = opener(http_error(429, {"Retry-After": "1"}), FakeResp())
        mig._json_request("https://api.github.com/x", method="POST", headers={}, payload={})
        assert len(calls) == 2

    def test_network_errors_retry_for_get_only(self, opener):
        calls = opener(urllib.error.URLError("reset"), FakeResp())
        mig._json_request("https://api.github.com/x", headers={})
        assert len(calls) == 2
        calls.clear()
        opener(urllib.error.URLError("reset"))
        with pytest.raises(MigrationError):
            mig._json_request("https://api.github.com/x", method="POST", headers={}, payload={})
        assert len(calls) == 1

    def test_gives_up_after_the_attempt_limit(self, opener):
        calls = opener(http_error(429, {"Retry-After": "1"}))
        with pytest.raises(MigrationError):
            mig._json_request("https://api.github.com/x", headers={})
        assert len(calls) == ratelimit.MAX_ATTEMPTS

    def test_cancelling_during_a_wait_stops_the_call(self, monkeypatch):
        monkeypatch.setattr(mig._opener, "open", lambda req, timeout=60: (_ for _ in ()).throw(
            http_error(429, {"Retry-After": "30"})))
        ratelimit.bind(lambda: True, None)
        with pytest.raises(MigrationError, match="cancelled"):
            mig._json_request("https://api.github.com/x", headers={})

    def test_waits_are_logged_through_the_bound_logger(self, monkeypatch):
        lines = []
        ratelimit.bind(None, lines.append)
        ratelimit.wait(2, "HTTP 429 from api.github.com")
        assert lines == ["    HTTP 429 from api.github.com; waiting 2s"]

    def test_pauses_before_the_call_that_would_use_the_last_request(self, opener):
        opener(FakeResp(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 20)}),
               FakeResp())
        mig._json_request("https://api.github.com/a", headers={})
        assert opener.waits == []
        mig._json_request("https://api.github.com/b", headers={})
        assert 15 <= opener.waits[0] <= 22

    def test_other_hosts_are_unaffected_by_one_hosts_budget(self, opener):
        opener(FakeResp(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 20)}),
               FakeResp())
        mig._json_request("https://api.github.com/a", headers={})
        mig._json_request("https://gitlab.com/b", headers={})
        assert opener.waits == []

    def test_only_http_urls_are_opened(self, opener):
        calls = opener(FakeResp())
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x"):
            with pytest.raises(MigrationError, match="refusing"):
                mig._json_request(url, headers={})
        assert calls == []


# ── paging ───────────────────────────────────────────────────────────────────


class TestPaging:
    def test_link_header_parsing(self):
        link = '<https://api.github.com/x?after=abc>; rel="next", <https://api.github.com/x?after=zzz>; rel="last"'
        assert mig.next_link(link) == "https://api.github.com/x?after=abc"
        assert mig.next_link('<https://a/x>; rel="prev"') is None
        assert mig.next_link(None) is None and mig.next_link("") is None

    def test_json_page_returns_the_next_url_and_headers(self, opener):
        opener(FakeResp(b'[1,2]', headers={"Link": '<https://api.github.com/x?page=2>; rel="next"', "X-A": "b"}))
        data, nxt, hdrs = mig._json_page("https://api.github.com/x", headers={})
        assert (data, nxt, hdrs["x-a"]) == ([1, 2], "https://api.github.com/x?page=2", "b")

    def _ctx(self, tmp_path, **kw):
        return supplementary.Ctx(provider="github", target="o/r", token="t", api_base="https://api.github.com",
                                 headers={}, out=tmp_path, log=lambda _m: None, **kw)

    def _serve(self, monkeypatch, pages):
        asked = []

        def fake_page(url, *, headers, timeout=60):
            asked.append(url)
            return pages[url]

        monkeypatch.setattr(supplementary, "_json_page", fake_page)
        return asked

    def test_cursor_paged_alerts_follow_the_servers_links_not_page_numbers(self, tmp_path, monkeypatch):
        base = "https://api.github.com/repos/o/r/dependabot/alerts"
        asked = self._serve(monkeypatch, {
            f"{base}?per_page=100": ([{"n": 1}, {"n": 2}], f"{base}?per_page=100&after=c1", {}),
            f"{base}?per_page=100&after=c1": ([{"n": 3}], f"{base}?per_page=100&after=c2", {}),
            f"{base}?per_page=100&after=c2": ([{"n": 4}], None, {}),
        })
        items = supplementary.paged_all(self._ctx(tmp_path, max_items=0), base)
        assert [i["n"] for i in items] == [1, 2, 3, 4]
        assert not any("page=" in u.replace("per_page=", "") for u in asked)     # never guesses page=N

    def test_zero_means_no_limit_and_a_limit_is_reported(self, tmp_path, monkeypatch):
        url = "https://api.github.com/x"
        self._serve(monkeypatch, {
            f"{url}?per_page=100": ([1, 2], f"{url}?p=2", {}),
            f"{url}?p=2": ([3, 4], f"{url}?p=3", {}),
            f"{url}?p=3": ([5], None, {}),
        })
        assert supplementary.paged_all(self._ctx(tmp_path, max_items=0), url) == [1, 2, 3, 4, 5]
        ctx = self._ctx(tmp_path, max_items=3)
        assert supplementary.paged_all(ctx, url) == [1, 2, 3]
        assert ctx.truncated == ["x limited to 3 items"]

    def test_a_listing_that_exactly_fits_is_not_reported_as_cut(self, tmp_path, monkeypatch):
        url = "https://api.github.com/x"
        self._serve(monkeypatch, {f"{url}?per_page=100": ([1, 2, 3], None, {})})
        ctx = self._ctx(tmp_path, max_items=3)
        assert supplementary.paged_all(ctx, url) == [1, 2, 3] and ctx.truncated == []

    def test_a_link_to_another_host_is_refused(self, tmp_path, monkeypatch):
        url = "https://api.github.com/x"
        self._serve(monkeypatch, {f"{url}?per_page=100": ([1], "https://evil.example.com/steal", {})})
        with pytest.raises(MigrationError, match="another host"):
            supplementary.paged_all(self._ctx(tmp_path, max_items=0), url)

    def test_workflow_runs_are_unwrapped_from_their_envelope(self, tmp_path, monkeypatch):
        url = "https://api.github.com/x"
        self._serve(monkeypatch, {f"{url}?per_page=100": ({"workflow_runs": [{"id": 1}]}, None, {})})
        assert supplementary.paged_all(self._ctx(tmp_path, max_items=0), url, key="workflow_runs") == [{"id": 1}]

    def test_discovery_paged_follows_links_without_a_cap(self, monkeypatch):
        url = "https://api.github.com/user/orgs"
        pages = {f"{url}?per_page=100": ([1] * 100, f"{url}?x=2", {}), f"{url}?x=2": ([2] * 50, None, {})}
        monkeypatch.setattr(mig, "_json_page", lambda u, *, headers, timeout=60: pages[u])
        assert len(mig.paged(url, headers={})) == 150

    def test_bitbucket_follows_next_on_its_own_host_only(self, tmp_path, monkeypatch):
        ctx = self._ctx(tmp_path, max_items=0)
        base = "https://api.bitbucket.org/2.0/repositories/ws/r/refs"
        monkeypatch.setattr(supplementary, "_json_page", lambda u, *, headers, timeout=60: (
            {"values": [1], "next": "https://evil.example.com/x"}, None, {}))
        with pytest.raises(MigrationError, match="another host"):
            bitbucket.cloud_paged(ctx, base)
        monkeypatch.setattr(supplementary, "_json_page", lambda u, *, headers, timeout=60: (
            ({"values": [1], "next": base + "?page=2"} if "page=2" not in u else {"values": [2]}), None, {}))
        assert bitbucket.cloud_paged(ctx, base) == [1, 2]

    def test_repos_page_reports_sso_hidden_orgs(self, monkeypatch):
        monkeypatch.setattr(mig, "_json_page", lambda u, *, headers, timeout=60: (
            [{"name": "a", "full_name": "o/a"}], None, {"x-github-sso": "partial-results; organizations=1,2"}))
        result = mig.GitHubMigration("t").repos_page(1, everything=True)
        assert result["notes"] == [mig.SSO_NOTE] and result["items"][0]["target"] == "o/a"
        assert result["has_more"] is False

    def test_has_more_comes_from_the_link_or_a_full_page(self, monkeypatch):
        monkeypatch.setattr(mig, "_json_page", lambda u, *, headers, timeout=60: (
            [{"name": "a", "full_name": "o/a"}], "https://api.github.com/user/repos?page=2", {"link": "x"}))
        assert mig.GitHubMigration("t").repos_page(1)["has_more"] is True
        monkeypatch.setattr(mig, "_json_page", lambda u, *, headers, timeout=60: (
            [{"name": str(i), "full_name": f"o/{i}"} for i in range(100)], None, {}))
        assert mig.GitHubMigration("t").repos_page(1)["has_more"] is True      # no Link header, full page


# ── redaction ────────────────────────────────────────────────────────────────


class TestRedaction:
    def test_fields_named_like_secrets_are_blanked(self):
        out = redact.scrub_obj({"secret": "hunter2", "password": "p", "api_key": "k", "token": "t",
                                "client_secret": "s", "name": "ok", "nested": {"private_key": "x"}})
        assert out["secret"] == out["password"] == out["api_key"] == out["token"] == redact.REDACTED
        assert out["client_secret"] == redact.REDACTED and out["nested"]["private_key"] == redact.REDACTED
        assert out["name"] == "ok"

    def test_metadata_that_merely_mentions_a_secret_is_kept(self):
        data = {"secret_type": "github_personal_access_token", "secret_type_display_name": "GitHub PAT",
                "token_scopes": ["repo"], "has_secret": True, "secret_scanning": {"status": "enabled"},
                "html_url": "https://x"}
        assert redact.scrub_obj(data) == data

    def test_a_secret_scanning_alert_never_keeps_the_leaked_value(self):
        alert = {"number": 1, "secret_type": "slack_api_token", "secret": "xoxb-12345678901-abcdefghijkl",
                 "state": "open"}
        out = redact.scrub_obj(alert)
        assert out["secret"] == redact.REDACTED and out["secret_type"] == "slack_api_token"

    def test_webhook_urls_are_secrets(self):
        text = ("https://hooks.slack.com/services/T0000/B0000/abcdefghijklmnop and "
                "https://discord.com/api/webhooks/123456/AbCdEf-gh_ij and https://u:pw@host/x")
        out = redact.scrub_secrets(text)
        assert "abcdefghijklmnop" not in out and "AbCdEf" not in out and "pw@" not in out
        assert "hooks.slack.com/services/<redacted>" in out

    def test_strings_inside_json_are_pattern_scrubbed(self):
        out = redact.scrub_obj({"config": {"url": "https://hooks.slack.com/services/T/B/zzzzzzzzzzzz"},
                                "list": ["ghp_" + "a" * 36]})
        assert "zzzzzzzzzzzz" not in json.dumps(out) and "ghp_" not in json.dumps(out)

    def test_private_key_blocks_are_removed(self):
        text = "x\n-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\ny"
        assert redact.scrub_secrets(text) == "x\n<redacted-private-key>\ny"

    def test_collected_json_is_scrubbed_before_it_is_written(self, tmp_path):
        ctx = supplementary.Ctx(provider="github", target="o/r", token="t", api_base="https://api.github.com",
                                headers={}, out=tmp_path, log=lambda _m: None)
        ctx.save_json("hooks", [{"config": {"secret": "s3cret", "url": "https://hooks.slack.com/services/T/B/qqqqqqqqqq"}}])
        written = (tmp_path / "hooks.json").read_text()
        assert "s3cret" not in written and "qqqqqqqqqq" not in written
        assert json.loads(written)[0]["config"]["secret"] == redact.REDACTED

    def test_downloaded_logs_are_scrubbed_in_place(self, tmp_path, monkeypatch):
        ctx = supplementary.Ctx(provider="gitlab", target="g/p", token="t", api_base="https://gitlab.com",
                                headers={}, out=tmp_path, log=lambda _m: None)

        def fake_download(url, dest, *, headers, log, timeout=120):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("deploy with token=abc123def456 now\nAuthorization: Bearer xyz\nfine line\n")
            return dest

        monkeypatch.setattr(supplementary, "_download", fake_download)
        supplementary.fetch_file(ctx, "https://gitlab.com/x", "job-traces/1.log", scrub="text")
        text = (tmp_path / "job-traces" / "1.log").read_text()
        assert "abc123def456" not in text and "xyz" not in text and "fine line" in text

    def test_log_zips_are_rewritten_with_secrets_removed(self, tmp_path):
        path = tmp_path / "logs.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("1_build.txt", "password=hunter2hunter2 ok")
            z.writestr("bin.dat", b"\x00\x01token=raw")
        assert redact.scrub_zip(path) is True
        with zipfile.ZipFile(path) as z:
            assert b"hunter2" not in z.read("1_build.txt") and b"ok" in z.read("1_build.txt")
            assert z.read("bin.dat") == b"\x00\x01token=raw"            # binary members untouched
        assert not list(tmp_path.glob("*.scrub"))

    def test_zip_bombs_and_bad_zips_are_left_alone(self, tmp_path, monkeypatch):
        path = tmp_path / "big.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("a.txt", "x" * 1000)
        monkeypatch.setattr(redact, "MAX_SCRUB_BYTES", 100)
        before = path.read_bytes()
        assert redact.scrub_zip(path) is False and path.read_bytes() == before
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip")
        assert redact.scrub_zip(bad) is False and bad.read_bytes() == b"not a zip"


# ── safety checks ────────────────────────────────────────────────────────────


class TestSafety:
    @pytest.mark.parametrize("name", ["owner/repo", "group/sub/project", "PROJ/my-repo", "12345", "a.b_c-d/e~f"])
    def test_ordinary_names_pass(self, name):
        assert safety.valid_target(name)

    @pytest.mark.parametrize("name", ["", "../etc/passwd", "a/../b", "a b/c", "a;rm -rf/c", "a\nb/c",
                                      "-flag/repo", "a//b", "/abs/path", "x" * 400, "a/b?c=1", "a/b#frag", "$(id)/x"])
    def test_hostile_names_fail(self, name):
        assert not safety.valid_target(name)

    def test_bad_targets_stop_the_run_before_anything_happens(self):
        for bad, why in (("evil/../../x", "owner/repo"), ("bad name/x", "not a valid repository name"),
                         ("o/r;rm -rf", "not a valid repository name")):
            with pytest.raises(MigrationError, match=why):
                runner.JobSpec(provider="github", token="t", targets=["ok/repo", bad]).validate()
        for provider in ("gitlab", "bitbucket"):
            with pytest.raises(MigrationError, match="not a valid repository name"):
                runner.JobSpec(provider=provider, token="t", targets=["g/p q"], extras=["refs"]).validate()
        runner.JobSpec(provider="github", token="t", targets=["https://github.com/o/r.git"]).validate()

    def test_safe_join_blocks_escapes(self, tmp_path):
        assert safety.safe_join(tmp_path, "a/b.json") == (tmp_path / "a" / "b.json").resolve()
        for bad in ("../x", "a/../../x", "/etc/passwd", "", "a\x00b"):
            with pytest.raises(MigrationError):
                safety.safe_join(tmp_path, bad)

    def test_safe_join_blocks_symlink_escapes(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        inside = tmp_path / "inside"
        inside.mkdir()
        (inside / "link").symlink_to(outside)
        with pytest.raises(MigrationError):
            safety.safe_join(inside, "link/stolen.txt")

    def test_a_hostile_file_name_cannot_leave_the_target_folder(self, tmp_path):
        ctx = supplementary.Ctx(provider="github", target="o/r", token="t", api_base="https://api.github.com",
                                headers={}, out=tmp_path / "t", log=lambda _m: None)
        with pytest.raises(MigrationError):
            ctx.save_json("../../escape", {})
        with pytest.raises(MigrationError):
            supplementary.fetch_file(ctx, "https://api.github.com/x", "../escape.bin")

    def test_origin_comparison(self):
        assert safety.same_origin("https://api.github.com/a", "https://API.github.com:443/b")
        assert not safety.same_origin("https://api.github.com/a", "http://api.github.com/a")
        assert not safety.same_origin("https://api.github.com/a", "https://api.github.com.evil.io/a")

    def test_low_disk_is_refused_with_numbers(self, tmp_path, monkeypatch):
        usage = type("U", (), {"free": 10 * 1024 * 1024})()
        monkeypatch.setattr(safety.shutil, "disk_usage", lambda _p: usage)
        with pytest.raises(MigrationError, match="only 10 MB free"):
            safety.check_disk(tmp_path / "not-yet", need_mb=512)
        safety.check_disk(tmp_path, need_mb=5)

    def test_a_run_refuses_to_start_on_a_full_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "run_dir", lambda spec: tmp_path)
        monkeypatch.setattr(runner, "check_disk", lambda *a, **k: (_ for _ in ()).throw(MigrationError("only 1 MB free")))
        spec = runner.JobSpec(provider="bitbucket", token="t", targets=["ws/r"], extras=["refs"])
        with pytest.raises(MigrationError, match="MB free"):
            runner.run_job(spec, log=lambda _m: None)


# ── delete-archive only after a verified download ────────────────────────────


class TestDeleteArchive:
    def _tarball(self, path):
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo("a.txt")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"abc"))
        return path

    def test_complete_download_check(self, tmp_path):
        good = self._tarball(tmp_path / "a.tar.gz")
        size = good.stat().st_size
        assert runner._download_is_complete(good, size)
        assert runner._download_is_complete(good, str(size))
        assert not runner._download_is_complete(good, size + 1)           # short
        assert not runner._download_is_complete(good, "")                 # server gave no length
        assert not runner._download_is_complete(good, None)
        junk = tmp_path / "junk.tar.gz"
        junk.write_bytes(b"x" * 10)
        assert not runner._download_is_complete(junk, 10)                 # right size, not a tarball
        assert not runner._download_is_complete(tmp_path / "missing", 5)

    def _client(self):
        class Client:
            deleted, unlocked = [], []

            def delete_archive(self, mid):
                self.deleted.append(mid)

            def unlock_repo(self, mid, repo):
                self.unlocked.append(repo)
        return Client()

    def test_archive_is_deleted_only_when_the_download_checks_out(self, tmp_path):
        good = self._tarball(tmp_path / "a.tar.gz")
        spec = runner.JobSpec(provider="github", token="t", targets=["o/r"], delete_archive=True)
        logs = []
        client = self._client()
        runner._github_cleanup(spec, client, "9", ["o/r"], logs.append, good, good.stat().st_size)
        assert client.deleted == ["9"]
        client = self._client()
        runner._github_cleanup(spec, client, "9", ["o/r"], logs.append, good, good.stat().st_size + 5)
        assert client.deleted == [] and any("not deleting" in line for line in logs)

    def test_unlocking_does_not_wait_for_verification(self, tmp_path):
        spec = runner.JobSpec(provider="github", token="t", targets=["o/r"], lock_repositories=True,
                              unlock_repos=True)
        client = self._client()
        runner._github_cleanup(spec, client, "9", ["o/r"], lambda _m: None, tmp_path / "missing", None)
        assert client.unlocked == ["o/r"]


# ── the nested download zip ──────────────────────────────────────────────────


def make_run(tmp_path):
    run = tmp_path / "run1"
    (run / "extras" / "acme-api").mkdir(parents=True)
    (run / "extras" / "acme-web").mkdir(parents=True)
    (run / "extras" / "ws-orphan").mkdir(parents=True)
    (run / "acme-api-1.tar.gz").write_bytes(b"A" * 50)
    (run / "acme-api-1.for-check.csv").write_text("repo,section,key,value\n")
    (run / "acme-web-2.tar.gz").write_bytes(b"W" * 40)
    (run / "extras" / "acme-api" / "hooks.json").write_text("[]")
    (run / "extras" / "acme-api" / "sub").mkdir()
    (run / "extras" / "acme-api" / "sub" / "log.log").write_text("log")
    (run / "extras" / "acme-web" / "releases.json").write_text("[]")
    (run / "extras" / "ws-orphan" / "refs.json").write_text("[]")
    (run / "partial.tar.gz.part").write_bytes(b"unfinished")
    (run / "manifest.json").write_text(json.dumps({"org": "", "archives": [
        {"target": "acme/api", "path": str(run / "acme-api-1.tar.gz")},
        {"target": "acme/web", "path": str(run / "acme-web-2.tar.gz")},
        {"target": "acme/failed", "path": ""}]}))
    return run


def read_nested(path):
    with zipfile.ZipFile(path) as outer:
        result = {}
        for name in outer.namelist():
            with zipfile.ZipFile(io.BytesIO(outer.read(name))) as inner:
                result[name] = sorted(inner.namelist())
        return result


class TestBundle:
    def test_one_zip_per_target_inside_the_final_zip(self, tmp_path):
        run = make_run(tmp_path)
        built = bundle.build(run, tmp_dir=tmp_path)
        try:
            assert read_nested(built) == {
                "acme-api.zip": ["acme-api-1.for-check.csv", "acme-api-1.tar.gz",
                                 "extras/acme-api/hooks.json", "extras/acme-api/sub/log.log"],
                "acme-web.zip": ["acme-web-2.tar.gz", "extras/acme-web/releases.json"],
                "ws-orphan.zip": ["extras/ws-orphan/refs.json"],
                "run-files.zip": ["manifest.json"],
            }
        finally:
            built.unlink()

    def test_unfinished_downloads_and_symlinks_are_left_out(self, tmp_path):
        run = make_run(tmp_path)
        secret = tmp_path / "secret.txt"
        secret.write_text("do not ship")
        (run / "extras" / "acme-api" / "evil.json").symlink_to(secret)
        (run / "link.txt").symlink_to(secret)
        names = {arc for entries in bundle.plan(run).values() for _p, arc in entries}
        assert not any(n.endswith(".part") for n in names)
        assert "extras/acme-api/evil.json" not in names and "link.txt" not in names

    def test_archives_are_stored_and_text_is_deflated(self, tmp_path):
        run = make_run(tmp_path)
        built = bundle.build(run, tmp_dir=tmp_path)
        try:
            with zipfile.ZipFile(built) as outer:
                assert all(i.compress_type == zipfile.ZIP_STORED for i in outer.infolist())
                with zipfile.ZipFile(io.BytesIO(outer.read("acme-api.zip"))) as inner:
                    kinds = {i.filename: i.compress_type for i in inner.infolist()}
            assert kinds["acme-api-1.tar.gz"] == zipfile.ZIP_STORED
            assert kinds["extras/acme-api/hooks.json"] == zipfile.ZIP_DEFLATED
        finally:
            built.unlink()

    def test_extras_for_a_bare_org_scope_name_attach_to_its_archive(self, tmp_path):
        run = tmp_path / "run2"
        (run / "extras" / "acme-api").mkdir(parents=True)
        (run / "extras" / "acme-api" / "hooks.json").write_text("[]")
        (run / "api-5.tar.gz").write_bytes(b"x")
        (run / "manifest.json").write_text(json.dumps({"org": "acme", "archives": [
            {"target": "api", "path": str(run / "api-5.tar.gz")}]}))
        assert sorted(a for _p, a in bundle.plan(run)["api.zip"]) == ["api-5.tar.gz", "extras/acme-api/hooks.json"]

    def test_extras_only_runs_still_download(self, tmp_path):
        run = tmp_path / "bb"
        (run / "extras" / "ws-repo").mkdir(parents=True)
        (run / "extras" / "ws-repo" / "refs.json").write_text("[]")
        built = bundle.build(run, tmp_dir=tmp_path)
        try:
            assert read_nested(built) == {"ws-repo.zip": ["extras/ws-repo/refs.json"]}
        finally:
            built.unlink()

    def test_an_empty_run_has_nothing_to_download(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(MigrationError, match="nothing to download"):
            bundle.build(tmp_path / "empty", tmp_dir=tmp_path)

    def test_a_failed_build_leaves_no_temp_files(self, tmp_path, monkeypatch):
        run = make_run(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setattr(bundle.zipfile.ZipFile, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(OSError):
            bundle.build(run, tmp_dir=scratch)
        assert list(scratch.iterdir()) == []

    def test_it_checks_disk_space_first(self, tmp_path, monkeypatch):
        run = make_run(tmp_path)
        monkeypatch.setattr(bundle, "check_disk", lambda *a, **k: (_ for _ in ()).throw(MigrationError("only 1 MB free")))
        with pytest.raises(MigrationError, match="MB free"):
            bundle.build(run, tmp_dir=tmp_path)


# ── issues ───────────────────────────────────────────────────────────────────


class TestIssues:
    def test_every_failure_kind_becomes_an_issue_with_a_hint(self):
        manifest = {
            "archives": [{"target": "lh2-tech/x", "error":
                          "HTTP 403 POST https://api.github.com/orgs/lh2-tech/migrations — Must be an owner"},
                         {"target": "ok/ok", "error": ""}],
            "export_jobs": [{"action": "export", "targets": ["P/r"], "error": "HTTP 401 POST https://bb/x"}],
            "extras": [{"target": "o/r", "extra": "hooks", "error": "HTTP 404 GET https://api.github.com/repos/o/r/hooks"},
                       {"target": "o/r", "extra": "actions", "error": "", "truncated": ["runs limited to 200 items"]},
                       {"target": "o/r", "extra": "releases", "error": ""}],
        }
        issues = runner.issues_from_manifest(manifest)
        assert [(i["kind"], i["step"]) for i in issues] == [
            ("archive", "export + download"), ("export-job", "export job (export)"),
            ("extra", "hooks"), ("limit", "actions")]
        assert "Owner" in issues[0]["hint"] and "token" in issues[1]["hint"].lower()
        assert "admin access" in issues[2]["hint"] and "Max items" in issues[3]["hint"]

    def test_a_clean_run_has_no_issues(self):
        assert runner.issues_from_manifest({"archives": [{"target": "a", "error": ""}], "extras": []}) == []
        assert runner.issues_from_manifest({}) == []

    @pytest.mark.parametrize("message,needle", [
        ("HTTP 422 POST https://api.github.com/user/migrations", "scope auto"),
        ("HTTP 403 GET https://api.github.com/repos/o/r/dependabot/alerts", "security_events"),
        ("HTTP 404 GET https://api.github.com/repos/o/r/code-scanning/alerts", "Advanced Security"),
        ("HTTP 429 GET https://x — rate limit", "throttled"),
        ("network error GET https://x: boom", "connectivity"),
        ("timed out after 3600s waiting", "timeout"),
        ("only 10 MB free where this run writes", "disk space"),
        ("something nobody planned for", ""),
    ])
    def test_hints(self, message, needle):
        hint = runner.explain(message)
        assert (needle in hint) if needle else hint == ""

    def test_a_run_records_its_issues_in_the_manifest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "run_dir", lambda spec: tmp_path)
        monkeypatch.setattr(supplementary, "_json_page", lambda u, *, headers, timeout=60: (_ for _ in ()).throw(
            MigrationError("HTTP 403 GET https://api.bitbucket.org/2.0/x — nope")))
        spec = runner.JobSpec(provider="bitbucket", token="t", targets=["ws/r"], extras=["refs"])
        manifest = runner.run_job(spec, log=lambda _m: None)
        assert manifest["failed"] == 1 and manifest["issues"][0]["step"] == "refs"
        assert json.loads((tmp_path / "manifest.json").read_text())["issues"] == manifest["issues"]

    def test_truncated_listings_are_flagged_in_the_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "run_dir", lambda spec: tmp_path)
        monkeypatch.setattr(supplementary, "_json_page", lambda u, *, headers, timeout=60: (
            {"values": [{}, {}, {}], "next": "https://api.bitbucket.org/2.0/more"}, None, {}))
        spec = runner.JobSpec(provider="bitbucket", token="t", targets=["ws/r"], extras=["refs"], max_items=2)
        manifest = runner.run_job(spec, log=lambda _m: None)
        assert manifest["extras"][0]["truncated"] and manifest["failed"] == 0
        assert [i["kind"] for i in manifest["issues"]] == ["limit"]

    def test_max_items_zero_means_no_limit(self):
        assert runner._max_items(None) == 200 and runner._max_items("") == 200
        assert runner._max_items("0") == 0 and runner._max_items("75") == 75
        assert runner.JobSpec.from_form({"provider": "github", "max_items": "0"}).max_items == 0


# ── the web layer ────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    import app
    monkeypatch.setattr(app, "github_token", lambda: "env-token")
    return app.app.test_client()


class TestWeb:
    def test_discover_serves_one_page_at_a_time(self, client, monkeypatch):
        seen = []

        def fake_page(self, page=1, *, org="", everything=False):
            seen.append((page, org, everything))
            return {"items": [{"target": f"o/r{page}"}], "has_more": page < 3, "notes": ["note"] if page == 2 else []}

        monkeypatch.setattr(mig.GitHubMigration, "repos_page", fake_page)
        first = client.post("/api/discover", json={"provider": "github", "kind": "repos", "scope": "auto"}).get_json()
        third = client.post("/api/discover", json={"provider": "github", "kind": "repos", "scope": "auto", "page": 3}).get_json()
        assert (first["page"], first["has_more"], third["page"], third["has_more"]) == (1, True, 3, False)
        assert seen == [(1, "", True), (3, "", True)]

    def test_the_browser_cannot_supply_a_url_or_a_bad_page(self, client, monkeypatch):
        reply = client.post("/api/discover", json={"provider": "github", "page": "https://evil.example/x"})
        assert reply.status_code == 400
        pages = []
        monkeypatch.setattr(mig.GitHubMigration, "repos_page", lambda self, page=1, **kw: pages.append(page) or
                            {"items": [], "has_more": False, "notes": []})
        assert client.post("/api/discover", json={"provider": "github", "page": -5}).status_code == 200
        assert pages == [1]                                 # clamped, never negative

    def test_extras_endpoint_lists_light_before_heavy(self, client):
        extras = client.get("/api/extras").get_json()["extras"]
        flags = [e["heavy"] for e in extras["github"]]
        assert flags == sorted(flags) and set(extras) >= {"github", "gitlab", "bitbucket", "bitbucket-dc"}

    def test_zip_download_is_a_zip_of_zips_and_is_cleaned_up(self, client, tmp_path, monkeypatch):
        import app
        run = make_run(tmp_path)
        monkeypatch.setattr(app, "_run_root", lambda name: run)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setattr(bundle.tempfile, "gettempdir", lambda: str(scratch))
        reply = client.get("/api/archive-zip/run1")
        assert reply.status_code == 200 and reply.mimetype == "application/zip"
        body = reply.get_data()
        reply.close()
        outer = zipfile.ZipFile(io.BytesIO(body))
        assert sorted(outer.namelist()) == ["acme-api.zip", "acme-web.zip", "run-files.zip", "ws-orphan.zip"]
        assert list(scratch.iterdir()) == []                # temp file removed after sending

    def test_zip_download_errors_are_clear(self, client, tmp_path, monkeypatch):
        import app
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(app, "_run_root", lambda name: empty)
        assert client.get("/api/archive-zip/x").status_code == 404
        run = make_run(tmp_path)
        monkeypatch.setattr(app, "_run_root", lambda name: run)
        monkeypatch.setattr(bundle, "check_disk", lambda *a, **k: (_ for _ in ()).throw(MigrationError("only 1 MB free")))
        reply = client.get("/api/archive-zip/run1")
        assert reply.status_code == 507 and "MB free" in reply.get_json()["error"]

    def test_job_status_and_listing_carry_the_issues(self, client):
        import jobstore
        spec = runner.JobSpec(provider="github", token="t", targets=["o/r"])
        job_id = jobstore.create(spec)
        legacy = {"archives": [{"target": "o/r", "error": "HTTP 403 POST https://api.github.com/orgs/o/migrations"}],
                  "extras": [], "failed": 1, "ok": 0}               # written before `issues` existed
        jobstore.update(job_id, manifest=legacy, state="partial")
        status = client.get(f"/api/jobs/{job_id}").get_json()
        assert status["state"] == "partial" and len(status["issues"]) == 1 and "Owner" in status["issues"][0]["hint"]
        listed = {j["id"]: j for j in client.get("/api/jobs").get_json()["jobs"]}
        assert listed[job_id]["issues"] == 1

    def test_the_page_has_the_secret_button_and_issue_dialog_and_no_partial_label(self, client):
        html = client.get("/").get_data(as_text=True)
        for needle in ('id="opts-open"', 'id="opts-backdrop"', 'id="issues-backdrop"', 'id="picker-sso"', 'id="zip2"'):
            assert needle in html
        assert "<button type=\"button\" class=\"secret\"" in html
        assert "SAML SSO" in html
