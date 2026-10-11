"""Checks that stop a bad input or a hostile API response doing damage.

None of these are clever; each closes one specific way things go wrong:

  * `same_origin`   a pagination link that points at another host would get our
                    token sent there, so links are only followed on the origin
                    the request started from
  * `safe_join`     a file name taken from an API response (asset, snippet,
                    patch) must not climb out of the run folder
  * `valid_target`  repo/project names end up in URLs, paths and git arguments
  * `check_disk`    bulk downloads and zip builds need room; fail before, not
                    halfway through
  * `http_url`      urllib will happily open `file://`; only http(s) is allowed
"""

from __future__ import annotations

import re
import shutil
import urllib.parse
from pathlib import Path

from errors import MigrationError

MIN_FREE_MB = 512

# owner/repo, group/sub/project, workspace/repo, PROJ/slug, or a numeric id
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~\-]*(?:/[A-Za-z0-9][A-Za-z0-9._~\-]*)*$")


def valid_target(target: str) -> bool:
    target = target.strip()
    return bool(target) and len(target) <= 300 and ".." not in target.split("/") and bool(_TARGET.match(target))


def require_valid_target(target: str) -> str:
    if not valid_target(target):
        raise MigrationError(
            f"'{target[:80]}' is not a valid repository name "
            "(letters, digits, . _ - ~ separated by /)")
    return target


def http_url(url: str) -> str:
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in {"http", "https"}:
        raise MigrationError(f"refusing to open a {scheme or 'schemeless'} URL")
    return url


def _origin(url: str):
    parts = urllib.parse.urlparse(url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower())
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


def same_origin(base: str, other: str) -> bool:
    return _origin(base) == _origin(other)


def require_same_origin(base: str, other: str) -> str:
    if not same_origin(base, other):
        raise MigrationError(
            f"refusing to follow a link to another host ({urllib.parse.urlparse(other).hostname}); "
            "the token is only sent to the host the request started on")
    return other


def safe_join(base: Path, rel: str) -> Path:
    """`base / rel`, guaranteed to stay inside `base`."""
    if not rel or rel.startswith(("/", "\\")) or "\x00" in rel:
        raise MigrationError(f"unsafe path '{rel[:60]}'")
    base = base.resolve()
    path = (base / rel).resolve()
    if base != path and base not in path.parents:
        raise MigrationError(f"path '{rel[:60]}' escapes the output folder")
    return path


def check_disk(path: Path, need_mb: int = MIN_FREE_MB, what: str = "this run") -> None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free_mb = shutil.disk_usage(probe).free // (1024 * 1024)
    if free_mb < need_mb:
        raise MigrationError(
            f"only {free_mb} MB free where {what} writes ({probe}); need at least {need_mb} MB")
