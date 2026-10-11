"""The retry and paging code against the real urllib stack.

The other tests fake urllib's opener. This one starts a throwaway server on
127.0.0.1 that sends genuine `Link` and `Retry-After` headers, so a mistake in
how real `HTTPError`/header objects are handled can't hide behind a fake.
"""

import http.server
import json
import threading

import pytest

import migration_api as mig
import ratelimit
import supplementary
from errors import MigrationError

_REAL_OPEN = mig._opener.open          # captured before conftest swaps it out for each test


@pytest.fixture
def local_api(monkeypatch):
    """A tiny API on 127.0.0.1 with cursor paging, one 429 and a hostile link."""
    state = {"hits": [], "throttled": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, body, headers=None):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            state["hits"].append(self.path)
            base = f"http://127.0.0.1:{self.server.server_port}"
            if self.path.startswith("/alerts"):
                if not state["throttled"]:
                    state["throttled"] = True
                    return self._json(429, {"message": "slow down"}, {"Retry-After": "1"})
                if "after=c2" in self.path:
                    return self._json(200, [{"n": 4}])
                if "after=c1" in self.path:
                    return self._json(200, [{"n": 3}], {"Link": f'<{base}/alerts?per_page=100&after=c2>; rel="next"'})
                return self._json(200, [{"n": 1}, {"n": 2}],
                                  {"Link": f'<{base}/alerts?per_page=100&after=c1>; rel="next"'})
            if self.path.startswith("/evil"):
                return self._json(200, [{"n": 1}], {"Link": '<http://169.254.169.254/latest/meta-data>; rel="next"'})
            if self.path.startswith("/forbidden"):
                return self._json(403, {"message": "Resource not accessible by personal access token"})
            self._json(404, {"message": "Not Found"})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(mig._opener, "open", _REAL_OPEN)
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()


def make_ctx(tmp_path, base):
    return supplementary.Ctx(provider="github", target="o/r", token="t", api_base=base,
                             headers={"Authorization": "Bearer t"}, out=tmp_path, log=lambda _m: None, max_items=0)


def test_cursor_paging_and_a_429_over_real_http(local_api, tmp_path):
    waits = []
    ratelimit.bind(None, waits.append)
    items = supplementary.paged_all(make_ctx(tmp_path, local_api["base"]), f"{local_api['base']}/alerts")
    assert [i["n"] for i in items] == [1, 2, 3, 4]
    hits = local_api["hits"]
    assert len(hits) == 4                                   # one throttled try, then three pages
    assert hits[0] == hits[1] and "after=c1" in hits[2] and "after=c2" in hits[3]
    assert any("429" in line and "waiting 1s" in line for line in waits)
    assert not any("page=" in h.replace("per_page=", "") for h in hits)    # never guesses page numbers


def test_a_link_to_another_host_is_not_followed_over_real_http(local_api, tmp_path):
    with pytest.raises(MigrationError, match="another host"):
        supplementary.paged_all(make_ctx(tmp_path, local_api["base"]), f"{local_api['base']}/evil")
    assert all(h.startswith("/evil") for h in local_api["hits"])


def test_a_permission_403_comes_back_at_once_with_its_message(local_api):
    with pytest.raises(MigrationError) as err:
        mig._json_request(f"{local_api['base']}/forbidden", headers={})
    assert local_api["hits"] == ["/forbidden"]
    assert "personal access token" in mig.brief_error(err.value)
