#!/usr/bin/env python3
"""Apply the ``severity:*`` label matching an issue form's "Severity" answer (#1210).

The issue forms (``.github/ISSUE_TEMPLATE/*.yml``) render their Severity
dropdown answer into the issue body as a ``### Severity`` heading followed by
the chosen value. :func:`parse_severity` extracts that value;
:func:`apply_severity_label` reconciles the issue's ``severity:*`` labels to
match (removing any stale one), and :func:`main` wires the two together for the
``auto-label-severity`` workflow.

Security: the user-controlled body is matched against a hard-coded allow-list
(:data:`VALID_SEVERITIES`) and only ever a fixed ``severity:*`` constant reaches
the GitHub API — the body is never executed or passed as a label (avoids the
CWE-94 issue-body injection class). The server-controlled issue number is
validated numeric before use.

Usage:
    python -m hephaestus.github.severity_label

    Reads ``GITHUB_REPOSITORY``, ``ISSUE_NUMBER`` and ``ISSUE_BODY`` from the
    environment (bound by the workflow, never interpolated into the shell).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    add_version_arg,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.github.client import gh_call

# Allow-list: the only labels this tool may ever apply. Mirrors the provisioned
# ``severity:*`` labels (verified via ``gh label list``).
VALID_SEVERITIES: tuple[str, ...] = ("critical", "major", "minor", "nitpick")

# A rendered issue-form dropdown answer appears under a markdown heading whose
# text is exactly the field ``label:`` ("Severity").
_HEADING_RE = re.compile(r"^#{1,6}\s+severity\s*$", re.IGNORECASE)


def parse_severity(issue_body: str) -> str | None:
    """Return the lowercased severity under the rendered "### Severity" heading.

    GitHub renders an issue-form dropdown answer as a markdown heading whose
    text is the field ``label:`` followed (after optional blank lines) by the
    selected option on its own line.

    Args:
        issue_body: The full rendered issue body.

    Returns:
        One of :data:`VALID_SEVERITIES`, or ``None`` when no recognised
        severity is found (including the ``_No response_`` placeholder), so
        callers treat "unset" as a safe no-op.

    """
    lines = issue_body.splitlines()
    for idx, line in enumerate(lines):
        if not _HEADING_RE.match(line.strip()):
            continue
        # Scan the next few non-blank lines for an allow-listed value.
        for follow in lines[idx + 1 : idx + 5]:
            candidate = follow.strip().lower()
            if not candidate:
                continue
            if candidate in VALID_SEVERITIES:
                return candidate
            break  # first non-blank line wasn't a severity → stop
    return None


def _gh(*args: str) -> str:
    """Run ``gh`` through the shared adapter."""
    return gh_call(list(args), check=True).stdout


def apply_severity_label(repo: str, issue_number: int, selected: str | None) -> None:
    """Reconcile the issue's ``severity:*`` labels to exactly ``selected``.

    Lists the issue's current labels, removes any ``severity:*`` label that is
    not the selected one, then adds the selected one (idempotent). With
    ``selected=None`` all ``severity:*`` labels are removed and none is added.

    Args:
        repo: ``owner/name`` slug.
        issue_number: The server-controlled issue number.
        selected: A value from :data:`VALID_SEVERITIES`, or ``None`` to clear.

    """
    current = _gh(
        "api",
        f"repos/{repo}/issues/{issue_number}/labels",
        "--jq",
        ".[].name",
    ).split()
    target = f"severity:{selected}" if selected else None
    # Remove any stale severity:* label (reconciliation, not just add).
    for name in current:
        if name.startswith("severity:") and name != target:
            _gh(
                "api",
                "--method",
                "DELETE",
                f"repos/{repo}/issues/{issue_number}/labels/{name}",
            )
    if target and target not in current:
        _gh(
            "api",
            "--method",
            "POST",
            f"repos/{repo}/issues/{issue_number}/labels",
            "-f",
            f"labels[]={target}",
        )


def main(argv: list[str] | None = None) -> int:
    """Reconcile the severity label for the issue named by the environment.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv[1:]``); used so
            ``--help`` works without requiring the workflow env vars.

    Returns:
        Process exit code (0 on success, 1 on a non-numeric issue number).

    """
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile the severity:* label for a GitHub issue from its issue-form "
            "Severity answer. Reads GITHUB_REPOSITORY, ISSUE_NUMBER and ISSUE_BODY "
            "from the environment."
        )
    )
    add_github_throttle_args(parser)
    add_json_arg(parser)
    add_version_arg(parser)
    args = parser.parse_args(argv)
    configure_github_throttle_from_args(args)

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo or "/" not in repo:
        message = f"Unexpected GITHUB_REPOSITORY {repo!r} (expected owner/name)"
        if args.json:
            emit_json_status(1, message)
        else:
            print(message, file=sys.stderr)
        return 1
    raw = os.environ.get("ISSUE_NUMBER", "")
    if not raw.isdigit():
        message = f"Unexpected ISSUE_NUMBER {raw!r} (not a positive integer)"
        if args.json:
            emit_json_status(1, message)
        else:
            print(message, file=sys.stderr)
        return 1
    selected = parse_severity(os.environ.get("ISSUE_BODY", ""))
    apply_severity_label(repo, int(raw), selected)
    message = f"Reconciled severity label to: {selected or '(none)'}"
    if args.json:
        emit_json_status(0, message, severity=selected)
    else:
        print(message)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
