"""Single source of truth for every path and secret in the DataLabs workspace.

Three workspace-wide invariants; nothing may deviate from them:

1. **One env file** — `DataLabs/.env`. No per-skill `.env`, no `tokens` files.
2. **One outputs root** — `DataLabs/outputs/<component>/...`. Every skill and
   script writes into its own subdirectory there and nowhere else.
3. **One repo store** — `DataLabs/clones/...`. Every skill and script clones
   into it and reads existing clones from it.

Every consumer imports from here instead of recomputing paths, so relocating
any of the three is a one-line change (or an env var override).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, Optional

DATALABS_ROOT = Path(__file__).resolve().parent

# ── 1. the one env file ──────────────────────────────────────────────────────

# Override with DATALABS_ENV_FILE to point at a different secrets file.
ENV_FILE = Path(
    os.environ.get("DATALABS_ENV_FILE", str(DATALABS_ROOT / ".env"))
).resolve()

_ENV_LOADED = False


def parse_env_file(path: Optional[Path] = None) -> Dict[str, str]:
    """Parse `KEY=value` lines. Comments, blanks and quotes are tolerated."""
    path = Path(path) if path else ENV_FILE
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_env(path: Optional[Path] = None, override: bool = False) -> Dict[str, str]:
    """Load `.env` into os.environ. Real environment wins unless override=True.

    Keys that are not valid shell identifiers (the legacy hyphenated token
    names) are returned but never exported.
    """
    global _ENV_LOADED
    values = parse_env_file(path)
    for key, value in values.items():
        if not key.replace("_", "").isalnum():
            continue
        if override or key not in os.environ:
            os.environ[key] = value
    _ENV_LOADED = True
    return values


def env(key: str, *fallbacks: str, default: str = "") -> str:
    """Read a secret: os.environ first, then `.env`, then `default`."""
    for name in (key, *fallbacks):
        value = os.environ.get(name)
        if value:
            return value
    values = parse_env_file()
    for name in (key, *fallbacks):
        value = values.get(name)
        if value:
            return value
    return default


def github_token() -> str:
    return env("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PAT")


def gitlab_token() -> str:
    return env("GITLAB_TOKEN", "GITLAB_PAT", "GL_TOKEN", "CI_JOB_TOKEN")


def anthropic_key() -> str:
    return env("ANTHROPIC_API_KEY")


def openai_key() -> str:
    return env("OPENAI_API_KEY")


# ── 2. the one outputs root ──────────────────────────────────────────────────

# Override with DATALABS_OUTPUTS_DIR to relocate all run artifacts.
OUTPUTS_ROOT = Path(
    os.environ.get("DATALABS_OUTPUTS_DIR", str(DATALABS_ROOT / "outputs"))
).resolve()


def outputs_for(component: str, *parts: str) -> Path:
    """`outputs/<component>/<parts...>` — the only legal place to write.

    Pure path arithmetic, no side effects, so it is safe as a module-level
    default. `component` is the skill or script name, e.g.
    `outputs_for("repo-metrics", "run1")`.
    """
    return OUTPUTS_ROOT.joinpath(component, *parts)


def ensure_outputs(component: str, *parts: str) -> Path:
    """outputs_for(), with the directory created."""
    path = outputs_for(component, *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_parent(path: Path) -> Path:
    """Create the parent directory of a file path about to be written."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return Path(path)


# ── 3. the one repo store ────────────────────────────────────────────────────

# Override with DATALABS_CLONES_DIR to relocate the shared clone cache.
CLONES_ROOT = Path(
    os.environ.get("DATALABS_CLONES_DIR", str(DATALABS_ROOT / "clones"))
).resolve()


def clones_github() -> Path:
    return CLONES_ROOT / "github"


def clones_gitlab() -> Path:
    return CLONES_ROOT / "gitlab"


def clones_batch(name: str) -> Path:
    """Org/batch-specific working clones (e.g. lh2-share, tactconnect)."""
    return CLONES_ROOT / "batches" / name


def clones_bare() -> Path:
    return CLONES_ROOT / "bare"


def default_clone_search_roots() -> list[Path]:
    """Directories scanned for existing local clones before re-cloning."""
    return [CLONES_ROOT]


def ensure_clone_dirs() -> None:
    clones_github().mkdir(parents=True, exist_ok=True)
    clones_gitlab().mkdir(parents=True, exist_ok=True)
    clones_bare().mkdir(parents=True, exist_ok=True)
    (CLONES_ROOT / "batches").mkdir(parents=True, exist_ok=True)


# ── shared tooling ───────────────────────────────────────────────────────────

TOOLS_ROOT = DATALABS_ROOT / ".tools"


def tool(name: str) -> Path:
    """Resolve a bundled binary: `.tools/bin/<name>` then `.tools/<name>`."""
    for candidate in (TOOLS_ROOT / "bin" / name, TOOLS_ROOT / name):
        if candidate.exists():
            return candidate
    return Path(name)


def tools_path_entries() -> Iterable[str]:
    """PATH entries that expose the bundled toolchain (scc, node, jscpd, ...)."""
    return [
        str(TOOLS_ROOT / "bin"),
        str(TOOLS_ROOT),
        str(TOOLS_ROOT / "node-v22.11.0-darwin-arm64" / "bin"),
    ]


__all__ = [
    "DATALABS_ROOT", "ENV_FILE", "OUTPUTS_ROOT", "CLONES_ROOT", "TOOLS_ROOT",
    "parse_env_file", "load_env", "env",
    "github_token", "gitlab_token", "anthropic_key", "openai_key",
    "outputs_for", "ensure_outputs", "ensure_parent",
    "clones_github", "clones_gitlab", "clones_batch", "clones_bare",
    "default_clone_search_roots", "ensure_clone_dirs",
    "tool", "tools_path_entries",
]
