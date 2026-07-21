#!/usr/bin/env python3
r"""Validate that a commit message carries a DCO Signed-off-by trailer.

Enforces the Developer Certificate of Origin requirement documented in
CONTRIBUTING.md ("Developer Certificate of Origin (DCO)"): every commit must
include a Signed-off-by: Name <email> trailer (added by ``git commit -s``).
This is distinct from the cryptographic ``-S`` signature enforced by the
``homeric-main-baseline`` ruleset. The local hook and ``pr-policy`` Check 3
enforce DCO.

Used by both the local ``commit-msg`` pre-commit hook (the message file path is
passed as argv[0]) and the ``pr-policy`` CI job (full commit messages are piped
on stdin, NUL-separated, via ``-``).

Usage:
    # commit-msg hook: validate the message file
    python scripts/check_dco_signoff.py .git/COMMIT_EDITMSG
    # CI: validate NUL-separated full messages piped on stdin (jq emits NUL via ^@ in -j mode)
    jq -j '.data.repository.pullRequest.commits.nodes[] | .commit.message + "\\u0000"' commits.json
        | python3 scripts/check_dco_signoff.py -
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# A valid sign-off line: "Signed-off-by: Real Name <addr@host>". Require a
# non-empty name and an email containing '@' inside angle brackets so a bare
# "Signed-off-by:" does not pass. Anchored on the line prefix after strip,
# not a free substring scan, per the executable-convention-guard pattern.
_SIGNOFF_RE = re.compile(r"^Signed-off-by: .+ <[^<>@\s]+@[^<>@\s]+>$")


def validate_message(message: str) -> str | None:
    """Return an error string if *message* lacks a valid DCO trailer, else None.

    Args:
        message: The full commit message (subject + body).

    Returns:
        ``None`` when at least one well-formed ``Signed-off-by`` line is present,
        otherwise a human-readable error string.

    """
    lines = message.splitlines()
    non_empty = [ln for ln in lines if ln.strip()]
    if not non_empty:
        return "empty commit message: missing 'Signed-off-by: Name <email>' trailer"
    subject = non_empty[0]
    for line in lines:
        if _SIGNOFF_RE.match(line.strip()):
            return None
    return f"missing 'Signed-off-by: Name <email>' trailer in: {subject!r}"


def _messages_from_args(argv: list[str]) -> list[str]:
    """Return full commit messages from a file path (argv[0]) or NUL-split stdin.

    File path (commit-msg stage): the whole file is one message (comment lines
    starting with '#' are stripped, as git does before applying the trailer).
    Stdin ('-', CI): messages are NUL-separated; empty records are dropped.

    Args:
        argv: Positional arguments -- a single message-file path, or ``["-"]``
            (or empty) to read NUL-delimited messages from stdin.

    Returns:
        A list of commit messages to validate.

    """
    if not argv or argv[0] == "-":
        raw = sys.stdin.read()
        return [m for m in raw.split("\x00") if m.strip()]
    text = Path(argv[0]).read_text(encoding="utf-8")
    body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
    return [body] if body.strip() else []


def main(argv: list[str] | None = None) -> int:
    """Validate each message; exit non-zero if any lacks a DCO trailer.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when every message carries a valid trailer (or there are none),
        ``1`` otherwise.

    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    messages = _messages_from_args(args)
    # An empty message set is not a violation; the signing + Closes checks
    # cover the empty-commits anomaly. (Mirrors check_conventional_commit.py.)
    failed = False
    for message in messages:
        err = validate_message(message)
        if err:
            print(f"FAILED: DCO sign-off check: {err}")
            failed = True
    if failed:
        print(
            "Every commit MUST carry a 'Signed-off-by: Name <email>' trailer "
            "(the DCO). Add it with: git commit -S -s  (re-sign existing commits "
            "with: git rebase --exec 'git commit --amend --no-edit -S -s' origin/main). "
            "See CONTRIBUTING.md, section 'Developer Certificate of Origin (DCO)'."
        )
        return 1
    print("PASSED: DCO sign-off check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
