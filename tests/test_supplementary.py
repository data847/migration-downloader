"""Offline tests for the supplementary collectors and the new export options.

No network: `_json_request`, `_download` and `run_git` are replaced by fakes
that serve canned responses and record what was asked for.

    python3 -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap  # noqa: E402,F401
import bitbucket  # noqa: E402
import supplementary  # noqa: E402
from migration_api import GitHubMigration, GitLabExport, MigrationError  # noqa: E402
from runner import JobSpec, _run_extras  # noqa: E402


class FakeHttp:
    """Maps URL substrings to responses; records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, *, method="GET", headers=None, payload=None, timeout=60):
        self.calls.append((method, url, payload))
        for needle, response in self.routes.items():
            if needle in url:
                if isinstance(response, Exception):
                    raise response
                return 200, response
        raise MigrationError(f"HTTP 404 {method} {url}")


def make_ctx(tmp, provider="github", target="acme/widgets", **kw):
    return supplementary.Ctx(
        provider=provider, target=target, token="secret-token-123",
        api_base=kw.pop("api_base", "https://api.github.com"),
        headers={"Authorization": "Bearer secret-token-123"},
        out=Path(tmp), log=lambda _m: None, **kw)


class ResolveNames(unittest.TestCase):
    def test_all_skips_heavy_and_everything_includes_them(self):
        light = supplementary.resolve_names("github", ["all"])
        full = supplementary.resolve_names("github", ["everything"])
        self.assertIn("releases", light)
        self.assertNotIn("wiki", light)
        self.assertIn("wiki", full)

    def test_removed_collectors_stay_removed(self):
        gone = {
            "github": {"release-assets", "mirror", "actions-artifact-files", "collaborators",
                       "deploy-keys", "lfs"},
            "gitlab": {"deploy-keys", "deploy-tokens", "members", "variables", "lfs", "mirror",
                       "packages", "registry", "group-export"},
            "bitbucket": {"default-reviewers", "deploy-keys", "permissions", "workspace-members",
                          "lfs", "mirror", "download-files"},
        }
        for provider, names in gone.items():
            self.assertFalse(names & set(supplementary.REGISTRY[provider]), provider)

    def test_unknown_name_is_rejected(self):
        with self.assertRaises(MigrationError):
            supplementary.resolve_names("gitlab", ["not-a-thing"])

    def test_duplicates_collapse(self):
        self.assertEqual(supplementary.resolve_names("github", ["hooks", "hooks"]), ["hooks"])

    def test_every_provider_has_collectors(self):
        for provider in ("github", "gitlab", "bitbucket", "bitbucket-dc"):
            self.assertTrue(supplementary.REGISTRY[provider], provider)


class GitHubExportOptions(unittest.TestCase):
    def test_exclude_flags_land_in_the_payload(self):
        fake = FakeHttp({"/migrations": {"id": 7, "state": "pending"}})
        with mock.patch("migration_api._json_request", fake):
            client = GitHubMigration("t", scope="org", org="acme",
                                     exclude=["releases", "attachments"], org_metadata_only=True)
            self.assertEqual(client.start(["acme/widgets"]), "7")
        payload = fake.calls[0][2]
        self.assertEqual(payload["repositories"], ["widgets"])
        self.assertTrue(payload["exclude_releases"] and payload["exclude_attachments"])
        self.assertTrue(payload["org_metadata_only"])
        self.assertNotIn("exclude_metadata", payload)

    def test_bad_exclude_and_user_scope_org_metadata_only(self):
        with self.assertRaises(MigrationError):
            GitHubMigration("t", exclude=["bogus"])
        with self.assertRaises(MigrationError):
            GitHubMigration("t", scope="user", org_metadata_only=True)

    def test_housekeeping_urls(self):
        fake = FakeHttp({"/migrations": []})
        with mock.patch("migration_api._json_request", fake):
            client = GitHubMigration("t", scope="org", org="acme")
            client.unlock_repo("9", "acme/widgets")
            client.delete_archive("9")
        self.assertEqual(fake.calls[0][:2], ("DELETE", "https://api.github.com/orgs/acme/migrations/9/repos/widgets/lock"))
        self.assertEqual(fake.calls[1][:2], ("DELETE", "https://api.github.com/orgs/acme/migrations/9/archive"))


