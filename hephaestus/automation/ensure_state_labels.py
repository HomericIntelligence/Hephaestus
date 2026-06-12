"""Idempotently provision automation ``state:*`` labels on one or more repos.

Pairs with :mod:`hephaestus.automation.state_labels` (the single source of truth
for the labels) and is the one-shot operator tool used after #704 ships: run
once with ``--org`` to create/update the plan-state, implementation-review, and
skip labels across the whole HomericIntelligence org so the first
label-application call doesn't race against a missing label.

The script is idempotent: GitHub's ``gh label create --force`` upserts the
label (creating it if absent, updating colour/description if present), so
re-running is safe and the script can be wired into routine ops.

Usage examples::

    # Default — current repo (derived from ``git remote`` of cwd):
    hephaestus-ensure-state-labels

    # A specific repo:
    hephaestus-ensure-state-labels --repo HomericIntelligence/ProjectScylla

    # Every non-archived, non-fork repo in an org (with confirmation):
    hephaestus-ensure-state-labels --org HomericIntelligence

    # Dry run — print what would happen, mutate nothing:
    hephaestus-ensure-state-labels --org HomericIntelligence --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys

from hephaestus.cli.utils import add_json_arg, add_version_arg, emit_json_status

from .state_labels import STATE_LABEL_SPECS

logger = logging.getLogger(__name__)

# Repo names to skip when iterating an org (matches loop_runner's policy: skip
# archives, forks, and the special "Odysseus" sandbox repo).
_SKIP_REPO_NAMES: frozenset[str] = frozenset({"Odysseus"})


def _gh_list_org_repos(org: str, *, timeout: int = 60) -> list[str]:
    """Return non-archived, non-fork repo names for ``org``.

    Mirrors :func:`loop_runner._gh_list_repos` (deliberately duplicated as a
    leaf utility so this script has zero import-time dependency on the loop
    runner — operators running it during an emergency shouldn't transitively
    load the automation pipeline).
    """
    out = subprocess.run(
        [
            "gh",
            "repo",
            "list",
            org,
            "--no-archived",
            "--source",  # excludes forks
            "--limit",
            "200",
            "--json",
            "name,isArchived,isFork",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if out.returncode != 0:
        raise SystemExit(f"gh repo list {org} failed (rc={out.returncode}): {out.stderr.strip()}")
    try:
        entries = json.loads(out.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"gh repo list returned invalid JSON: {exc}") from exc
    return sorted(
        e["name"]
        for e in entries
        if not e.get("isArchived", False)
        and not e.get("isFork", False)
        and e["name"] not in _SKIP_REPO_NAMES
    )


def ensure_labels_on_repo(repo: str, *, dry_run: bool = False) -> int:
    """Create/update the automation ``state:*`` labels on ``repo``.

    Args:
        repo: ``OWNER/NAME`` slug.
        dry_run: When True, log what would happen and exit without calling
            ``gh label create``.

    Returns:
        Number of label-create calls actually issued (0 in dry-run mode).

    """
    issued = 0
    for label, spec in STATE_LABEL_SPECS.items():
        if dry_run:
            logger.info(
                "[dry-run] %s ← gh label create %r colour=%s",
                repo,
                label,
                spec["color"],
            )
            continue
        cmd = [
            "gh",
            "label",
            "create",
            label,
            "--repo",
            repo,
            "--color",
            spec["color"],
            "--description",
            spec["description"],
            "--force",  # upsert: create-or-update
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
        if proc.returncode != 0:
            logger.warning(
                "%s: failed to ensure label %r (rc=%s): %s",
                repo,
                label,
                proc.returncode,
                proc.stderr.strip(),
            )
            continue
        logger.info("%s: ensured label %r", repo, label)
        issued += 1
    return issued


def _detect_current_repo_slug() -> str:
    """Derive ``owner/name`` from the current git checkout's ``origin`` remote."""
    proc = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SystemExit(
            "Could not detect current repo via 'gh repo view'. "
            "Pass --repo OWNER/NAME or --org NAME explicitly."
        )
    return proc.stdout.strip()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hephaestus-ensure-state-labels",
        description=(
            "Idempotently provision automation state:* labels "
            "(plan-state, implementation-review, and skip) on one or more repos."
        ),
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help="Single target repo (default: the current git checkout's origin).",
    )
    target.add_argument(
        "--org",
        metavar="ORG",
        help=(
            "Apply to every non-archived, non-fork repo in the org "
            "(Odysseus is skipped, matching loop_runner)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; mutate nothing.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    add_json_arg(parser)
    add_version_arg(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``hephaestus-ensure-state-labels``.

    Returns 0 on success, non-zero on hard failure (e.g. ``gh`` not on PATH or
    an unrecoverable ``gh repo list`` failure). Per-label-create warnings
    do not fail the overall run — operators can re-run to retry.
    """
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.org:
        repos = _gh_list_org_repos(args.org)
        if not repos:
            logger.warning("No repos returned for org %s — nothing to do.", args.org)
            if args.json:
                emit_json_status(0, "no-repos", org=args.org)
            return 0
        slugs = [f"{args.org}/{name}" for name in repos]
        logger.info("Ensuring state:* labels on %d repos in %s", len(slugs), args.org)
    elif args.repo:
        slugs = [args.repo]
    else:
        slugs = [_detect_current_repo_slug()]

    total_issued = 0
    for slug in slugs:
        total_issued += ensure_labels_on_repo(slug, dry_run=args.dry_run)
    if args.dry_run:
        logger.info(
            "[dry-run] Would ensure %d labels across %d repo(s).",
            len(STATE_LABEL_SPECS) * len(slugs),
            len(slugs),
        )
    else:
        logger.info(
            "Ensured %d label(s) across %d repo(s).",
            total_issued,
            len(slugs),
        )
    if args.json:
        emit_json_status(
            0,
            "dry-run" if args.dry_run else "ok",
            repos=len(slugs),
            labels_ensured=total_issued,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
