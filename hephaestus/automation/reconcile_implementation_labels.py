"""Reconcile loop-owned implementation labels against the current-head verdict.

``state:implementation-go`` is automated implementation eligibility. The pipeline
applies it exclusively, with a fresh readback, on its own write paths (#2688). A
verdict published *outside* the loop therefore has no effect on it, and the label
can outlive the verdict that contradicts it.

The observed case was ``HomericIntelligence/Scylla#2093``: a manually applied
``state:implementation-go`` from ``2026-08-23`` survived a current-head ``NO-GO``
verdict published on ``2026-09-14`` with two unresolved required findings. The loop
never parses published verdicts; it learns outcomes from its own reviewer job, so a
verdict published by an operator running the review skill directly is invisible to it.

This pass closes that gap. It reads the published review-exchange carrier for the
pull request and **clears** a loop-owned ``state:implementation-go`` label when the
verdict bound to the pull request's live head is ``NO-GO``.

Design constraints, each enforced below:

* **Exact-head binding.** A verdict binds only the head it was published against.
  The review's commit and the carrier's bound revision must both equal the live head.
* **One-way and fail-safe.** The pass only ever removes eligibility. It can never
  add ``state:implementation-go``; granting it stays with the reviewed-head GO proof
  in the ``pr_review`` stage.
* **Untrusted carrier.** The carrier is forge content. Its digest is verified by
  recomputation and a mismatched, malformed, or off-target carrier mutates nothing.
* **Exclusive transition.** A correction is one atomic label edit followed by a fresh
  exclusive readback.
* **Repo-scoped reads.** Every ``gh`` call names the repository explicitly; an issue
  number carries no repository (#2245).

Usage examples::

    # Report stale labels across an org, mutate nothing:
    hephaestus-reconcile-implementation-labels --org HomericIntelligence --dry-run

    # Reconcile one repository:
    hephaestus-reconcile-implementation-labels --repo HomericIntelligence/Scylla

    # Reconcile specific pull requests:
    hephaestus-reconcile-implementation-labels --repo OWNER/NAME --pr 2093
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hephaestus.automation.github_api import gh_call, gh_issue_edit_labels
from hephaestus.cli.localization import text
from hephaestus.cli.utils import (
    configure_cli_logging,
    configure_github_throttle_from_args,
    emit_json_status,
)
from hephaestus.utils.terminal import terminal_guard

from ._review_utils import build_automation_parser
from .state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
)

logger = logging.getLogger(__name__)

#: Name of the published review-exchange carrier marker.
EXCHANGE_MARKER_NAME = "HomericIntelligence:review-exchange:v1"

#: Schema identifier of the carrier document that carries a review verdict.
EXCHANGE_SCHEMA_ID = "athena.review-exchange.state"

VERDICT_GO = "GO"
VERDICT_CONDITIONAL_GO = "CONDITIONAL GO"
VERDICT_NO_GO = "NO-GO"

#: Verdicts a carrier may legitimately report.
ALLOWED_VERDICTS = frozenset({VERDICT_GO, VERDICT_CONDITIONAL_GO, VERDICT_NO_GO})

#: Verdicts this pass acts on. GO is deliberately absent: the pass may never grant
#: implementation eligibility, only withdraw it.
ACTIONABLE_VERDICTS = frozenset({VERDICT_NO_GO})

_MARKER = re.compile(
    r"<!--\s*"
    + re.escape(EXCHANGE_MARKER_NAME)
    + r"\s+kind=(?P<kind>[A-Za-z0-9_-]+)\s+sha256=(?P<digest>[0-9a-fA-F]{64})\s*-->"
)
_FENCED_JSON = re.compile(r"```json[ \t]*\r?\n(?P<payload>.*?)\r?\n```", re.DOTALL)


class CarrierRejectedError(Exception):
    """The published carrier is absent, malformed, tampered, or off-target."""


@dataclass(frozen=True)
class PublishedVerdict:
    """A review verdict that is bound to one exact head of one pull request."""

    verdict: str
    head_oid: str
    pr_number: int
    repository: str
    required_remaining: int


@dataclass(frozen=True)
class ReconciliationDecision:
    """The outcome of comparing the current-head verdict with the live labels."""

    action: str
    reason: str
    verdict: PublishedVerdict | None = None

    @property
    def should_write(self) -> bool:
        """Return True when the pass must correct the labels."""
        return self.action == "reconcile"


def canonical_state_digest(state: Mapping[str, Any]) -> str:
    """Return the carrier digest for a review-exchange ``state`` object.

    The published scheme is the SHA-256 hexdigest of the compact, key-sorted JSON
    encoding of the state object. Verified against the live carrier published on
    ``HomericIntelligence/Scylla#2093``, whose recomputed digest equals both its
    marker digest and its ``state_sha256`` field.

    Args:
        state: The carrier ``state`` object.

    Returns:
        The lowercase hex digest.

    """
    payload = json.dumps(state, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_mapping(value: object, detail: str) -> Mapping[str, Any]:
    """Return *value* as a mapping or raise :class:`CarrierRejectedError`."""
    if not isinstance(value, Mapping):
        raise CarrierRejectedError(detail)
    return value


def parse_carrier(body: str, *, pr_number: int, repository: str) -> PublishedVerdict:
    """Parse and verify one published review-exchange state carrier.

    Args:
        body: The review body that may contain the carrier.
        pr_number: Pull request the carrier must target.
        repository: ``OWNER/NAME`` the carrier must target.

    Returns:
        The verified verdict bound to the carrier's revision.

    Raises:
        CarrierRejectedError: The carrier is missing, malformed, tampered, or targets a
            different pull request, repository, revision, or surface.

    """
    marker = _find_marker(body)
    document = _parse_document(body, marker)
    state = _verify_document(document, marker.group("digest"))
    _verify_target(state, pr_number=pr_number, repository=repository)
    revision = _verify_binding(state)
    verdict = _verify_verdict(state)
    remaining = _required_remaining(state)
    if remaining is None:
        raise CarrierRejectedError("carrier does not publish a required-finding count")
    return PublishedVerdict(
        verdict=verdict,
        head_oid=revision,
        pr_number=pr_number,
        repository=repository,
        required_remaining=remaining,
    )


def _find_marker(body: str) -> re.Match[str]:
    """Return the carrier's state marker, or raise :class:`CarrierRejectedError`."""
    if not body:
        raise CarrierRejectedError("carrier body is empty")
    marker = _MARKER.search(body)
    if marker is None:
        raise CarrierRejectedError(f"no {EXCHANGE_MARKER_NAME} marker")
    if marker.group("kind") != "state":
        raise CarrierRejectedError(f"unexpected carrier kind {marker.group('kind')!r}")
    return marker


