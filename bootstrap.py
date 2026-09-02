"""Import path setup, shared by app.py / cli.py / runner.py.

`datalabs_paths` is the workspace's single definition of the three invariants
(one env file, one outputs root, one repo store). It is resolved in this order:

1. the DataLabs workspace root, one level up — the real thing, when running
   on the host;
2. `vendor/` — a refreshed copy, so the Docker image (which has no workspace
   root above it) is self-contained.

The workspace root is searched first, so the vendored copy can never shadow it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STANDALONE = not (HERE.parent / "datalabs_paths.py").is_file()

# Standalone checkout: `datalabs_paths` resolves to vendor/, which would put its
# notion of the root inside vendor/ and scatter outputs there. Anchor the three
# invariants on the component directory instead — unless the caller (or the
# Dockerfile) already set them, which always wins.
if STANDALONE:
    os.environ.setdefault("DATALABS_ENV_FILE", str(HERE / ".env"))
    os.environ.setdefault("DATALABS_OUTPUTS_DIR", str(HERE / "outputs"))
    os.environ.setdefault("DATALABS_CLONES_DIR", str(HERE / "clones"))

for entry in (str(HERE.parent), str(HERE)):   # workspace root wins
    if entry not in sys.path:
        sys.path.insert(0, entry)

_vendor = str(HERE / "vendor")                # container fallback, last resort
if _vendor not in sys.path:
    sys.path.append(_vendor)
