"""Identify the executing automation package and its host runtime."""

from __future__ import annotations

import json
import re
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import hephaestus


def runtime_identity() -> dict[str, str | None]:
    """Return runtime facts without using the target repository revision.

    A commit is available only when installation metadata records a VCS origin.
    An editable checkout or a wheel without that metadata has no installed commit.
    """
    version = "unknown"
    commit = None
    try:
        package = distribution("HomericIntelligence-Hephaestus")
        version = package.version
        direct_url = json.loads(package.read_text("direct_url.json") or "{}")
        vcs = direct_url.get("vcs_info") if isinstance(direct_url, dict) else None
        candidate = vcs.get("commit_id") if isinstance(vcs, dict) else None
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{40,64}", candidate):
            commit = candidate
    except (PackageNotFoundError, OSError, ValueError):
        pass
    return {
        "launcher": str(Path(sys.argv[0]).absolute()),
        "interpreter": sys.executable,
        "prefix": sys.prefix,
        "distribution_version": version,
        "package_path": str(Path(hephaestus.__file__).resolve().parent),
        "installed_commit": commit,
    }


def require_virtual_environment(runtime: Path) -> None:
    """Reject a base Python environment before host runtime copying starts."""
    if not (runtime / "pyvenv.cfg").is_file():
        raise RuntimeError(
            "host_verification_runtime_unsupported: "
            "Use the automation launcher from a Python 3.13 virtual environment."
        )