def _parse_document(body: str, marker: re.Match[str]) -> Mapping[str, Any]:
    """Return the JSON object in the fenced block after the marker."""
    fenced = _FENCED_JSON.search(body, marker.end())
    if fenced is None:
        raise CarrierRejectedError("state marker is not followed by a json block")
    try:
        document = json.loads(fenced.group("payload"))
    except json.JSONDecodeError as error:
        raise CarrierRejectedError(f"carrier payload is not valid JSON: {error}") from error
    return _require_mapping(document, "carrier payload is not an object")


def _verify_document(document: Mapping[str, Any], marker_digest: str) -> Mapping[str, Any]:
    """Return the verified ``state`` object of a carrier document."""
    if document.get("schema_id") != EXCHANGE_SCHEMA_ID:
        raise CarrierRejectedError(f"unexpected carrier schema_id {document.get('schema_id')!r}")
    state = _require_mapping(document.get("state"), "carrier has no state object")
    computed = canonical_state_digest(state)
    if computed != marker_digest.lower():
        raise CarrierRejectedError("carrier state digest does not match the marker digest")
    declared = str(document.get("state_sha256") or "").lower()
    if declared != computed:
        raise CarrierRejectedError("carrier state_sha256 does not match the state digest")
    return state


def _verify_target(state: Mapping[str, Any], *, pr_number: int, repository: str) -> None:
    """Require the carrier to describe this pull request and nothing else."""
    if state.get("surface") != "pull_request":
        raise CarrierRejectedError(f"carrier surface {state.get('surface')!r} is not a PR")
    target = _require_mapping(state.get("target"), "carrier has no target")
    target_number = target.get("number")
    if not isinstance(target_number, int) or isinstance(target_number, bool):
        raise CarrierRejectedError("carrier target number is invalid")
    if target_number != pr_number:
        raise CarrierRejectedError(f"carrier targets PR #{target_number}, not #{pr_number}")
    if str(target.get("repository") or "") != repository:
        raise CarrierRejectedError(
            f"carrier targets {target.get('repository')!r}, not {repository!r}"
        )


