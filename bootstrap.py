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

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

for entry in (str(HERE.parent), str(HERE)):   # workspace root wins
    if entry not in sys.path:
        sys.path.insert(0, entry)

_vendor = str(HERE / "vendor")                # container fallback, last resort
if _vendor not in sys.path:
    sys.path.append(_vendor)
