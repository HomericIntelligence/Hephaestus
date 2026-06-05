"""Internal helper: resolve the installed distribution version.

Single source of truth for the `__version__` lookup used by both
`hephaestus/__init__.py` and `hephaestus/cli/utils.py`. Kept as a private
leaf module so importers do not need to evaluate `hephaestus/__init__.py`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# The PyPI distribution name is "HomericIntelligence-Hephaestus", which
# importlib.metadata does NOT normalize to the import name "hephaestus".
_DIST_NAME = "HomericIntelligence-Hephaestus"

__all__ = ["get_version"]


def get_version() -> str:
    """Return the installed distribution version, or ``"unknown"`` if absent.

    Returns:
        The version string from package metadata, or the literal ``"unknown"``
        when the distribution is not installed (e.g. running from a source
        checkout without an editable install).

    """
    try:
        return _pkg_version(_DIST_NAME)
    except PackageNotFoundError:
        return "unknown"