def _verify_binding(state: Mapping[str, Any]) -> str:
    """Return the carrier's bound revision, requiring internal consistency."""
    binding = _require_mapping(state.get("artifact_binding"), "carrier has no binding")
    revision = binding.get("revision")
    if not isinstance(revision, str) or not revision:
        raise CarrierRejectedError("carrier does not bind a revision")
    progress_revision = _latest_progress_revision(state)
    if progress_revision is not None and progress_revision != revision:
        raise CarrierRejectedError("carrier progress revision does not match the bound revision")
    return revision


def _verify_verdict(state: Mapping[str, Any]) -> str:
    """Return the carrier's verdict, rejecting anything outside the vocabulary."""
    verdict = str(state.get("verdict") or "")
    if verdict not in ALLOWED_VERDICTS:
        raise CarrierRejectedError(f"unknown verdict {verdict!r}")
    return verdict


def _latest_progress_revision(state: Mapping[str, Any]) -> str | None:
    """Return the artifact revision of the newest ``progress`` entry, if any.

    The carrier publishes one ``progress`` entry per review round, each binding the
    artifact revision that round reviewed. When the field is present it must agree
    with the carrier-wide binding, so a carrier cannot describe one revision while
    reporting the progress of another.

    Args:
        state: The carrier ``state`` object.

    Returns:
        The newest bound revision, or None when no entry publishes one.

    """
    progress = state.get("progress")
    if not isinstance(progress, list):
        return None
    for entry in reversed(progress):
        if not isinstance(entry, Mapping):
            continue
        revision = entry.get("artifact_revision")
        if isinstance(revision, str) and revision:
            return revision
    return None


def _required_remaining(state: Mapping[str, Any]) -> int | None:
    """Return the number of required findings still open on the bound head.

    The carrier's authoritative count is the newest ``progress`` entry's
    ``required_remaining``. When no ``progress`` entry publishes it, the count is
    derived from the findings already open with a ``required`` disposition.

    Args:
        state: The carrier ``state`` object.

    Returns:
        A non-negative count, or None when the carrier publishes neither source.

    """
    progress = state.get("progress")
    if isinstance(progress, list):
        for entry in reversed(progress):
            if not isinstance(entry, Mapping):
                continue
            value = entry.get("required_remaining")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value

    findings = state.get("findings")
    if not isinstance(findings, list):
        return None
    return sum(
        1
        for finding in findings
        if isinstance(finding, Mapping)
        and finding.get("disposition") == "required"
        and str(finding.get("state") or "open") != "closed"
    )


def _review_commit_oid(review: Mapping[str, Any]) -> str:
    """Return the commit OID a review was submitted against, or an empty string."""
    commit = review.get("commit")
    if not isinstance(commit, Mapping):
        return ""
    oid = commit.get("oid")
    return oid if isinstance(oid, str) else ""


