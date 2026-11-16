#!/usr/bin/env python3
"""Consolidate duplicate canonical plan/review pairs on one GitHub issue.

Two tools can each publish one canonical plan/review pair on the same issue.
No code path selects a winner. The operator names the exact keep and
supersede comment IDs and supplies the merged plan and review content. The
command is read-only by default; ``--apply`` runs the ordered consolidation
with exact read-back after every write.

Symptom: an automation round stops with a planning-marker identity conflict,
and the issue shows two qualifying plan/review pairs. See
``docs/runbooks/consolidate-plan-records.md``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hephaestus.automation import github_api
from hephaestus.automation.issue_timeline import issue_comments_from_metadata
from hephaestus.automation.plan_consolidation import (
    ConsolidationError,
    ConsolidationPlan,
    ConsolidationRequest,
    execute_consolidation,
    plan_consolidation as _plan_consolidation,
    planning_role_inventory,
)


def _repo(value: str) -> tuple[str, str]:
    owner, separator, name = value.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise argparse.ArgumentTypeError("repository must be OWNER/NAME")
    return owner, name


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("comment ids must be positive integers")
    return number


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=_repo, help="OWNER/NAME (defaults to current repo)")
    parser.add_argument("--issue", type=int, required=True, help="issue number to consolidate")
    parser.add_argument(
        "--keep-plan-comment",
        type=_positive_int,
        required=True,
        help="comment id of the plan pair member that survives",
    )
    parser.add_argument(
        "--keep-review-comment",
        type=_positive_int,
        required=True,
        help="comment id of the review pair member that survives",
    )
    parser.add_argument(
        "--supersede-plan-comment",
        type=_positive_int,
        action="append",
        default=[],
        help="comment id of one superseded plan; repeatable",
    )
    parser.add_argument(
        "--supersede-review-comment",
        type=_positive_int,
        action="append",
        default=[],
        help="comment id of one superseded review; repeatable",
    )
    parser.add_argument(
        "--plan-body-file",
        type=Path,
        required=True,
        help="file with the operator-merged plan content",
    )
    parser.add_argument(
        "--review-body-file",
        type=Path,
        required=True,
        help="file with the operator-merged review content",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the displayed plan; without this flag the command is read-only",
    )
    return parser


def _read_content(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"cannot read the merged {label} file {path}: {error}") from error


def _issue_payload(issue_number: int, repo: tuple[str, str]) -> dict[str, Any]:
    return github_api.gh_issue_json(issue_number, repo=repo)


def _issue_labels(payload: dict[str, Any]) -> list[str]:
    labels = payload.get("labels") or []
    return [str(label["name"]) for label in labels if isinstance(label, dict)]


def _print_inventory(comments: Sequence[Any]) -> None:
    inventory = planning_role_inventory(comments)
    if not inventory:
        print("inventory: no plan/review claims on the issue")
        return
    print("inventory:")
    for claim in inventory:
        owned = "owned" if claim.owned else "FOREIGN"
        print(
            f"  {claim.role:6} comment={claim.comment_id} author={claim.author_login} "
            f"alias={claim.alias_tag} revision={claim.revision} owned={owned}"
        )


def _print_consolidation_plan(plan: ConsolidationPlan) -> None:
    print("provenance:")
    print(f"  {plan.provenance_line}")
    print(f"epoch: {plan.epoch}")
    if plan.archives:
        print("archives:")
        for archive in plan.archives:
            print(
                f"  {archive.claim.role:6} source={archive.claim.comment_id} "
                f"-> revision={archive.claim.revision}"
            )
    if plan.deletions:
        print(f"deletes: {list(plan.deletions)}")
    print("upserts:")
    print(f"  plan   comment={plan.request.keep_plan_comment_id} -> epoch={plan.epoch}")
    print(f"  review comment={plan.request.keep_review_comment_id} -> epoch={plan.epoch}")
    if plan.requires_label_transition:
        print(f"labels: add={list(plan.label_add)} remove={list(plan.label_remove)}")
    else:
        print("labels: none")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the read-only-default, operator-authorized consolidation."""
    args = build_parser().parse_args(argv)
    repo = args.repo or github_api.get_repo_info()
    viewer_login = github_api.gh_current_login() or ""
    if not viewer_login:
        print("error: authenticated GitHub login could not be verified", file=sys.stderr)
        return 2

    def fetch_comments() -> list[Any]:
        metadata = github_api.fetch_issue_comments_metadata(args.issue, repo)
        return issue_comments_from_metadata(metadata, viewer_login=viewer_login)

    try:
        comments = fetch_comments()
        payload = _issue_payload(args.issue, repo)
        issue_body = str(payload.get("body") or "")
        labels = _issue_labels(payload)
        request = ConsolidationRequest(
            issue_number=args.issue,
            issue_owner=repo[0],
            issue_name=repo[1],
            keep_plan_comment_id=args.keep_plan_comment,
            keep_review_comment_id=args.keep_review_comment,
            supersede_plan_comment_ids=tuple(args.supersede_plan_comment),
            supersede_review_comment_ids=tuple(args.supersede_review_comment),
            plan_content=_read_content(args.plan_body_file, label="plan"),
            review_content=_read_content(args.review_body_file, label="review"),
        )
        plan = _plan_consolidation(comments, issue_body, labels, request)
        _print_consolidation_plan(plan)
        if not args.apply:
            print("dry run: nothing written. Re-run with --apply to execute.")
            return 0
        outcome = execute_consolidation(
            plan,
            fetch_comments=fetch_comments,
            fetch_labels=lambda: _issue_labels(_issue_payload(args.issue, repo)),
            fetch_issue_body=lambda: str(_issue_payload(args.issue, repo).get("body") or ""),
            append_comment=lambda body: github_api.gh_issue_comment(args.issue, body, repo=repo),
            delete_comment=lambda comment_id: github_api.gh_issue_delete_comment(
                comment_id, repo=repo, missing_ok=True
            ),
            upsert_owned_comment=lambda marker, body: github_api.gh_issue_upsert_owned_comment(
                args.issue, marker, body, repo=repo
            ),
            edit_labels=lambda add, remove: github_api.gh_issue_edit_labels(
                args.issue, add=list(add), remove=list(remove), repo=repo
            ),
        )
    except ConsolidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if outcome.already_consolidated:
        print(f"already consolidated at epoch {outcome.epoch}; no mutation performed")
        return 0
    print(
        f"consolidated at epoch {outcome.epoch}: "
        f"archives={outcome.appended_archives} "
        f"deletes={list(outcome.deleted_comment_ids)} labels_edited={outcome.labels_edited}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
