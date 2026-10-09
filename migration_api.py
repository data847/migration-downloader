"""GitHub + GitLab migration/export API clients.

Implements the four-step flows from *Coding Pilot - Upload Instructions v1.0*:

GitHub (user or org)          GitLab (project)
  1. PAT (supplied)             1. PAT (supplied)
  2. POST  .../migrations       2. POST  /projects/:path/export
  3. GET   .../migrations/:id   3. GET   /projects/:path/export
  4. GET   .../:id/archive      4. GET   /projects/:id/export/download

Stdlib only (urllib) so the component needs nothing but Flask for its UI.
Tokens are never written to the log stream.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional

# Header versions exactly as the instruction document specifies them.
GITHUB_API_VERSION_USER = "2022-11-28"
GITHUB_API_VERSION_ORG = "2026-03-10"
GITHUB_API_BASE = "https://api.github.com"
# `exclude_<name>` booleans accepted by POST .../migrations
GITHUB_EXCLUDE_OPTIONS = frozenset(
    {"metadata", "git_data", "attachments", "releases", "owner_projects"}
)
GITLAB_API_BASE = "https://gitlab.com"

DOWNLOAD_CHUNK = 1024 * 1024

Logger = Callable[[str], None]

import ratelimit  # noqa: E402
from errors import MigrationError  # noqa: E402,F401  (re-exported for every importer)
from safety import http_url, require_same_origin  # noqa: E402


def _noop(_message: str) -> None:
    pass


def brief_error(exc: BaseException) -> str:
    """One line for logs and manifests: the failing request, plus the API's own
    explanation (`— Must be an organization owner`) when it sent one."""
    lines = str(exc).splitlines()
    head = lines[0] if lines else str(exc)
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            data = None
        message = ""
        if isinstance(data, dict):
            err = data.get("error")
            message = str(data.get("message") or (err if isinstance(err, str) else "")
                          or (err.get("message") if isinstance(err, dict) else "") or "")
        return f"{head} — {(message or line)[:200]}"
    return head


def mask(token: str) -> str:
    if not token:
        return "<empty>"
    return f"{token[:4]}…{token[-4:]} ({len(token)} chars)" if len(token) > 12 else "<short token>"


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Archive/export downloads 302 to a presigned storage URL (S3, codeload…).

    urllib's default handler replays every original header on the redirect,
    including our GitHub/GitLab `Authorization`. Presigned URLs already carry
    their own auth in the query string, so a second `Authorization` header
    makes the storage host reject the request (e.g. S3's "only one auth
    mechanism allowed", surfaced here as a plain HTTP 400). Strip the
    sensitive headers whenever the redirect target isn't the original host.
    """

    _STRIP = ("Authorization", "Accept", "X-Github-Api-Version", "Private-Token")

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        if urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            for header in self._STRIP:
                # Request.remove_header() doesn't capitalize its argument the
                # way add_header() capitalized it when storing the key.
                new_req.remove_header(header.capitalize())
        return new_req


_opener = urllib.request.build_opener(_SafeRedirectHandler)


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict,
    body: Optional[bytes] = None,
    timeout: int = 60,
):
    """Open a request, waiting out rate limits and retrying transient failures.

    POSTs are only retried when the server clearly did not act (429, rate-limit
    403), never after a 5xx or a dropped connection: a repeat could start a
    second migration.
    """
    http_url(url)
    host = urllib.parse.urlparse(url).netloc
    safe_to_repeat = method in {"GET", "HEAD", "DELETE"}
    attempt = 0
    while True:
        ratelimit.before(host)
        req = urllib.request.Request(url, data=body, method=method)
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            resp = _opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raw = b""
            if exc.code == 403:             # the body says whether it was a limit
                raw = exc.read()
            transient = exc.code >= 500
            delay = None if (transient and not safe_to_repeat) else ratelimit.delay_for(
                exc.code, exc.headers, raw.decode("utf-8", "replace")[:400], attempt)
            if delay is None:
                if raw:                     # hand the body on for the error message
                    raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.headers, io.BytesIO(raw)) from None
                raise
            try:
                ratelimit.wait(delay, f"HTTP {exc.code} from {host}")
            except ratelimit.Cancelled:
                raise MigrationError("cancelled while waiting out a rate limit") from None
            attempt += 1
            continue
        except urllib.error.URLError as exc:
            delay = ratelimit.delay_for_network(attempt) if safe_to_repeat else None
            if delay is None:
                raise
            try:
                ratelimit.wait(delay, f"network error talking to {host} ({exc.reason})")
            except ratelimit.Cancelled:
                raise MigrationError("cancelled while retrying") from None
            attempt += 1
            continue
        ratelimit.observe(host, resp.headers)
        return resp