def select_current_head_verdict(
    reviews: Sequence[Mapping[str, Any]],
    *,
    pr_number: int,
    repository: str,
    head_oid: str,
) -> PublishedVerdict | None:
    """Return the newest verdict bound to the pull request's live head.

    A review counts only when the review itself was submitted against ``head_oid``
    *and* the carrier inside it binds the same revision. Both must agree, so a
    carrier copied onto a later head cannot be mistaken for a current verdict.

    Args:
        reviews: ``gh pr view --json reviews`` entries.
        pr_number: Pull request being reconciled.
        repository: ``OWNER/NAME`` of the pull request.
        head_oid: The pull request's live head commit OID.

    Returns:
        The newest usable verdict, or None when there is none.

    """
    if not head_oid:
        return None
    candidates: list[tuple[str, PublishedVerdict]] = []
    for review in reviews:
        if not isinstance(review, Mapping):
            continue
        if _review_commit_oid(review) != head_oid:
            continue
        body = review.get("body")
        if not isinstance(body, str) or not body:
            continue
        try:
            verdict = parse_carrier(body, pr_number=pr_number, repository=repository)
        except CarrierRejectedError:
            continue
        if verdict.head_oid != head_oid:
            continue
        candidates.append((str(review.get("submittedAt") or ""), verdict))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def decide(
    *,
    labels: Iterable[str],
    verdict: PublishedVerdict | None,
) -> ReconciliationDecision:
    """Decide whether a stale implementation label must be corrected.

    The decision is one-way. Only a ``NO-GO`` verdict bound to the live head can
    withdraw eligibility; nothing here can grant it.

    Args:
        labels: The pull request's current label names.
        verdict: The current-head verdict, or None when there is none.

    Returns:
        The decision, carrying the reason for the audit trail.

    """
    present = set(labels)
    has_go = STATE_IMPLEMENTATION_GO in present
    has_no_go = STATE_IMPLEMENTATION_NO_GO in present

    if verdict is None:
        return ReconciliationDecision("skip", "no-current-head-verdict")
    if verdict.verdict not in ACTIONABLE_VERDICTS:
        return ReconciliationDecision("skip", "verdict-not-no-go", verdict)
    if verdict.required_remaining < 1:
        return ReconciliationDecision("skip", "no-required-findings-remaining", verdict)
    if has_no_go and not has_go:
        return ReconciliationDecision("skip", "already-exclusive-no-go", verdict)
    if not has_go:
        return ReconciliationDecision("skip", "no-stale-go-label", verdict)
    return ReconciliationDecision("reconcile", "current-head-no-go", verdict)


def fetch_pr_snapshot(repository: str, pr_number: int) -> dict[str, Any]:
    """Return the live head, label names, and reviews for a pull request.

    Args:
        repository: ``OWNER/NAME`` of the repository.
        pr_number: Pull request number.

    Returns:
        A mapping with ``head_oid``, ``labels``, and ``reviews`` keys.

    Raises:
        subprocess.CalledProcessError: The ``gh`` call failed.
        RuntimeError: The ``gh`` output was not a JSON object.

    """
    result = gh_call(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repository,
            "--json",
            "headRefOid,labels,reviews",
        ]
    )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"gh pr view {repository}#{pr_number} returned invalid JSON: {error}"
        ) from error
    if not isinstance(data, Mapping):
        raise RuntimeError(f"gh pr view {repository}#{pr_number} returned non-object JSON")

    raw_labels = data.get("labels")
    labels: list[str] = []
    if isinstance(raw_labels, list):
        for entry in raw_labels:
            name = entry.get("name") if isinstance(entry, Mapping) else entry
            if isinstance(name, str) and name:
                labels.append(name)
    raw_reviews = data.get("reviews")
    return {
        "head_oid": str(data.get("headRefOid") or ""),
        "labels": labels,
        "reviews": raw_reviews if isinstance(raw_reviews, list) else [],
    }


