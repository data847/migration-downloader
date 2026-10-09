"""Scrub secrets and local paths out of run logs before they leave the machine.

The runner never prints a raw token — `mask()` is used everywhere — but a log
line can still quote an API error body, a redirect URL or a header echo, and a
log the user forwards to someone else should not carry any of that. Everything
here is defence in depth: the log is filtered again on the way out.

What is removed:
  * provider tokens — GitHub (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`,
    `github_pat_`), GitLab (`glpat-`, `glcbt-`, `gldt-`, `glsoat-`),
    Slack/AWS/OpenAI/Anthropic key shapes, JWTs;
  * anything after `Authorization:`, `Bearer`, `PRIVATE-TOKEN:`, `token=`,
    `private_token=`, `access_token=`, `?key=`;
  * userinfo in URLs (`https://user:pw@host`);
  * the home directory, replaced with `~`, so a shared log does not name the
    machine's user.

Logs carry no repository content — only API URLs, states, sizes and file names.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable, List

_HOME = os.path.expanduser("~")

_PATTERNS = [
    # provider tokens
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "<redacted-github-token>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "<redacted-github-token>"),
    (re.compile(r"\b(?:glpat|glcbt|gldt|glsoat|glrt)-[A-Za-z0-9\-_]{10,}\b"), "<redacted-gitlab-token>"),
    # other common key shapes that could appear in a pasted error body
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "<redacted-slack-token>"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    (re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9\-_]{20,}\b"), "<redacted-api-key>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "<redacted-jwt>"),
    # header and query-parameter carriers
    (re.compile(r"(?i)\b(authorization|private-token|x-api-key)\s*[:=]\s*(?:bearer\s+)?\S+"), r"\1: <redacted>"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b((?:private_|access_|api_)?token|key|password|secret)=[^&\s\"']+"),
     r"\1=<redacted>"),
    # credentials embedded in a URL
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1<redacted>@"),
    # webhook URLs are bearer secrets: whoever holds the URL can post
    (re.compile(r"(https://hooks\.slack\.com/(?:services|workflows|triggers)/)[A-Za-z0-9/_-]+"), r"\1<redacted>"),
    (re.compile(r"(https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/)\d+/[A-Za-z0-9._-]+"), r"\1<redacted>"),
    (re.compile(r"(https://[A-Za-z0-9.-]+\.webhook\.office\.com/webhookb2/)[A-Za-z0-9@/_.-]+"), r"\1<redacted>"),
    (re.compile(r"(https://outlook\.office(?:365)?\.com/webhook/)[A-Za-z0-9@/_.-]+"), r"\1<redacted>"),
    # private key blocks pasted into a log
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<redacted-private-key>"),
]


def scrub_secrets(text: str) -> str:
    """Redact secrets only (collected files keep their paths intact)."""
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub(text: str) -> str:
    """Redact secrets and the home directory from one string."""
    text = scrub_secrets(text)
    if text and _HOME and _HOME != "/":
        text = text.replace(_HOME, "~")
    return text


def scrub_lines(lines: Iterable[str]) -> List[str]:
    return [scrub(line) for line in lines]


# ── collected data (extras) ──────────────────────────────────────────────────
#
# JSON from the APIs can carry live secrets in a field, not just in a string
# shape the patterns know: a secret-scanning alert's `secret` is the leaked
# value itself, a webhook's `config.secret` / `url` can be one. Keys that name a
# secret are blanked, every other string goes through the patterns.

_SECRET_KEY = re.compile(r"(?i)(secret|token|passw(or)?d|passwd|api[_-]?key|private[_-]?key|credential|authorization)")
# metadata that merely *mentions* a secret: `secret_type`, `token_scopes`, `secret_scanning_*` ...
_SECRET_KEY_OK = re.compile(
    r"(?i)(_type|_type_display_name|_types|_scopes?|_count|_at|_by|_enabled|_status|_state|_url|"
    r"_validity|_name)$|^secret_scanning|^has_|^token_type$")
REDACTED = "<redacted>"


def scrub_obj(obj: Any) -> Any:
    """A copy of parsed JSON with secret-named fields blanked and strings scrubbed."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            name = str(key)
            if (_SECRET_KEY.search(name) and not _SECRET_KEY_OK.search(name)
                    and isinstance(value, (str, int, float)) and not isinstance(value, bool)
                    and value not in ("", 0)):
                out[key] = REDACTED
            else:
                out[key] = scrub_obj(value)
        return out
    if isinstance(obj, list):
        return [scrub_obj(v) for v in obj]
    if isinstance(obj, str):
        return scrub_secrets(obj)
    return obj


TEXT_SUFFIXES = {"", ".log", ".txt", ".md", ".json", ".ndjson", ".yml", ".yaml", ".out"}
MAX_SCRUB_BYTES = 256 * 1024 * 1024        # per file / per zip, uncompressed


def scrub_text_file(path: Path) -> bool:
    """Scrub a text file in place. False when it was skipped (too big, binary)."""
    try:
        if path.stat().st_size > MAX_SCRUB_BYTES:
            return False
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            return False
        text = raw.decode("utf-8", "replace")
    except OSError:
        return False
    cleaned = scrub_secrets(text)
    if cleaned != text:
        path.write_text(cleaned, encoding="utf-8")
    return True


def scrub_zip(path: Path) -> bool:
    """Rewrite a zip with its text members scrubbed (CI log archives).

    Skips, leaving the file untouched, when the declared uncompressed size is
    over the cap, which also guards against zip bombs.
    """
    tmp_name = ""
    try:
        with zipfile.ZipFile(path) as zin:
            if sum(i.file_size for i in zin.infolist()) > MAX_SCRUB_BYTES:
                return False
            tmp = tempfile.NamedTemporaryFile(dir=path.parent, suffix=".scrub", delete=False)
            tmp.close()
            tmp_name = tmp.name
            with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    data = zin.read(info)
                    suffix = Path(info.filename).suffix.lower()
                    if not info.is_dir() and suffix in TEXT_SUFFIXES and b"\x00" not in data[:4096]:
                        data = scrub_secrets(data.decode("utf-8", "replace")).encode("utf-8")
                    zout.writestr(info.filename, data)
        os.replace(tmp_name, path)
        return True
    except (zipfile.BadZipFile, OSError):
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return False
