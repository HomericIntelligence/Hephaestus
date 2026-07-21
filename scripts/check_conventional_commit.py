#!/usr/bin/env python3
r"""Validate that a commit subject follows Conventional Commits.

Enforces DoD row 5 (docs/DEFINITION_OF_DONE.md): commit subjects must match
``type(scope): description`` with an allowed type. Used by both the local
``commit-msg`` pre-commit hook (the message file path is passed as argv[0])
and the ``pr-policy`` CI job (each PR commit subject is piped on stdin via
``-``, one per line).

Usage:
    # commit-msg hook: validate the message file
    python scripts/check_conventional_commit.py .git/COMMIT_EDITMSG
    # CI: validate subjects piped on stdin
    printf '%s\\n' "fix(io): handle EOF" | python scripts/check_conventional_commit.py -
"""

from __future__ import annotations

import sys
from pathlib import Path

ALLOWED_TYPES = frozenset(
    {"feat", "fix", "docs", "refactor", "test", "chore", "ci", "build", "perf", "style", "revert"}
)

# git-generated subjects that are never Conventional Commits and must pass.
_MACHINERY_PREFIXES = ("Merge ", "Revert ", "fixup!", "squash!")


def validate_subject(subject: str) -> str | None:
    """Return an error string if *subject* is not a valid Conventional Commit, else None.

    Splits on the first colon only; extracts an optional ``(scope)`` via
    index/rindex so nested parens survive; allows a trailing ``!`` breaking marker.

    Args:
        subject: A single commit subject line.

    Returns:
        ``None`` when the subject conforms, otherwise a human-readable error string.

    """
    subject = subject.rstrip("\n")
    if not subject.strip():
        return "empty commit subject"
    if subject.startswith(_MACHINERY_PREFIXES):
        return None
    if ":" not in subject:
        return f"missing 'type(scope): ' prefix in: {subject!r}"
    prefix, message = subject.split(":", 1)
    if not message.strip():
        return f"empty description after ':' in: {subject!r}"
    if "(" in prefix and prefix.rstrip().endswith((")", ")!")):
        # Strip a trailing breaking-change '!' before scope extraction.
        core = prefix[:-1] if prefix.rstrip().endswith("!") else prefix
        scope = core[core.index("(") + 1 : core.rindex(")")]
        if not scope.strip():
            return f"empty scope '()' in: {subject!r}"
        type_token = core[: core.index("(")]
    else:
        type_token = prefix[:-1] if prefix.endswith("!") else prefix
    if type_token not in ALLOWED_TYPES:
        return (
            f"invalid type {type_token!r} in: {subject!r}; "
            f"allowed: {', '.join(sorted(ALLOWED_TYPES))}"
        )
    return None


def _subjects_from_args(argv: list[str]) -> list[str]:
    """Return subject lines from a message file path (argv[0]) or stdin ('-').

    File path (commit-msg stage): the subject is the first non-comment,
    non-blank line of the message file. Stdin ('-', CI): every non-blank line
    is treated as one subject.

    Args:
        argv: Positional arguments — a single message-file path, or ``["-"]``
            (or empty) to read newline-delimited subjects from stdin.

    Returns:
        A list of subject lines to validate.

    """
    if not argv or argv[0] == "-":
        return [line for line in sys.stdin.read().splitlines() if line.strip()]
    text = Path(argv[0]).read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            return [line]
    return [""]


def main(argv: list[str] | None = None) -> int:
    """Validate each subject; exit non-zero if any violation is found.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when every subject conforms (or there are none), ``1`` otherwise.

    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    subjects = _subjects_from_args(args)
    # An empty subject set (no commits / empty stdin) is not a violation;
    # the PR-body and DCO policy checks cover malformed empty metadata.
    failed = False
    for subject in subjects:
        err = validate_subject(subject)
        if err:
            print(f"FAILED: Conventional Commit check: {err}")
            failed = True
    if failed:
        print(
            "Commit subjects must follow Conventional Commits: "
            "type(scope): description. See docs/DEFINITION_OF_DONE.md row 5."
        )
        return 1
    print("PASSED: Conventional Commit check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