class GitLabExportOptions(unittest.TestCase):
    def test_upload_and_description_are_sent(self):
        fake = FakeHttp({"/export": {}})
        with mock.patch("migration_api._json_request", fake):
            GitLabExport("t").start("g/p", upload_url="https://s3/x?sig=1", upload_method="PUT",
                                    description="nightly")
        self.assertEqual(fake.calls[0][2], {"description": "nightly",
                                            "upload": {"url": "https://s3/x?sig=1", "http_method": "PUT"}})

    def test_plain_start_sends_no_body(self):
        fake = FakeHttp({"/export": {}})
        with mock.patch("migration_api._json_request", fake):
            GitLabExport("t").start("g/p")
        self.assertIsNone(fake.calls[0][2])


class GitHubCollectors(unittest.TestCase):
    def test_org_scope_bare_name_builds_owner_repo_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp, target="widgets", org="acme", scope="org")
            fake = FakeHttp({"/repos/acme/widgets/hooks": [{"id": 1}]})
            with mock.patch("supplementary._json_request", fake):
                files = supplementary.REGISTRY["github"]["hooks"](ctx)
            self.assertEqual(files, ["hooks.json"])
            self.assertEqual(json.loads((Path(tmp) / "hooks.json").read_text()), [{"id": 1}])

    def test_branch_protection_records_per_branch_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp)
            fake = FakeHttp({
                "branches?protected=true": [{"name": "main"}, {"name": "dev"}],
                "/branches/main/protection": {"required_reviews": 2},
                "/branches/dev/protection": MigrationError("HTTP 403 nope"),
            })
            with mock.patch("supplementary._json_request", fake):
                supplementary.REGISTRY["github"]["branch-protection"](ctx)
            data = json.loads((Path(tmp) / "branch-protection.json").read_text())
            self.assertEqual(data["main"], {"required_reviews": 2})
            self.assertIn("error", data["dev"])

    def test_actions_unwraps_workflow_runs_and_respects_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp, max_items=2)
            runs = {"workflow_runs": [{"id": 1}, {"id": 2}, {"id": 3}]}
            fake = FakeHttp({"/actions/runs?": runs, "/jobs": {"jobs": []}})
            with mock.patch("supplementary._json_request", fake):
                supplementary.REGISTRY["github"]["actions"](ctx)
            saved = json.loads((Path(tmp) / "actions-runs.json").read_text())
            self.assertEqual([r["id"] for r in saved], [1, 2])

    def test_projects_v2_runs_once_per_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp)
            fake = FakeHttp({"/graphql": {"data": {"user": {"projectsV2": {"nodes": []}}}}})
            with mock.patch("supplementary._json_request", fake):
                first = supplementary.REGISTRY["github"]["projects-v2"](ctx)
                second = supplementary.REGISTRY["github"]["projects-v2"](ctx)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            self.assertEqual(len(fake.calls), 1)

    def test_ghes_api_base_maps_to_web_and_graphql(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp, api_base="https://ghe.example.com/api/v3")
            self.assertEqual(supplementary.gh_web_base(ctx), "https://ghe.example.com")
            self.assertEqual(supplementary._graphql_url(ctx), "https://ghe.example.com/api/graphql")
            public = make_ctx(tmp)
            self.assertEqual(supplementary.gh_web_base(public), "https://github.com")


