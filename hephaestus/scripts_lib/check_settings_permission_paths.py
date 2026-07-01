#!/usr/bin/env python3
r"""Guard: permission paths in ``.claude/settings.json`` are canonical POSIX paths.

An audit (issue #1495) found a ``Read(//home/...)`` double-slash prefix in a
machine-local settings file. Double-slash or backslash prefixes may match
differently than intended under prefix-based allowlist matching, silently
granting or denying access the author did not intend (POLA). This guard makes
any such artifact in the *tracked* ``.claude/settings.json`` fail CI.

The audit's named file — ``.claude/settings.local.json`` — is gitignored,
untracked, and absent from the repository, so it cannot be edited in a PR. The
durable, reviewable fix is therefore a guard over the tracked settings file:
any future ``//``- or ``\``-prefixed permission path lands where a reviewer
and CI can catch it.

This invariant is enforced at CI time via the unit test
``tests/unit/scripts_lib/test_check_settings_permission_paths.py::TestMain::test_passes_on_real_tracked_settings``,
which asserts that ``main()`` exits with status ``0`` against the tracked
``.claude/settings.json``. This test-based enforcement is the deliberate protection
mechanism in lieu of a pre-commit hook.

Usage:
    python3 -m hephaestus.scripts_lib.check_settings_permission_paths
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from hephaestus.constants import repo_root

# Permission tools whose single argument is a filesystem path/glob. Only these
# are validated; command tools (Bash, WebFetch, ...) legitimately contain ``//``
# (URLs, pipes) that are not filesystem paths.
_PATH_SCOPED_TOOLS = ("Read", "Write", "Edit")
_ENTRY_RE = re.compile(r"^(?P<tool>[A-Za-z]+)\((?P<arg>.*)\)$")


def find_violations(settings: dict[str, Any]) -> list[str]:
    """Return a sorted list of non-canonical permission-path violations.

    Scans the ``allow``/``deny``/``ask`` permission buckets for entries of the
    form ``Tool(path)`` where ``Tool`` is a path-scoped tool (``Read``,
    ``Write``, ``Edit``) and flags any whose path argument contains ``//`` or a
    backslash — i.e. is not a canonical POSIX path.

    Args:
        settings: Parsed ``.claude/settings.json`` contents.

    Returns:
        A sorted list of human-readable violation strings, one per offending
        entry. Empty when every path-scoped permission path is canonical.

    """
    violations: list[str] = []
    perms = settings.get("permissions", {})
    for bucket in ("allow", "deny", "ask"):
        for entry in perms.get(bucket, []):
            match = _ENTRY_RE.match(entry.strip())
            if not match or match.group("tool") not in _PATH_SCOPED_TOOLS:
                continue
            arg = match.group("arg")
            if "//" in arg or "\\" in arg:
                violations.append(f"{bucket}: {entry} (non-canonical path {arg!r})")
    return sorted(violations)


def main(argv: list[str] | None = None) -> int:
    """Validate the tracked ``.claude/settings.json`` and report violations.

    Args:
        argv: Unused; present for the standard console-entry-point signature.

    Returns:
        ``0`` when every permission path is canonical. ``1`` when a
        non-canonical path is found, or when the tracked settings file is
        absent or cannot be located.

    """
    settings_path = repo_root() / ".claude" / "settings.json"
    if not settings_path.exists():
        print(f"settings file not found: {settings_path}", file=sys.stderr)
        return 1
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    violations = find_violations(settings)
    if violations:
        print(
            "Non-canonical permission paths in .claude/settings.json "
            "(use canonical POSIX paths, no '//' or '\\'):",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
