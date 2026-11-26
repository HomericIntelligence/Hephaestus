"""Bounded startup update notice for Hephaestus console entry points.

A console script calls :func:`check_for_updates` when it starts work. The helper
reads the installed distribution metadata, fetches release metadata with a short
timeout, and writes one notice to stderr when a newer stable release exists.
Importing this module has no side effects. The helper never installs a package
and never changes the process exit code.
"""

from __future__ import annotations

import json
import logging
import shlex
import sys
import urllib.request
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError, distribution, version as distribution_version
from typing import Any, TextIO

from packaging.version import InvalidVersion, Version

__all__ = [
    "DEFAULT_INDEX_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "DIST_NAME",
    "check_for_updates",
    "format_notice",
    "latest_stable_version",
    "reset_check_state",
]

DIST_NAME = "HomericIntelligence-Hephaestus"
DEFAULT_INDEX_URL = "https://pypi.org/pypi/HomericIntelligence-Hephaestus/json"
DEFAULT_TIMEOUT_SECONDS = 2.0
_MAX_RESPONSE_BYTES = 1_048_576
_USER_AGENT = "hephaestus-startup-update-check/1"

_Fetcher = Callable[[str, float], Mapping[str, Any]]

logger = logging.getLogger(__name__)

# One invocation may route through more than one entry function. Record the
# attempt before any I/O so a nested call cannot repeat the notice or the fetch.
_check_completed = False


def _fetch_release_metadata(url: str, timeout: float) -> Mapping[str, Any]:
    """Fetch and decode size-bounded release metadata from the package index."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise ValueError("release metadata exceeds the response size cap")
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("release metadata is not a JSON object")
    return decoded


def _stable_version(raw: object) -> Version | None:
    """Return a final public release version, or None for an unusable value."""
    if not isinstance(raw, str):
        return None
    try:
        candidate = Version(raw)
    except InvalidVersion:
        return None
    if candidate.is_prerelease or candidate.is_devrelease or candidate.local is not None:
        return None
    return candidate


def latest_stable_version(metadata: Mapping[str, Any]) -> Version | None:
    """Select the highest stable release from controlled index metadata.

    Pre-release, development, and local versions from the release map are
    ignored. The ``info.version`` value is used as a fallback when the release
    map is absent.
    """
    releases = metadata.get("releases")
    candidates: list[Version] = []
    if isinstance(releases, Mapping):
        for raw in releases:
            parsed = _stable_version(raw)
            if parsed is not None:
                candidates.append(parsed)
    info = metadata.get("info")
    if isinstance(info, Mapping):
        from_info = _stable_version(info.get("version"))
        if from_info is not None:
            candidates.append(from_info)
    return max(candidates, default=None)


def _metadata_version() -> str | None:
    """Return the installed distribution version, or None without metadata."""
    try:
        return distribution_version(DIST_NAME)
    except PackageNotFoundError:
        return None


def _is_editable_install() -> bool:
    """Return True when the active distribution points at an editable source."""
    try:
        direct_url = distribution(DIST_NAME).read_text("direct_url.json")
    except (PackageNotFoundError, OSError):
        return False
    if not direct_url:
        return False
    try:
        payload = json.loads(direct_url)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    dir_info = payload.get("dir_info")
    return isinstance(dir_info, dict) and bool(dir_info.get("editable"))


def _resolve_current_version(explicit: str | None) -> Version | None:
    """Return the installed stable release version, or None when not checkable."""
    parsed = _stable_version(explicit if explicit is not None else _metadata_version())
    if parsed is None or _is_editable_install():
        return None
    return parsed


def format_notice(current: Version, latest: Version, interpreter: str) -> str:
    """Render the stderr notice with the active interpreter and update command."""
    command = f"{shlex.quote(interpreter)} -m pip install --upgrade {DIST_NAME}"
    return (
        f"A newer release of {DIST_NAME} is available: {latest} (installed: {current}).\n"
        f"Interpreter: {interpreter}\n"
        f"Update with: {command}\n"
    )


def reset_check_state() -> None:
    """Clear the once-per-invocation gate so a later invocation can check again."""
    global _check_completed
    _check_completed = False


def check_for_updates(
    *,
    stream: TextIO | None = None,
    fetcher: _Fetcher | None = None,
    current_version: str | None = None,
    interpreter: str | None = None,
    url: str = DEFAULT_INDEX_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Emit at most one update notice to stderr for this invocation.

    Args:
        stream: Output stream. Defaults to ``sys.stderr``.
        fetcher: Controlled metadata source for tests. Defaults to a bounded
            HTTP read of the package index.
        current_version: Override for the installed version. Defaults to the
            installed distribution metadata.
        interpreter: Override for the active interpreter path.
        url: Package index metadata URL.
        timeout: Network timeout in seconds.

    Returns:
        True when this call wrote a notice, False otherwise. Every failure path
        returns False without raising or writing a notice.

    """
    global _check_completed
    if _check_completed:
        return False
    _check_completed = True

    try:
        current = _resolve_current_version(current_version)
        if current is None:
            return False
        source = fetcher if fetcher is not None else _fetch_release_metadata
        metadata = source(url, timeout)
        latest = latest_stable_version(metadata)
        if latest is None or latest <= current:
            return False
        target = stream if stream is not None else sys.stderr
        target.write(format_notice(current, latest, interpreter or sys.executable))
        return True
    except Exception as error:
        # A startup notice is advisory. Keep the cause visible at debug level
        # without adding stderr output or changing the caller's exit status.
        logger.debug("startup update check skipped: %s", error)
        return False
