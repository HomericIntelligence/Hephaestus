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
    python -m hephaestus.github.severity_label \
      --repo owner/name --issue-number 42 --body-file issue-body.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    add_version_arg,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.github.client import DEFAULT_GH_TIMEOUT, gh_call, positive_timeout

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


def _gh(*args: str, timeout: int | None = None) -> str:
    """Run ``gh`` through :func:`gh_call` (circuit breaker + rate-limit retry).

    Routing through the shared adapter — never bare ``subprocess.run`` — is the
    invariant #1433 established for this module.
    """
    if timeout is None:
        return gh_call(list(args), check=True).stdout
    return gh_call(list(args), check=True, timeout=timeout).stdout


def apply_severity_label(
    repo: str,
    issue_number: int,
    selected: str | None,
    *,
    gh_timeout: int | None = None,
) -> None:
    """Reconcile the issue's ``severity:*`` labels to exactly ``selected``.

    Lists the issue's current labels, removes any ``severity:*`` label that is
    not the selected one, then adds the selected one (idempotent). With
    ``selected=None`` all ``severity:*`` labels are removed and none is added.

    Args:
        repo: ``owner/name`` slug.
        issue_number: The server-controlled issue number.
        selected: A value from :data:`VALID_SEVERITIES`, or ``None`` to clear.

    """
    gh_kwargs = {"timeout": gh_timeout} if gh_timeout is not None else {}
    current = _gh(
        "api",
        f"repos/{repo}/issues/{issue_number}/labels",
        "--jq",
        ".[].name",
        **gh_kwargs,
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
                **gh_kwargs,
            )
    if target and target not in current:
        _gh(
            "api",
            "--method",
            "POST",
            f"repos/{repo}/issues/{issue_number}/labels",
            "-f",
            f"labels[]={target}",
            **gh_kwargs,
        )


def _read_body(path: str) -> str:
    """Read an issue body from *path*, where ``-`` selects standard input."""
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Reconcile an issue severity label from explicit CLI inputs.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv[1:]``); used so
            ``--help`` works without reading the body input.

    Returns:
        Process exit code (0 on success, 1 on a non-numeric issue number).

    """
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile the severity:* label for a GitHub issue from its issue-form "
            "Severity answer supplied as an explicit file or standard input."
        )
    )
    add_github_throttle_args(parser)
    add_json_arg(parser)
    add_version_arg(parser)
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--issue-number", required=True, type=int, metavar="N")
    parser.add_argument(
        "--body-file",
        required=True,
        metavar="PATH",
        help="issue body path, or - to read standard input",
    )
    parser.add_argument(
        "--gh-timeout",
        type=positive_timeout,
        default=DEFAULT_GH_TIMEOUT,
        metavar="SECONDS",
        help=f"per-call GitHub CLI timeout (default: {DEFAULT_GH_TIMEOUT})",
    )
    args = parser.parse_args(argv)
    configure_github_throttle_from_args(args)

    repo = args.repo
    if not repo or "/" not in repo:
        message = f"Unexpected --repo {repo!r} (expected owner/name)"
        if args.json:
            emit_json_status(1, message)
        else:
            print(message, file=sys.stderr)
        return 1
    if args.issue_number <= 0:
        message = f"Unexpected --issue-number {args.issue_number!r} (not a positive integer)"
        if args.json:
            emit_json_status(1, message)
        else:
            print(message, file=sys.stderr)
        return 1
    try:
        body = _read_body(args.body_file)
    except OSError as exc:
        message = f"Could not read --body-file {args.body_file!r}: {exc}"
        if args.json:
            emit_json_status(1, message)
        else:
            print(message, file=sys.stderr)
        return 1
    selected = parse_severity(body)
    apply_severity_label(
        repo,
        args.issue_number,
        selected,
        gh_timeout=args.gh_timeout,
    )
    message = f"Reconciled severity label to: {selected or '(none)'}"
    if args.json:
        emit_json_status(0, message, severity=selected)
    else:
        print(message)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