class GitHandling(unittest.TestCase):
    def test_token_never_appears_in_git_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp)
            calls = []

            def fake_git(args, *, env, cwd=None):
                calls.append((args, env))
                return mock.Mock(returncode=0, stderr="")

            with mock.patch("supplementary.run_git", fake_git):
                supplementary.REGISTRY["github"]["wiki"](ctx)
            args, env = calls[0]
            self.assertNotIn("secret-token-123", " ".join(args))
            self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
            self.assertTrue(env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))
            self.assertEqual(args[:2], ["clone", "--mirror"])

    def test_missing_wiki_is_skipped_not_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp)
            gone = mock.Mock(returncode=128, stderr="fatal: Repository not found.")
            with mock.patch("supplementary.run_git", lambda *a, **k: gone):
                self.assertEqual(supplementary.REGISTRY["github"]["wiki"](ctx), [])

    def test_other_git_failures_still_fail_the_wiki_collector(self):
        with tempfile.TemporaryDirectory() as tmp:
            denied = mock.Mock(returncode=128, stderr="fatal: Authentication failed")
            with mock.patch("supplementary.run_git", lambda *a, **k: denied):
                with self.assertRaises(MigrationError):
                    supplementary.REGISTRY["github"]["wiki"](make_ctx(tmp))

    def test_existing_wiki_clone_is_updated_not_recloned(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "wiki.git").mkdir()
            (Path(tmp) / "wiki.git" / "HEAD").write_text("ref: refs/heads/main")
            calls = []
            ok = mock.Mock(returncode=0, stderr="")
            with mock.patch("supplementary.run_git", lambda a, **k: calls.append(a) or ok):
                supplementary.REGISTRY["github"]["wiki"](make_ctx(tmp))
            self.assertEqual(calls[0][0], "remote")

    def test_gitlab_wiki_uses_oauth2_user_and_wiki_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp, provider="gitlab", target="g/p", api_base="https://gitlab.com")
            seen = []
            ok = mock.Mock(returncode=0, stderr="")

            def fake_git(args, *, env, cwd=None):
                seen.append((args, env))
                return ok

            with mock.patch("supplementary.run_git", fake_git):
                supplementary.REGISTRY["gitlab"]["wiki"](ctx)
            import base64
            args, env = seen[0]
            self.assertIn("https://gitlab.com/g/p.wiki.git", args)
            value = env["GIT_CONFIG_VALUE_0"].split()[-1]
            self.assertEqual(base64.b64decode(value).decode(), "oauth2:secret-token-123")


class GitLabCollectors(unittest.TestCase):
    def test_project_path_is_url_encoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp, provider="gitlab", target="grp/sub/proj", api_base="https://gitlab.com")
            fake = FakeHttp({"/projects/grp%2Fsub%2Fproj/releases": [{"tag_name": "v1"}]})
            with mock.patch("supplementary._json_request", fake):
                supplementary.REGISTRY["gitlab"]["releases"](ctx)
            self.assertTrue((Path(tmp) / "releases.json").is_file())

    def test_job_artifact_404_is_silent_other_errors_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            logged = []
            ctx = make_ctx(tmp, provider="gitlab", target="g/p", api_base="https://gitlab.com")
            ctx.log = logged.append
            ctx.shared["gl_jobs"] = {"g/p": [1, 2]}

            def fake_download(url, dest, *, headers, log, timeout=120):
                raise MigrationError("HTTP 404 x" if "/jobs/1/" in url else "HTTP 500 x")

            with mock.patch("supplementary._download", fake_download):
                self.assertEqual(supplementary.REGISTRY["gitlab"]["job-artifacts"](ctx), [])
            self.assertEqual(len(logged), 1)
            self.assertIn("job 2", logged[0])


class BitbucketCloud(unittest.TestCase):
    def test_auth_header_shapes(self):
        self.assertTrue(bitbucket.cloud_headers("tok")["Authorization"].startswith("Bearer "))
        self.assertTrue(bitbucket.cloud_headers("me@x.com:tok")["Authorization"].startswith("Basic "))

    def test_paging_follows_next_links_and_caps(self):
        pages = {
            "pagelen=100": {"values": [1, 2], "next": "https://api/page2"},
            "page2": {"values": [3, 4], "next": "https://api/page3"},
            "page3": {"values": [5]},
        }
        fake = FakeHttp(pages)
        with mock.patch("supplementary._json_request", fake):
            self.assertEqual(bitbucket.cloud_paged("https://api/x", {}, cap=10), [1, 2, 3, 4, 5])
            self.assertEqual(bitbucket.cloud_paged("https://api/x", {}, cap=3), [1, 2, 3])

    def test_pullrequests_query_every_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp, provider="bitbucket", target="ws/repo",
                           api_base="https://api.bitbucket.org/2.0")
            fake = FakeHttp({"/pullrequests?": {"values": []}})
            with mock.patch("supplementary._json_request", fake):
                supplementary.REGISTRY["bitbucket"]["pullrequests"](ctx)
            url = fake.calls[0][1]
            for state in bitbucket.PR_STATES:
                self.assertIn(f"state={state}", url)

    def test_workspace_level_collectors_run_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = {}
            fake = FakeHttp({"/hooks": {"values": [{"u": 1}]}})
            with mock.patch("supplementary._json_request", fake):
                first = supplementary.REGISTRY["bitbucket"]["hooks"](
                    make_ctx(tmp, provider="bitbucket", target="ws/a", shared=shared,
                             api_base="https://api.bitbucket.org/2.0"))
                second = supplementary.REGISTRY["bitbucket"]["hooks"](
                    make_ctx(tmp, provider="bitbucket", target="ws/b", shared=shared,
                             api_base="https://api.bitbucket.org/2.0"))
            # repo hooks every time, workspace hooks only the first time
            self.assertEqual(len(first), 2)
            self.assertEqual(len(second), 1)