def reconcile_pr(repository: str, pr_number: int, *, dry_run: bool = False) -> bool:
    """Correct a stale loop-owned implementation label for one pull request.

    Args:
        repository: ``OWNER/NAME`` of the repository.
        pr_number: Pull request number.
        dry_run: When True, report the decision and mutate nothing.

    Returns:
        True only when the labels were corrected and the correction was confirmed.

    Raises:
        RuntimeError: The post-write exclusive readback did not confirm NO-GO.

    """
    snapshot = fetch_pr_snapshot(repository, pr_number)
    verdict = select_current_head_verdict(
        snapshot["reviews"],
        pr_number=pr_number,
        repository=repository,
        head_oid=snapshot["head_oid"],
    )
    decision = decide(labels=snapshot["labels"], verdict=verdict)
    if not decision.should_write:
        logger.info("%s#%d: no reconciliation (%s)", repository, pr_number, decision.reason)
        return False

    if decision.verdict is None:
        raise RuntimeError("reconciliation decision is missing its verdict")
    logger.info(
        "%s#%d: stale %s with current-head %s (head %s, %d required finding(s) remaining)",
        repository,
        pr_number,
        STATE_IMPLEMENTATION_GO,
        decision.verdict.verdict,
        decision.verdict.head_oid,
        decision.verdict.required_remaining,
    )
    if dry_run:
        logger.info(
            "[dry-run] %s#%d: would replace %s with %s",
            repository,
            pr_number,
            STATE_IMPLEMENTATION_GO,
            STATE_IMPLEMENTATION_NO_GO,
        )
        return False

    owner, _, name = repository.partition("/")
    if not owner or not name:
        raise RuntimeError(f"repository {repository!r} is not an OWNER/NAME slug")
    gh_issue_edit_labels(
        pr_number,
        add=[STATE_IMPLEMENTATION_NO_GO],
        remove=[STATE_IMPLEMENTATION_GO],
        repo=(owner, name),
    )

    readback = fetch_pr_snapshot(repository, pr_number)
    has_go = STATE_IMPLEMENTATION_GO in readback["labels"]
    has_no_go = STATE_IMPLEMENTATION_NO_GO in readback["labels"]
    if has_go or not has_no_go:
        raise RuntimeError(f"{repository}#{pr_number} implementation-no-go label readback failed")
    logger.info(
        "%s#%d: reconciled to exclusive %s",
        repository,
        pr_number,
        STATE_IMPLEMENTATION_NO_GO,
    )
    return True


def list_org_repos(org: str) -> list[str]:
    """Return non-archived, non-fork repository names for an org.

    Mirrors :func:`hephaestus.automation.ensure_state_labels._gh_list_org_repos` as a
    leaf utility so this operator command has no import-time dependency on the loop.

    Args:
        org: Organization login.

    Returns:
        Sorted repository names.

    Raises:
        SystemExit: The ``gh`` call failed or returned invalid JSON.

    """
    try:
        result = gh_call(
            [
                "repo",
                "list",
                org,
                "--no-archived",
                "--source",
                "--limit",
                "200",
                "--json",
                "name,isArchived,isFork",
            ]
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            text(
                "gh repo list %(org)s failed (rc=%(rc)s): %(detail)s",
                org=org,
                rc=exc.returncode,
                detail=(exc.stderr or "").strip(),
            )
        ) from exc
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit(text("gh repo list returned invalid JSON: %(error)s", error=exc)) from exc
    if not isinstance(entries, list):
        raise SystemExit(text("gh repo list returned non-list JSON"))
    return sorted(
        str(entry["name"])
        for entry in entries
        if isinstance(entry, Mapping)
        and entry.get("name")
        and not entry.get("isArchived", False)
        and not entry.get("isFork", False)
    )


def open_pr_numbers(repository: str) -> list[int]:
    """Return the open pull request numbers for a repository.

    Args:
        repository: ``OWNER/NAME`` of the repository.

    Returns:
        Sorted pull request numbers.

    Raises:
        SystemExit: The ``gh`` call failed or returned invalid JSON.

    """
    try:
        result = gh_call(
            [
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--limit",
                "500",
                "--json",
                "number",
            ]
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            text(
                "gh pr list %(repository)s failed (rc=%(rc)s): %(detail)s",
                repository=repository,
                rc=exc.returncode,
                detail=(exc.stderr or "").strip(),
            )
        ) from exc
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit(
            text(
                "gh pr list %(repository)s returned invalid JSON: %(error)s",
                repository=repository,
                error=exc,
            )
        ) from exc
    if not isinstance(entries, list):
        raise SystemExit(
            text(
                "gh pr list %(repository)s returned non-list JSON",
                repository=repository,
            )
        )
    numbers = [
        int(entry["number"])
        for entry in entries
        if isinstance(entry, Mapping) and isinstance(entry.get("number"), int)
    ]
    return sorted(numbers)


_DETECT_REPO_FAILURE = (
    "Could not detect the current repo via 'gh repo view'. "
    "Pass --repo OWNER/NAME or --org NAME explicitly."
)


