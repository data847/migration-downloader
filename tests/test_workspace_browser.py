"""Browse workspaces: one list of what the token belongs to (GitHub orgs, Bitbucket workspaces, GitLab top-level groups; the
personal account last), then the repos inside the one clicked; GitLab groups also open into their subgroups.

Bitbucket Cloud retired its cross-workspace endpoints (CHANGE-2770: GET /2.0/repositories?role=member answers 404), so its
repos are listed per workspace from /2.0/user/workspaces, the one cross-workspace endpoint Atlassian keeps.
"""

import contextlib
import unittest
from unittest import mock

import bitbucket
from migration_api import GitHubMigration, GitLabExport, MigrationError


class FakeHttp:
    """Maps URL substrings (first match wins, so list the specific ones first) to responses; records every URL."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, *, method="GET", headers=None, payload=None, timeout=60):
        self.calls.append(url)
        for needle, response in self.routes.items():
            if needle in url:
                if isinstance(response, Exception):
                    raise response
                return 200, response
        raise MigrationError(f"HTTP 404 {method} {url}")


@contextlib.contextmanager
def patch_http(fake):
    def page(url, *, headers, timeout=60):
        _status, data = fake(url, headers=headers)
        return data, None, {}

    with mock.patch("supplementary._json_request", fake), \
            mock.patch("migration_api._json_request", fake), \
            mock.patch("bitbucket._json_request", fake), \
            mock.patch("supplementary._json_page", page), \
            mock.patch("migration_api._json_page", page):
        yield fake


def post(payload):
    import app
    return app.app.test_client().post("/api/discover", json=payload)


class GitHubWorkspaces(unittest.TestCase):
    def test_orgs_then_personal_in_one_list(self):
        fake = FakeHttp({"/user/orgs": [{"login": "zeta-co"}, {"login": "Acme"}], "/user": {"login": "me"}})
        with patch_http(fake):
            result = GitHubMigration("t").owners_page()
        self.assertEqual([(i["name"], i["kind"]) for i in result["items"]],
                         [("Acme", "org"), ("zeta-co", "org"), ("me", "personal")])
        self.assertFalse(result["has_more"])

    def test_repos_of_an_org_are_owner_slash_repo(self):
        fake = FakeHttp({"/orgs/acme/repos": [{"name": "api", "full_name": "acme/api"}]})
        with patch_http(fake):
            result = GitHubMigration("t").repos_page(1, org="acme", full_names=True)
        self.assertEqual([i["target"] for i in result["items"]], ["acme/api"])

    def test_personal_repos_are_only_the_ones_the_user_owns(self):
        fake = FakeHttp({"/user/repos": [{"name": "dots", "full_name": "me/dots"}]})
        with patch_http(fake):
            result = GitHubMigration("t").repos_page(1, personal=True)
        self.assertEqual([i["target"] for i in result["items"]], ["me/dots"])
        self.assertIn("affiliation=owner&", fake.calls[0])

    def test_discover_serves_workspaces_and_their_repos(self):
        fake = FakeHttp({"/user/orgs": [{"login": "acme"}], "/orgs/acme/repos": [{"name": "api", "full_name": "acme/api"}],
                         "/user/repos": [{"name": "dots", "full_name": "me/dots"}], "/user": {"login": "me"}})
        with patch_http(fake):
            ws = post({"provider": "github", "kind": "workspaces", "token": "t"}).get_json()
            org = post({"provider": "github", "kind": "repos", "token": "t", "owner": "acme", "owner_kind": "org"}).get_json()
            mine = post({"provider": "github", "kind": "repos", "token": "t", "owner": "me", "owner_kind": "personal"}).get_json()
        self.assertEqual([i["name"] for i in ws["items"]], ["acme", "me"])
        self.assertEqual([i["target"] for i in org["items"]], ["acme/api"])
        self.assertEqual([i["target"] for i in mine["items"]], ["me/dots"])


class BitbucketWorkspaces(unittest.TestCase):
    WS = {"values": [{"workspace": {"slug": "observence", "name": "Observence"}},
                     {"workspace": {"slug": "lamp_experts", "name": "Lamp Experts"}},
                     {"workspace": {"slug": "acme", "name": "acme"}}]}

    def test_workspaces_come_from_the_kept_endpoint_with_personal_last(self):
        fake = FakeHttp({"/2.0/user/workspaces": self.WS, "/2.0/user": {"username": "lamp_experts"}})
        with patch_http(fake):
            result = bitbucket.cloud_workspaces_page("me@x.com:tok")
        self.assertEqual([(i["name"], i["kind"]) for i in result["items"]],
                         [("acme", "workspace"), ("observence", "workspace"), ("lamp_experts", "personal")])
        self.assertFalse(any("repositories?role=member" in u for u in fake.calls))

    def test_falls_back_to_the_permissions_listing_when_the_new_endpoint_is_missing(self):
        fake = FakeHttp({"/2.0/user/workspaces": MigrationError("HTTP 404 GET x"),
                         "/2.0/user/permissions/workspaces": {"values": [{"workspace": {"slug": "acme", "name": "Acme"}}]},
                         "/2.0/user": {"username": "me"}})
        with patch_http(fake):
            result = bitbucket.cloud_workspaces_page("t")
        self.assertEqual([i["name"] for i in result["items"]], ["acme"])

    def test_repos_are_listed_inside_one_workspace(self):
        fake = FakeHttp({"/2.0/repositories/observence": {"values": [{"full_name": "observence/payments"}]}})
        with patch_http(fake):
            result = bitbucket.cloud_repos_page("t", page=2, workspace="observence")
        self.assertEqual([i["target"] for i in result["items"]], ["observence/payments"])
        self.assertIn("/2.0/repositories/observence?", fake.calls[0])
        self.assertIn("page=2", fake.calls[0])
        self.assertNotIn("role=member", fake.calls[0])

    def test_no_cross_workspace_listing_any_more(self):
        with self.assertRaisesRegex(MigrationError, "workspace"):
            bitbucket.cloud_repos_page("t")

    def test_discover_serves_bitbucket_workspaces_and_says_to_pick_one(self):
        fake = FakeHttp({"/2.0/user/workspaces": self.WS, "/2.0/user": {"username": "lamp_experts"},
                         "/2.0/repositories/acme": {"values": [{"full_name": "acme/site"}]}})
        with patch_http(fake):
            ws = post({"provider": "bitbucket", "kind": "workspaces", "token": "e:t"})
            repos = post({"provider": "bitbucket", "kind": "repos", "token": "e:t", "owner": "acme", "owner_kind": "workspace"})
            flat = post({"provider": "bitbucket", "kind": "repos", "token": "e:t"})
        self.assertEqual(len(ws.get_json()["items"]), 3)
        self.assertEqual([i["target"] for i in repos.get_json()["items"]], ["acme/site"])
        self.assertEqual(flat.status_code, 400)
        self.assertIn("workspace", flat.get_json()["error"])


class GitLabWorkspaces(unittest.TestCase):
    def test_top_level_groups_then_the_personal_namespace(self):
        fake = FakeHttp({"/api/v4/groups?": [{"full_path": "platform", "name": "Platform"}, {"full_path": "acme", "name": "Acme"}],
                         "/api/v4/user": {"username": "me"}})
        with patch_http(fake):
            result = GitLabExport("t").workspaces_page()
        self.assertEqual([(i["name"], i["kind"]) for i in result["items"]],
                         [("acme", "group"), ("platform", "group"), ("me", "personal")])
        self.assertIn("top_level_only=true", fake.calls[0])

    def test_a_group_opens_into_its_subgroups_and_its_own_projects(self):
        fake = FakeHttp({"/groups/acme%2Fteam/subgroups": [{"full_path": "acme/team/infra", "name": "infra"}],
                         "/groups/acme%2Fteam/projects": [{"path_with_namespace": "acme/team/api"}]})
        with patch_http(fake):
            first = GitLabExport("t").group_page("acme/team", page=1)
            second = GitLabExport("t").group_page("acme/team", page=2)
        self.assertEqual([(i.get("kind"), i.get("name") or i.get("target")) for i in first["items"]],
                         [("group", "acme/team/infra"), (None, "acme/team/api")])
        projects_url = [u for u in fake.calls if "/projects" in u][0]
        self.assertIn("include_subgroups=false", projects_url)   # subgroups are opened, not flattened in
        self.assertIn("min_access_level=40", projects_url)        # only projects the token may export
        self.assertEqual([i.get("kind") for i in second["items"]], [None])  # subgroups come with page 1 only

    def test_personal_projects_are_the_ones_the_user_owns(self):
        fake = FakeHttp({"/api/v4/projects": [{"path_with_namespace": "me/dots"}]})
        with patch_http(fake):
            result = GitLabExport("t").personal_projects_page(1)
        self.assertEqual([i["target"] for i in result["items"]], ["me/dots"])
        self.assertIn("owned=true", fake.calls[0])

    def test_discover_serves_gitlab_groups_subgroups_and_personal(self):
        fake = FakeHttp({"/api/v4/groups/acme/subgroups": [{"full_path": "acme/team", "name": "team"}],
                         "/api/v4/groups/acme/projects": [{"path_with_namespace": "acme/site"}],
                         "/api/v4/groups?": [{"full_path": "acme", "name": "Acme"}],
                         "/api/v4/projects": [{"path_with_namespace": "me/dots"}],
                         "/api/v4/user": {"username": "me"}})
        with patch_http(fake):
            ws = post({"provider": "gitlab", "kind": "workspaces", "token": "t"}).get_json()
            grp = post({"provider": "gitlab", "kind": "repos", "token": "t", "owner": "acme", "owner_kind": "group"}).get_json()
            mine = post({"provider": "gitlab", "kind": "repos", "token": "t", "owner": "me", "owner_kind": "personal"}).get_json()
        self.assertEqual([i["name"] for i in ws["items"]], ["acme", "me"])
        self.assertEqual([i.get("name") or i.get("target") for i in grp["items"]], ["acme/team", "acme/site"])
        self.assertEqual([i["target"] for i in mine["items"]], ["me/dots"])


class Page(unittest.TestCase):
    def test_the_page_has_browse_workspaces_and_a_way_back(self):
        import app
        html = app.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('id="pick-ws"', html)
        self.assertIn('id="picker-back"', html)
        self.assertIn("openPicker('workspaces')", html)
        self.assertIn("owner_kind: pickerOwner ? pickerOwner.kind : ''", html)
        # Bitbucket Cloud's flat Browse & select is hidden: it can only list inside a workspace now
        self.assertIn("$('pick-repos').hidden = bb;", html)


if __name__ == "__main__":
    unittest.main()
