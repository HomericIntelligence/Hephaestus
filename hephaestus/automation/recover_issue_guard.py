"""Operator-only inspection and recovery for stale issue work guards."""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from argparse import ArgumentParser
from typing import Any

from ._review_utils import build_automation_parser
from .issue_guard import (
    GitHubIssueGuardStore,
    GuardError,
    IssueGuard,
    normalize_repository,
)

logger = logging.getLogger(__name__)


def _build_parser() -> ArgumentParser:
    parser = build_automation_parser(
        prog="hephaestus-recover-issue-guard",
        description="Inspect or operator-recover an expired issue work guard.",
        add_agent=False,
        add_max_workers=False,
        add_dry_run=False,
        add_verbose=False,
    )
    parser.add_argument("--repo", required=True, metavar="OWNER/REPO")
    parser.add_argument("--issue", required=True, type=int)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--recover", action="store_true")
    parser.add_argument("--expected-claim", type=uuid.UUID)
    parser.add_argument("--expected-oid")
    parser.add_argument("--reason")
    return parser


def _recovery_environment() -> tuple[dict[str, str], set[str]]:
    token = os.environ.get("HEPHAESTUS_GUARD_RECOVERY_TOKEN", "")
    if not token:
        raise GuardError("HEPHAESTUS_GUARD_RECOVERY_TOKEN is required")
    if token in {os.environ.get("GH_TOKEN", ""), os.environ.get("GITHUB_TOKEN", "")}:
        raise GuardError("recovery token must be distinct from GH_TOKEN and GITHUB_TOKEN")
    raw_actors = os.environ.get("HEPHAESTUS_GUARD_RECOVERY_ACTORS", "")
    actors = {actor.strip().casefold() for actor in raw_actors.split(",") if actor.strip()}
    if not actors:
        raise GuardError("HEPHAESTUS_GUARD_RECOVERY_ACTORS is required")
    environment = dict(os.environ)
    environment["GH_TOKEN"] = token
    environment.pop("GITHUB_TOKEN", None)
    return environment, actors


def _snapshot_json(repository: str, issue: int, store: GitHubIssueGuardStore) -> dict[str, Any]:
    snapshot = store.read_ref(repository, issue)
    labels = list(store.read_labels(repository, issue))
    if snapshot is None:
        return {"repository": repository, "issue": issue, "labels": labels, "guard": None}
    return {
        "repository": repository,
        "issue": issue,
        "labels": labels,
        "guard": {
            "oid": snapshot.oid,
            "tree": snapshot.tree,
            "record": json.loads(snapshot.record.to_json()),
            "server_time": snapshot.server_time.isoformat(),
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Run an inspect or explicitly authorized stale-guard recovery."""
    args = _build_parser().parse_args(argv)
    try:
        repository = normalize_repository(args.repo)
        if args.issue <= 0:
            raise GuardError("issue must be positive")
        if args.inspect:
            store = GitHubIssueGuardStore(repository)
            print(json.dumps(_snapshot_json(repository, args.issue, store), sort_keys=True))
            return 0
        if args.expected_claim is None or not args.expected_oid or not args.reason:
            raise GuardError(
                "--recover requires --expected-claim, --expected-oid, and --reason"
            )
        environment, actors = _recovery_environment()
        store = GitHubIssueGuardStore(repository, env=environment)
        actor = store.actor()
        if actor.casefold() not in actors:
            raise GuardError("authenticated actor is not in HEPHAESTUS_GUARD_RECOVERY_ACTORS")
        snapshot = IssueGuard(store).recover(
            repository,
            args.issue,
            expected_claim=args.expected_claim,
            expected_oid=args.expected_oid,
            reason=args.reason,
            actor=actor,
        )
        print(
            json.dumps(
                {
                    "repository": repository,
                    "issue": args.issue,
                    "actor": actor,
                    "phase": snapshot.record.phase.value,
                    "oid": snapshot.oid,
                },
                sort_keys=True,
            )
        )
        return 0
    except (GuardError, OSError, RuntimeError, ValueError) as exc:
        logger.error("issue guard operation failed: %s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["main"]