class BitbucketDataCenter(unittest.TestCase):
    def test_request_body_and_validation(self):
        body = bitbucket.BitbucketDCExport.request_body(["PROJ/repo-1"])
        self.assertEqual(body, {"repositoriesRequest": {"includes": [{"projectKey": "PROJ", "slug": "repo-1"}]}})
        with self.assertRaises(MigrationError):
            bitbucket.BitbucketDCExport.request_body(["no-slash"])
        with self.assertRaises(MigrationError):
            bitbucket.BitbucketDCExport.request_body([])

    def test_wait_stops_on_completed_and_raises_on_failed(self):
        client = bitbucket.BitbucketDCExport("t", "https://bb.example.com")
        with mock.patch.object(client, "status", return_value={"state": "completed"}):
            self.assertEqual(client.wait("1")["state"], "completed")
        with mock.patch.object(client, "status", return_value={"state": "FAILED"}):
            with self.assertRaises(MigrationError):
                client.wait("1")

    def test_cancel_and_preview_urls(self):
        fake = FakeHttp({"/migration/exports": {}})
        with mock.patch("bitbucket._json_request", fake):
            client = bitbucket.BitbucketDCExport("t", "https://bb.example.com/")
            client.preview(["P/r"])
            client.cancel("42")
        self.assertEqual(fake.calls[0][1], "https://bb.example.com/rest/api/1.0/migration/exports/preview")
        self.assertEqual(fake.calls[1][1], "https://bb.example.com/rest/api/1.0/migration/exports/42/cancel")

    def test_base_url_required(self):
        with self.assertRaises(MigrationError):
            bitbucket.BitbucketDCExport("t", "")


class SpecValidation(unittest.TestCase):
    def _spec(self, **kw):
        return JobSpec(token="t", targets=kw.pop("targets", ["a/b"]), **kw)

    def test_bitbucket_cloud_requires_extras(self):
        with self.assertRaises(MigrationError):
            self._spec(provider="bitbucket").validate()
        self._spec(provider="bitbucket", extras=["refs"]).validate()

    def test_unknown_extra_fails_fast(self):
        with self.assertRaises(MigrationError):
            self._spec(provider="github", extras=["nope"]).validate()

    def test_unlock_requires_lock(self):
        with self.assertRaises(MigrationError):
            self._spec(provider="github", unlock_repos=True).validate()

    def test_upload_url_is_gitlab_only(self):
        with self.assertRaises(MigrationError):
            self._spec(provider="github", upload_url="https://x").validate()

    def test_dc_needs_base_url_and_cancel_needs_job(self):
        with self.assertRaises(MigrationError):
            self._spec(provider="bitbucket-dc").validate()
        with self.assertRaises(MigrationError):
            self._spec(provider="bitbucket-dc", api_base="https://bb", dc_action="cancel").validate()
        self._spec(provider="bitbucket-dc", api_base="https://bb", dc_action="cancel",
                   dc_job_id="3", targets=[]).validate()

    def test_from_form_parses_new_fields(self):
        spec = JobSpec.from_form({"provider": "github", "targets": "a/b", "extras": "hooks, keys\nmirror",
                                  "exclude": "releases", "delete_archive": "true", "max_items": "50"})
        self.assertEqual(spec.extras, ["hooks", "keys", "mirror"])
        self.assertEqual(spec.exclude, ["releases"])
        self.assertTrue(spec.delete_archive)
        self.assertEqual(spec.max_items, 50)