def _read_json(url: str, method: str, headers: dict, payload, timeout: int):
    body = json.dumps(payload).encode() if payload is not None else None
    if body is not None:
        headers = {**headers, "Content-Type": "application/json"}
    try:
        with _request(url, method=method, headers=headers, body=body, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:  # surface GitHub/GitLab's own message
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise MigrationError(f"HTTP {exc.code} {method} {url}\n{detail}") from None
    except urllib.error.URLError as exc:
        raise MigrationError(f"network error {method} {url}: {exc.reason}") from None
    if not raw.strip():
        return status, {}, hdrs
    try:
        return status, json.loads(raw), hdrs
    except json.JSONDecodeError:
        return status, {"raw": raw.decode("utf-8", "replace")}, hdrs


def _json_request(url: str, *, method: str = "GET", headers: dict, payload=None, timeout: int = 60):
    status, data, _hdrs = _read_json(url, method, headers, payload, timeout)
    return status, data


def next_link(link_header: Optional[str]) -> Optional[str]:
    """The `rel="next"` URL from an RFC 8288 Link header, if there is one."""
    for part in (link_header or "").split(","):
        pieces = part.split(";")
        if len(pieces) > 1 and any(p.strip().replace(" ", "") == 'rel="next"' for p in pieces[1:]):
            return pieces[0].strip().strip("<>")
    return None


def _json_page(url: str, *, headers: dict, timeout: int = 60):
    """One GET: (data, next page URL or None, lower-cased response headers).

    Following `Link: rel=next` is the only paging that works for every
    endpoint: some take `page=N`, others (Dependabot, secret scanning) only
    cursors, and a wrong guess silently repeats the same page.
    """
    _status, data, hdrs = _read_json(url, "GET", headers, None, timeout)
    return data, next_link(hdrs.get("link")), hdrs


def _download(url: str, dest: Path, *, headers: dict, log: Logger, timeout: int = 120,
              info: Optional[dict] = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    try:
        with _request(url, headers=headers, timeout=timeout) as resp, partial.open("wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            if info is not None:
                info["content_length"] = total or ""
            done = 0
            next_mark = 0
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done * 100 / total)
                    if pct >= next_mark:
                        log(f"    downloading… {pct}% ({human(done)} / {human(total)})")
                        next_mark = pct - pct % 10 + 10
                elif done >= next_mark:
                    log(f"    downloading… {human(done)}")
                    next_mark = done + 25 * 1024 * 1024
    except urllib.error.HTTPError as exc:
        partial.unlink(missing_ok=True)
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise MigrationError(f"HTTP {exc.code} downloading archive\n{detail}") from None
    except urllib.error.URLError as exc:
        partial.unlink(missing_ok=True)
        raise MigrationError(f"network error downloading archive: {exc.reason}") from None
    partial.replace(dest)
    return dest


def fetch_page(url: str, *, headers: dict, page: int = 1, per_page: int = 100):
    """One numbered page of a listing: (items, has_more, response headers)."""
    sep = "&" if "?" in url else "?"
    data, nxt, hdrs = _json_page(f"{url}{sep}per_page={per_page}&page={page}", headers=headers)
    batch = data if isinstance(data, list) else []
    more = bool(nxt) if "link" in hdrs else len(batch) >= per_page
    return batch, more, hdrs


def paged(url: str, *, headers: dict, per_page: int = 100, cap: Optional[int] = None) -> list:
    """Every item of a listing (both APIs cap `per_page` at 100), following the
    server's own next-page links. `cap` is optional and off by default."""
    items: list = []
    sep = "&" if "?" in url else "?"
    nxt: Optional[str] = f"{url}{sep}per_page={per_page}"
    while nxt:
        data, link, _hdrs = _json_page(nxt, headers=headers)
        batch = data if isinstance(data, list) else []
        items.extend(batch)
        if cap and len(items) >= cap:
            return items[:cap]
        nxt = require_same_origin(url, link) if link else None
    return items


SSO_NOTE = ("Some organizations are not shown: they require SAML SSO and this token is not "
            "authorized for them. Authorize it under GitHub → Settings → Developer settings → "
            "Personal access tokens → Configure SSO, then reload the list.")


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(DOWNLOAD_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-._" else "-" for ch in text).strip("-") or "archive"


@dataclass
class ArchiveResult:
    target: str
    path: Optional[Path] = None
    bytes: int = 0
    digest: str = ""
    migration_id: Optional[str] = None
    state: str = ""
    error: str = ""
    skipped: bool = False
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "path": str(self.path) if self.path else "",
            "bytes": self.bytes,
            "size": human(self.bytes) if self.bytes else "",
            "sha256": self.digest,
            "migration_id": self.migration_id,
            "state": self.state,
            "error": self.error,
            "skipped": self.skipped,
            "ok": (bool(self.path) or bool(self.meta.get("uploaded_to"))) and not self.error,
        }


# ── GitHub ───────────────────────────────────────────────────────────────────


class GitHubMigration:
    """Steps 2-4 of the GitHub Migration API, for a user or an org."""

    TERMINAL_OK = "exported"
    TERMINAL_BAD = {"failed", "failed_garbage_collecting"}

    def __init__(
        self,
        token: str,
        *,
        scope: str = "user",
        org: str = "",
        api_base: str = GITHUB_API_BASE,
        api_version: str = "",
        lock_repositories: bool = False,
        exclude: Iterable[str] = (),
        org_metadata_only: bool = False,
        log: Logger = _noop,
    ):
        if scope not in {"user", "org"}:
            raise MigrationError("scope must be 'user' or 'org'")
        if scope == "org" and not org:
            raise MigrationError("an organization name is required for org scope")
        if not token:
            raise MigrationError("a GitHub personal access token is required")
        self.token = token
        self.scope = scope
        self.org = org.strip("/")
        self.api_base = api_base.rstrip("/")
        self.api_version = api_version or (
            GITHUB_API_VERSION_USER if scope == "user" else GITHUB_API_VERSION_ORG
        )
        self.lock_repositories = lock_repositories
        bad = set(exclude) - GITHUB_EXCLUDE_OPTIONS
        if bad:
            raise MigrationError(
                f"unknown exclude option(s) {sorted(bad)}; choose from {sorted(GITHUB_EXCLUDE_OPTIONS)}"
            )
        if org_metadata_only and scope != "org":
            raise MigrationError("org_metadata_only is only valid for org scope")
        self.exclude = sorted(set(exclude))
        self.org_metadata_only = org_metadata_only
        self.log = log
        self.download_info: dict = {}

    # -- plumbing ----------------------------------------------------------
    @property
    def _headers(self) -> dict:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "DataLabs-migration-downloader",
        }

    @property
    def _root(self) -> str:
        return (
            f"{self.api_base}/user/migrations"
            if self.scope == "user"
            else f"{self.api_base}/orgs/{self.org}/migrations"
        )

    def normalise(self, repo: str) -> str:
        """user scope wants `owner/repo`; org scope wants the bare repo name."""
        repo = repo.strip().strip("/")
        if repo.startswith(("http://", "https://")):
            repo = urllib.parse.urlparse(repo).path.strip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        if self.scope == "org":
            return repo.split("/")[-1]
        return repo

    def whoami(self) -> str:
        _status, data = _json_request(f"{self.api_base}/user", headers=self._headers)
        return data.get("login", "?")

    # -- discovery ---------------------------------------------------------
    @staticmethod
    def _repo_row(r: dict, bare: bool = False) -> dict:
        return {
            # org scope migrates bare names, everything else owner/repo
            "target": r.get("name") if bare else r.get("full_name"),
            "label": r.get("full_name") or r.get("name"),
            "private": bool(r.get("private")),
            "archived": bool(r.get("archived")),
            "size_kb": r.get("size") or 0,
            "updated": (r.get("pushed_at") or r.get("updated_at") or "")[:10],
        }

    def _repos_url(self, org: str = "", everything: bool = False) -> str:
        if org:
            return f"{self.api_base}/orgs/{org.strip('/')}/repos?type=all&sort=updated"
        affiliation = "owner,collaborator,organization_member" if everything else "owner,collaborator"
        return f"{self.api_base}/user/repos?affiliation={affiliation}&sort=updated"

    def repos_page(self, page: int = 1, *, org: str = "", everything: bool = False) -> dict:
        """One page of repos for the lazy-loading picker, plus anything the user should know."""
        batch, more, hdrs = fetch_page(self._repos_url(org, everything), headers=self._headers, page=page)
        notes = [SSO_NOTE] if hdrs.get("x-github-sso", "").startswith("partial-results") else []
        return {"items": [self._repo_row(r, bare=bool(org)) for r in batch if r.get("name")],
                "has_more": more, "notes": notes}

    def orgs_page(self, page: int = 1) -> dict:
        batch, more, _hdrs = fetch_page(f"{self.api_base}/user/orgs", headers=self._headers, page=page)
        return {"items": [{"name": o.get("login", ""), "description": o.get("description") or ""}
                          for o in batch if o.get("login")], "has_more": more, "notes": []}

    def list_orgs(self) -> List[dict]:
        """Orgs the token can see. Migration needs Owner or the Migrator role,
        which cannot be read from here — so every org is listed and a 403 on
        initiate is the real answer."""
        orgs = paged(f"{self.api_base}/user/orgs", headers=self._headers)
        return [{"name": o.get("login", ""), "description": o.get("description") or ""}
                for o in orgs if o.get("login")]

    def list_repos(self, org: str = "") -> List[dict]:
        """Repos visible to the token — the whole org's when `org` is given,
        otherwise the ones the user owns or collaborates on."""
        return [self._repo_row(r, bare=bool(org))
                for r in paged(self._repos_url(org), headers=self._headers) if r.get("name")]

    def list_all_repos(self, cap: Optional[int] = None) -> List[dict]:
        """Personal repos plus every org repo the token can reach, as owner/repo.

        One call covers both because `organization_member` adds the orgs'
        repos to what the user owns or collaborates on.
        """
        return [self._repo_row(r)
                for r in paged(self._repos_url(everything=True), headers=self._headers, cap=cap)
                if r.get("full_name")]

    # -- step 2 ------------------------------------------------------------
    def start(self, repos: Iterable[str]) -> str:
        names = [self.normalise(r) for r in repos if r.strip()]
        if not names:
            raise MigrationError("no repositories given")
        payload = {"lock_repositories": self.lock_repositories, "repositories": names}
        payload.update({f"exclude_{name}": True for name in self.exclude})
        if self.org_metadata_only:
            payload["org_metadata_only"] = True
        self.log(f"  POST {self._root}  repositories={names}")
        _status, data = _json_request(self._root, method="POST", headers=self._headers, payload=payload)
        migration_id = data.get("id")
        if not migration_id:
            raise MigrationError(f"no migration id in response: {json.dumps(data)[:400]}")
        self.log(f"  migration id = {migration_id} (state {data.get('state')})")
        return str(migration_id)

    # -- step 3 ------------------------------------------------------------
    def status(self, migration_id: str) -> dict:
        _status, data = _json_request(f"{self._root}/{migration_id}", headers=self._headers)
        return data

    def wait(self, migration_id: str, *, interval: int = 15, timeout: int = 3600, cancelled=None) -> dict:
        deadline = time.time() + timeout
        last = ""
        while True:
            if cancelled and cancelled():
                raise MigrationError("cancelled while waiting for the archive")
            data = self.status(migration_id)
            state = data.get("state", "?")
            if state != last:
                self.log(f"  state = {state}")
                last = state
            if state == self.TERMINAL_OK:
                return data
            if state in self.TERMINAL_BAD:
                raise MigrationError(f"migration {migration_id} ended in state '{state}'")
            if time.time() > deadline:
                raise MigrationError(
                    f"timed out after {timeout}s waiting for migration {migration_id} (last state '{state}')"
                )
            self._sleep(interval, cancelled)

    @staticmethod
    def _sleep(seconds: int, cancelled=None) -> None:
        for _ in range(max(1, seconds)):
            if cancelled and cancelled():
                return
            time.sleep(1)

    # -- step 4 ------------------------------------------------------------
    def download(self, migration_id: str, dest: Path) -> Path:
        url = f"{self._root}/{migration_id}/archive"
        self.log(f"  GET {url}")
        return _download(url, dest, headers=self._headers, log=self.log, info=self.download_info)

    def repositories(self, migration_id: str) -> List[str]:
        """Repos the source says this migration contains (`owner/name` when given)."""
        _status, data = _json_request(f"{self._root}/{migration_id}/repositories", headers=self._headers)
        return [r.get("full_name") or r.get("name") for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    # -- housekeeping (read-only listings, plus two opt-in mutations) ------
    def list_migrations(self) -> list:
        return paged(self._root, headers=self._headers)

    def migration_repositories(self, migration_id: str) -> list:
        return paged(f"{self._root}/{migration_id}/repositories", headers=self._headers)

    def delete_archive(self, migration_id: str) -> None:
        """Removes the archive from GitHub. Irreversible, so callers must opt in."""
        url = f"{self._root}/{migration_id}/archive"
        self.log(f"  DELETE {url}")
        _json_request(url, method="DELETE", headers=self._headers)

    def unlock_repo(self, migration_id: str, repo_name: str) -> None:
        """Releases the lock that `lock_repositories` placed on a repo."""
        name = repo_name.strip("/").split("/")[-1]
        url = f"{self._root}/{migration_id}/repos/{urllib.parse.quote(name, safe='')}/lock"
        self.log(f"  DELETE {url}")
        _json_request(url, method="DELETE", headers=self._headers)


# ── GitLab ───────────────────────────────────────────────────────────────────


class GitLabExport:
    """Steps 2-4 of the GitLab project export API."""

    TERMINAL_OK = "finished"
    TERMINAL_BAD = {"none", "regeneration_in_progress_failed"}

    def __init__(self, token: str, *, api_base: str = GITLAB_API_BASE, log: Logger = _noop):
        if not token:
            raise MigrationError("a GitLab personal access token is required")
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.log = log
        self.download_info: dict = {}

    @property
    def _headers(self) -> dict:
        return {"PRIVATE-TOKEN": self.token, "User-Agent": "DataLabs-migration-downloader"}

    def normalise(self, project: str) -> str:
        project = project.strip().strip("/")
        if project.startswith(("http://", "https://")):
            project = urllib.parse.urlparse(project).path.strip("/")
        if project.endswith(".git"):
            project = project[:-4]
        return project

    def _project_url(self, project: str, suffix: str = "") -> str:
        ident = project if project.isdigit() else urllib.parse.quote(project, safe="")
        return f"{self.api_base}/api/v4/projects/{ident}{suffix}"

    def whoami(self) -> str:
        _status, data = _json_request(f"{self.api_base}/api/v4/user", headers=self._headers)
        return data.get("username", "?")

    # -- discovery ---------------------------------------------------------
    # exporting a project needs Maintainer (40) or above, so discovery filters
    # to that level rather than listing projects that would 403 on initiate
    MIN_ACCESS_LEVEL = 40

    def _groups_url(self) -> str:
        return f"{self.api_base}/api/v4/groups?min_access_level={self.MIN_ACCESS_LEVEL}&all_available=false"

    def _projects_url(self, group: str = "") -> str:
        if group:
            ident = group if group.isdigit() else urllib.parse.quote(group.strip("/"), safe="")
            return (f"{self.api_base}/api/v4/groups/{ident}/projects"
                    f"?include_subgroups=true&min_access_level={self.MIN_ACCESS_LEVEL}"
                    f"&order_by=last_activity_at")
        return (f"{self.api_base}/api/v4/projects"
                f"?membership=true&min_access_level={self.MIN_ACCESS_LEVEL}"
                f"&order_by=last_activity_at")

    @staticmethod
    def _project_row(p: dict) -> dict:
        return {
            "target": p.get("path_with_namespace", ""),
            "label": p.get("path_with_namespace", ""),
            "private": p.get("visibility") != "public",
            "archived": bool(p.get("archived")),
            "size_kb": 0,
            "updated": (p.get("last_activity_at") or "")[:10],
        }

    def groups_page(self, page: int = 1) -> dict:
        batch, more, _hdrs = fetch_page(self._groups_url(), headers=self._headers, page=page)
        return {"items": [{"name": g.get("full_path", ""), "description": g.get("description") or ""}
                          for g in batch if g.get("full_path")], "has_more": more, "notes": []}

    def projects_page(self, page: int = 1, group: str = "") -> dict:
        batch, more, _hdrs = fetch_page(self._projects_url(group), headers=self._headers, page=page)
        return {"items": [self._project_row(p) for p in batch if p.get("path_with_namespace")],
                "has_more": more, "notes": []}

    def list_groups(self) -> List[dict]:
        return [{"name": g.get("full_path", ""), "description": g.get("description") or ""}
                for g in paged(self._groups_url(), headers=self._headers) if g.get("full_path")]

    def list_projects(self, group: str = "") -> List[dict]:
        """Projects the token could export — optionally only inside one group.

        Works the same against gitlab.com and a self-hosted instance; the only
        difference is `api_base`.
        """
        return [self._project_row(p)
                for p in paged(self._projects_url(group), headers=self._headers)
                if p.get("path_with_namespace")]

    # -- step 2 ------------------------------------------------------------
    def start(
        self,
        project: str,
        *,
        upload_url: str = "",
        upload_method: str = "",
        description: str = "",
    ) -> None:
        """`upload_url` makes GitLab push the finished archive there itself."""
        url = self._project_url(project, "/export")
        payload: dict = {}
        if description:
            payload["description"] = description
        if upload_url:
            payload["upload"] = {"url": upload_url, "http_method": upload_method or "PUT"}
        self.log(f"  POST {url}" + ("  (GitLab will upload the archive itself)" if upload_url else ""))
        status, _data = _json_request(
            url, method="POST", headers=self._headers, payload=payload or None
        )
        self.log(f"  HTTP {status} — export scheduled" if status in (200, 202) else f"  HTTP {status}")

    # -- step 3 ------------------------------------------------------------
    def status(self, project: str) -> dict:
        _status, data = _json_request(self._project_url(project, "/export"), headers=self._headers)
        return data

    def wait(self, project: str, *, interval: int = 15, timeout: int = 3600, cancelled=None) -> dict:
        deadline = time.time() + timeout
        last = ""
        while True:
            if cancelled and cancelled():
                raise MigrationError("cancelled while waiting for the export")
            data = self.status(project)
            state = data.get("export_status", "?")
            if state != last:
                self.log(f"  export_status = {state}")
                last = state
            if state == self.TERMINAL_OK:
                return data
            if state in self.TERMINAL_BAD and last:
                raise MigrationError(f"export of {project} ended in state '{state}'")
            if time.time() > deadline:
                raise MigrationError(
                    f"timed out after {timeout}s waiting for the export of {project} (last state '{state}')"
                )
            GitHubMigration._sleep(interval, cancelled)

    # -- step 4 ------------------------------------------------------------
    def download(self, project: str, dest: Path, *, project_id: Optional[int] = None) -> Path:
        ident = str(project_id) if project_id else project
        url = self._project_url(str(ident), "/export/download")
        self.log(f"  GET {url}")
        return _download(url, dest, headers=self._headers, log=self.log, info=self.download_info)

    # -- relations export (direct-transfer format, one file per relation) ---
    RELATION_DONE, RELATION_FAILED = 2, 3

    def relations_export(self, project: str, dest_dir: Path, *, interval: int = 15,
                         timeout: int = 3600, cancelled=None) -> List[Path]:
        url = self._project_url(project, "/export_relations")
        self.log(f"  POST {url}")
        _json_request(url, method="POST", headers=self._headers)
        deadline = time.time() + timeout
        while True:
            if cancelled and cancelled():
                raise MigrationError("cancelled while waiting for the relations export")
            _status, rows = _json_request(f"{url}/status", headers=self._headers)
            rows = rows if isinstance(rows, list) else []
            failed = [r.get("relation") for r in rows if r.get("status") == self.RELATION_FAILED]
            if failed:
                raise MigrationError(f"relations export failed for {failed}")
            if rows and all(r.get("status") == self.RELATION_DONE for r in rows):
                break
            if time.time() > deadline:
                raise MigrationError(f"timed out waiting for the relations export of {project}")
            GitHubMigration._sleep(interval, cancelled)
        saved = []
        for row in rows:
            name = row.get("relation", "")
            dest = dest_dir / f"{safe_name(name)}.ndjson.gz"
            saved.append(_download(
                f"{url}/download?relation={urllib.parse.quote(name, safe='')}",
                dest, headers=self._headers, log=self.log))
        return saved
