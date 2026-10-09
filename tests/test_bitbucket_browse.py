"""The repo picker for Bitbucket Cloud and Data Center."""

import base64

import pytest

import bitbucket
import supplementary
from errors import MigrationError


def serve(monkeypatch, payload):
    seen = []

    def fake_page(url, *, headers, timeout=60):
        seen.append((url, headers))
        return payload, None, {}

    monkeypatch.setattr(supplementary, "_json_page", fake_page)
    return seen


CLOUD = {"values": [
    {"full_name": "acme/api", "is_private": True, "size": 20480, "updated_on": "2026-09-30T10:00:00Z"},
    {"full_name": "other/site", "is_private": False, "updated_on": "2026-08-01T00:00:00Z"},
    {"slug": "no-full-name"},
], "next": "https://api.bitbucket.org/2.0/repositories?page=2"}


def test_cloud_lists_member_repos_as_workspace_slash_repo(monkeypatch):
    seen = serve(monkeypatch, CLOUD)
    result = bitbucket.cloud_repos_page("me@x.com:tok", page=3)
    assert [i["target"] for i in result["items"]] == ["acme/api", "other/site"]
    assert result["items"][0] == {"target": "acme/api", "label": "acme/api", "private": True, "archived": False,
                                  "size_kb": 20, "updated": "2026-09-30"}
    assert result["items"][1]["private"] is False
    assert result["has_more"] is True
    url, headers = seen[0]
    assert "role=member" in url and "page=3" in url and "pagelen=100" in url
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"me@x.com:tok").decode()


def test_cloud_accepts_a_bare_access_token_and_knows_the_last_page(monkeypatch):
    seen = serve(monkeypatch, {"values": [{"full_name": "a/b"}]})
    result = bitbucket.cloud_repos_page("ACCESSTOKEN")
    assert result["has_more"] is False
    assert seen[0][1]["Authorization"] == "Bearer ACCESSTOKEN"


DC = {"values": [
    {"slug": "service", "project": {"key": "PROJ"}, "public": False},
    {"slug": "old", "project": {"key": "PROJ"}, "archived": True, "public": True},
    {"slug": "orphan"},
], "isLastPage": False, "nextPageStart": 100}


def test_data_center_lists_project_key_slash_slug(monkeypatch):
    seen = serve(monkeypatch, DC)
    result = bitbucket.dc_repos_page("pat", "https://bb.example.com/", page=2)
    assert [i["target"] for i in result["items"]] == ["PROJ/service", "PROJ/old"]
    assert [i["private"] for i in result["items"]] == [True, False]
    assert result["items"][1]["archived"] is True and result["has_more"] is True
    url, headers = seen[0]
    assert url == "https://bb.example.com/rest/api/1.0/repos?limit=100&start=100"
    assert headers["Authorization"] == "Bearer pat"


def test_data_center_needs_its_base_url_and_stops_on_the_last_page(monkeypatch):
    with pytest.raises(MigrationError, match="base URL"):
        bitbucket.dc_repos_page("pat", "")
    serve(monkeypatch, {"values": [], "isLastPage": True})
    assert bitbucket.dc_repos_page("pat", "https://bb")["has_more"] is False


@pytest.fixture
def client(monkeypatch):
    import app
    monkeypatch.setenv("BITBUCKET_TOKEN", "env-bb-token")
    monkeypatch.setenv("BITBUCKET_DC_TOKEN", "env-dc-token")
    return app.app.test_client()


def test_discover_serves_bitbucket_cloud_pages(client, monkeypatch):
    calls = []
    monkeypatch.setattr(bitbucket, "cloud_repos_page", lambda token, page=1, **kw: calls.append((token, page)) or
                        {"items": [{"target": "a/b"}], "has_more": True, "notes": []})
    reply = client.post("/api/discover", json={"provider": "bitbucket", "kind": "repos", "page": 2}).get_json()
    assert reply["items"] == [{"target": "a/b"}] and reply["has_more"] is True and reply["page"] == 2
    assert calls == [("env-bb-token", 2)]                     # blank token box -> BITBUCKET_TOKEN from .env


def test_discover_serves_data_center_with_the_typed_base_url(client, monkeypatch):
    calls = []
    monkeypatch.setattr(bitbucket, "dc_repos_page", lambda token, base, page=1: calls.append((token, base, page)) or
                        {"items": [], "has_more": False, "notes": []})
    reply = client.post("/api/discover", json={"provider": "bitbucket-dc", "token": "typed", "api_base": "https://bb",
                                               "page": 1})
    assert reply.status_code == 200 and calls == [("typed", "https://bb", 1)]


def test_discover_reports_bitbucket_errors_instead_of_crashing(client, monkeypatch):
    def boom(token, page=1, **kw):
        raise MigrationError("HTTP 401 GET https://api.bitbucket.org/2.0/repositories\n{\"error\": {\"message\": \"Token is invalid\"}}")
    monkeypatch.setattr(bitbucket, "cloud_repos_page", boom)
    reply = client.post("/api/discover", json={"provider": "bitbucket", "token": "bad"})
    assert reply.status_code == 400 and "Token is invalid" in reply.get_json()["error"]


def test_discover_without_any_token_says_so(client, monkeypatch):
    monkeypatch.delenv("BITBUCKET_TOKEN", raising=False)
    monkeypatch.delenv("BB_TOKEN", raising=False)
    reply = client.post("/api/discover", json={"provider": "bitbucket"})
    assert reply.status_code == 400 and "no bitbucket token" in reply.get_json()["error"]


def test_the_page_offers_browse_for_bitbucket_and_says_which_token_to_paste(client):
    html = client.get("/").get_data(as_text=True)
    assert "$('pick-repos').hidden = bbAny" not in html            # the button is no longer hidden
    assert "gh2 || bbAny ? 'Browse & select repos…'" in html
    assert "email@example.com:API_TOKEN" in html and "HTTP access token" in html