class RunExtras(unittest.TestCase):
    def test_one_failing_collector_does_not_stop_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = JobSpec(provider="github", token="t", targets=["acme/widgets"],
                           extras=["hooks", "releases"])
            fake = FakeHttp({"/hooks": MigrationError("HTTP 403 forbidden"), "/releases": [{"id": 1}]})
            logs = []
            with mock.patch("supplementary._json_request", fake):
                records = _run_extras(spec, "t", Path(tmp), logs.append, None)
            by_name = {r["extra"]: r for r in records}
            self.assertIn("403", by_name["hooks"]["error"])
            self.assertEqual(by_name["releases"]["error"], "")
            self.assertEqual(by_name["releases"]["files"], ["releases.json"])
            self.assertTrue((Path(tmp) / "extras" / "acme-widgets" / "releases.json").is_file())

    def test_cancel_stops_further_collectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = JobSpec(provider="github", token="t", targets=["acme/widgets"], extras=["hooks"])
            records = _run_extras(spec, "t", Path(tmp), lambda _m: None, lambda: True)
            self.assertEqual(records[0]["error"], "cancelled")


class DefaultExtras(unittest.TestCase):
    def test_page_has_no_extras_picker_and_always_requests_everything(self):
        import app
        client = app.app.test_client()
        html = client.get("/").get_data(as_text=True)
        self.assertNotIn('id="extras-list"', html)
        self.assertNotIn('id="extras-box"', html)
        self.assertIn("extras: 'everything'", html)
        self.assertEqual(client.get("/api/extras").status_code, 404)

    def test_everything_is_valid_for_every_provider(self):
        for provider, extra in (("github", {}), ("gitlab", {}), ("bitbucket", {}),
                                ("bitbucket-dc", {"api_base": "https://bb.example.com"})):
            spec = JobSpec.from_form({"provider": provider, "targets": "a/b", "token": "t",
                                      "extras": "everything", **extra})
            spec.validate()
            names = supplementary.resolve_names(provider, spec.extras)
            self.assertEqual(set(names), set(supplementary.REGISTRY[provider]))


class BitbucketUi(unittest.TestCase):
    def test_page_offers_both_bitbucket_providers_and_dc_controls(self):
        import app
        html = app.app.test_client().get("/").get_data(as_text=True)
        for needle in ('data-provider="bitbucket"', 'Data Center (self-hosted)',
                       'id="dc_action"', 'id="extras-card"'):
            self.assertIn(needle, html)

    def test_job_payload_roundtrips_through_the_spec(self):
        spec = JobSpec.from_form({"provider": "bitbucket-dc", "targets": "PROJ/repo",
                                  "api_base": "https://bb.example.com", "dc_action": "preview",
                                  "extras": "archive"})
        self.assertEqual((spec.provider, spec.dc_action, spec.extras),
                         ("bitbucket-dc", "preview", ["archive"]))

    def test_upload_url_is_never_persisted(self):
        import jobstore
        spec = JobSpec(provider="gitlab", token="t", targets=["g/p"],
                       upload_url="https://s3.example.com/x?X-Amz-Signature=abc")
        self.assertNotIn("abc", json.dumps(jobstore._public_spec(spec)))

    def test_resuming_a_dc_job_never_restarts_the_export(self):
        import jobstore
        spec = JobSpec(provider="bitbucket-dc", token="t", targets=["P/r"],
                       api_base="https://bb.example.com", extras=["archive"])
        job = {"spec": jobstore._public_spec(spec), "state": "interrupted", "manifest": {}, "log": []}
        resumed = jobstore.spec_for_resume(job, token="t")
        self.assertEqual(resumed.dc_action, "none")
        self.assertEqual(resumed.extras, ["archive"])

    def test_resume_keeps_github_export_options(self):
        import jobstore
        spec = JobSpec(provider="github", token="t", targets=["a/b"], exclude=["releases"],
                       lock_repositories=True, unlock_repos=True)
        job = {"spec": jobstore._public_spec(spec), "state": "interrupted", "manifest": {}, "log": []}
        resumed = jobstore.spec_for_resume(job, token="t")
        self.assertEqual(resumed.exclude, ["releases"])
        self.assertTrue(resumed.unlock_repos)


if __name__ == "__main__":
    unittest.main()
