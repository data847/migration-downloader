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
from typing import Iterable, List

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
    (re.compile(r"(?i)\b(authorization|private-token|x-api-key)\s*[:=]\s*\S+"), r"\1: <redacted>"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b((?:private_|access_|api_)?token|key|password|secret)=[^&\s\"']+"),
     r"\1=<redacted>"),
    # credentials embedded in a URL
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1<redacted>@"),
]


def scrub(text: str) -> str:
    """Redact secrets and the home directory from one string."""
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    if _HOME and _HOME != "/":
        text = text.replace(_HOME, "~")
    return text


def scrub_lines(lines: Iterable[str]) -> List[str]:
    return [scrub(line) for line in lines]
