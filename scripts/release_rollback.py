#!/usr/bin/env python3
"""Preflight or rehearse an immutable release rollback."""

import sys
from pathlib import Path

# The release preflight runs directly from a fresh Actions checkout, before a
# development install exists. Keep this wrapper source-tree runnable there.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from hephaestus.ci.release_rollback import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