def _detect_current_repo_slug() -> str:
    """Derive ``owner/name`` from the current checkout's origin remote."""
    try:
        proc = gh_call(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(text(_DETECT_REPO_FAILURE)) from exc
    if not proc.stdout.strip():
        raise SystemExit(text(_DETECT_REPO_FAILURE))
    return proc.stdout.strip()


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``hephaestus-reconcile-implementation-labels`` parser."""
    parser = build_automation_parser(
        prog="hephaestus-reconcile-implementation-labels",
        description=text(
            "Clear a stale loop-owned state:implementation-go label when the verdict "
            "bound to the pull request's current head is NO-GO. One-way: this command "
            "can never grant implementation eligibility."
        ),
        add_agent=False,
        add_max_workers=False,
        add_github_throttle=True,
        dry_run_help="Report stale labels; mutate nothing.",
        verbose_help="Enable DEBUG logging.",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help=text("Single target repo (default: the current git checkout's origin)."),
    )
    target.add_argument(
        "--org",
        metavar="ORG",
        help=text("Apply to every open pull request in every non-archived, non-fork repo."),
    )
    parser.add_argument(
        "--pr",
        metavar="N",
        type=int,
        action="append",
        default=None,
        help=text("Limit to specific pull request numbers (repeatable)."),
    )
    return parser


def _resolve_slugs(args: argparse.Namespace) -> list[str] | None:
    """Resolve the target repositories, or None when the org enumeration is empty."""
    if args.org:
        names = list_org_repos(args.org)
        if not names:
            logger.warning("No repos returned for org %s - nothing to do.", args.org)
            return None
        return [f"{args.org}/{name}" for name in names]
    if args.repo:
        return [args.repo]
    return [_detect_current_repo_slug()]


def _run_reconciliation(
    slugs: Sequence[str],
    *,
    pr_numbers: Sequence[int] | None,
    dry_run: bool,
    shutdown: threading.Event,
) -> tuple[int, int]:
    """Inspect each target pull request and reconcile any stale label.

    Returns:
        An ``(inspected, reconciled)`` count pair.

    """
    inspected = 0
    reconciled = 0
    for slug in slugs:
        if shutdown.is_set():
            logger.warning("Interrupted; stopping before remaining repos.")
            break
        numbers = pr_numbers if pr_numbers is not None else open_pr_numbers(slug)
        for number in numbers:
            if shutdown.is_set():
                logger.warning("Interrupted; stopping before remaining PRs.")
                break
            inspected += 1
            if reconcile_pr(slug, number, dry_run=dry_run):
                reconciled += 1
    return inspected, reconciled


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``hephaestus-reconcile-implementation-labels``.

    Returns 0 on success. A reconciliation that fails its readback aborts the run,
    because a lost proof means a concurrent actor may own the state.
    """
    args = _build_parser().parse_args(argv)
    configure_github_throttle_from_args(args)
    configure_cli_logging(verbose=args.verbose, log_format=getattr(args, "log_format", "text"))

    shutdown = threading.Event()
    with terminal_guard(shutdown.set):
        slugs = _resolve_slugs(args)
        if slugs is None:
            if args.json:
                emit_json_status(0, "no-repos", org=args.org)
            return 0
        try:
            inspected, reconciled = _run_reconciliation(
                slugs,
                pr_numbers=args.pr,
                dry_run=args.dry_run,
                shutdown=shutdown,
            )
        except KeyboardInterrupt:
            if args.json:
                emit_json_status(130, message="interrupted")
            return 130

        if args.dry_run:
            logger.info(
                "[dry-run] Inspected %d PR(s) across %d repo(s); would reconcile %d.",
                inspected,
                len(slugs),
                reconciled,
            )
        else:
            logger.info(
                "Inspected %d PR(s) across %d repo(s); reconciled %d.",
                inspected,
                len(slugs),
                reconciled,
            )
        if args.json:
            emit_json_status(
                0,
                "dry-run" if args.dry_run else "ok",
                repos=len(slugs),
                inspected=inspected,
                reconciled=reconciled,
            )
        return 0


if __name__ == "__main__":
    sys.exit(main())
