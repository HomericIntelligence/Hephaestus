"""Real :class:`~hephaestus.automation.pipeline.stages.base.StageGitHub` adapter.

Coordinator-owned GitHub accessor (epic #1809, coordinator slice #1817). This
module is the ONE place where the pipeline's coordinator-neutral mutator names
(``add_labels``, ``upsert_plan_comment``, ``create_pr``, ...) are mapped onto
the real ``github_api`` / ``pr_manager`` / ``_review_utils`` helpers.

It deliberately lives OUTSIDE ``hephaestus/automation/pipeline/``: the
architecture guard (``tests/unit/automation/pipeline/test_pipeline_architecture``)
forbids ``github_api`` mutator imports in any ``pipeline/*`` module, so the
adapter is coordinator-side by construction — stages only ever see it through
``StageContext.github``.

Dry-run contract (``stages/base.py`` :class:`StageGitHub` docstring): dry-run
is honored INSIDE this accessor. Every mutator logs ``[dry-run] would ...``
and skips the underlying ``gh`` call when the adapter was built with
``dry_run=True``; reads always hit GitHub so classification stays truthful.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hephaestus.automation import github_api, pr_manager
from hephaestus.automation._review_phase import _is_automation_owned_thread
from hephaestus.automation._review_utils import (
    close_issue_as_covered,
    ensure_state_dir,
    find_merged_closing_pr,
    find_merged_pr_for_issue,
    find_pr_for_issue,
    get_pr_head_branch,
)
from hephaestus.automation.arming_state import ArmingStateStore
from hephaestus.automation.ci_check_inspector import CICheckInspector
from hephaestus.automation.prompts.pr_review import (
    BLOCKING_SEVERITIES,
    SEVERITY_MARKER_PREFIX,
    VALID_SEVERITIES,
)
from hephaestus.automation.protocol import PLAN_COMMENT_MARKER
from hephaestus.automation.review_state import (
    PLAN_REVIEW_PREFIX,
    is_plan_review_go,
    latest_verdict,
)
from hephaestus.automation.state_labels import (
    ALL_IMPLEMENTATION_STATE_LABELS,
    ALL_STATE_LABELS,
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_LABEL_SPECS,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
    STATE_SKIP,
    has_label,
    is_implementation_go,
    is_plan_go,
)
from hephaestus.constants import read_timeout_env
from hephaestus.github.client import gh_call

logger = logging.getLogger(__name__)


def rate_limit_remaining() -> tuple[int, int] | None:
    """Return ``(remaining, reset_epoch)`` for the GraphQL budget, or ``None``.

    Feeds the coordinator's non-blocking rate gate. A blocking *sleeping* guard
    would be fatal for a single coordinator thread, so the pipeline timer-parks
    instead (see ``coordinator._rate_budget_ok``).
    """
    try:
        out = gh_call(["api", "rate_limit"])
    except (subprocess.SubprocessError, RuntimeError, OSError):
        return None
    try:
        data = json.loads(out.stdout)
        gql = data["resources"]["graphql"]
        return int(gql["remaining"]), int(gql["reset"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def rate_budget_ok(now_epoch: float | None = None) -> tuple[bool, float]:
    """Non-blocking GraphQL rate-budget gate for the coordinator.

    Args:
        now_epoch: Current epoch seconds (injectable for tests).

    Returns:
        ``(ok, park_delay_s)``. ``ok`` is False when the GraphQL budget is
        below ``HEPHAESTUS_RATE_GUARD_THRESHOLD`` (default 200) and the
        ``HEPHAESTUS_RATE_GUARD`` env gate is enabled; ``park_delay_s`` is the
        seconds until the upstream reset (+5s slack, mirroring the legacy
        guard), 0.0 when ``ok``.

    """
    if os.environ.get("HEPHAESTUS_RATE_GUARD", "1") == "0":
        return True, 0.0
    threshold = read_timeout_env("HEPHAESTUS_RATE_GUARD_THRESHOLD", 200)
    rl = rate_limit_remaining()
    if rl is None:
        return True, 0.0
    remaining, reset_epoch = rl
    if remaining >= threshold:
        return True, 0.0
    now = time.time() if now_epoch is None else now_epoch
    return False, max(0.0, reset_epoch - now + 5.0)


def _with_severity_marker(comment: dict[str, Any]) -> str:
    """Prepend the ``<!-- hephaestus-severity: X -->`` marker line (#1856).

    An absent/unknown severity is written as ``major`` (blocking) so an
    unclassifiable thread never silently unblocks a GO, and so the pre-#1856
    all-blocking behavior is reproduced until the reviewer's severity is seeded.
    """
    sev = str(comment.get("severity") or "").strip().lower()
    if sev not in VALID_SEVERITIES:
        sev = "major"
    body = str(comment.get("body") or "")
    if body.startswith(SEVERITY_MARKER_PREFIX):
        return body  # already marked (idempotent re-post)
    return f"{SEVERITY_MARKER_PREFIX} {sev} -->\n{body}"


def _thread_severity_is_blocking(thread: dict[str, Any]) -> bool:
    """Return True if the thread's recovered severity is blocking; missing means blocking."""
    body = str(thread.get("body") or "")
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(SEVERITY_MARKER_PREFIX) and stripped.endswith("-->"):
            sev = stripped[len(SEVERITY_MARKER_PREFIX) : -3].strip().lower()
            return sev in BLOCKING_SEVERITIES
    return True


class PipelineGitHub:
    """Coordinator-owned GitHub accessor implementing ``StageGitHub``.

    Read surface delegates to the existing helpers verbatim; the mutator
    surface maps the coordinator-neutral names onto ``github_api`` /
    ``pr_manager`` / ``_review_utils`` mutators, honoring dry-run inside each
    mutator (log-and-skip) per the ``StageGitHub`` protocol docstring.
    """

    def __init__(
        self,
        org: str,
        *,
        repo: str | None = None,
        dry_run: bool = False,
        repo_root: Path | None = None,
    ) -> None:
        """Initialize the accessor.

        Args:
            org: GitHub organization.
            repo: Optional repository name. When set, every supported gh CLI
                read/write is explicitly scoped with ``--repo org/repo``.
            dry_run: When True, every mutator logs-and-skips.
            repo_root: Repo checkout root anchoring the drive-green arming
                state dir (defaults to the current working directory).

        """
        self.org = org
        self.repo = repo
        self.dry_run = dry_run
        self._repo_root = repo_root or Path.cwd()
        self._arming = ArmingStateStore(lambda: ensure_state_dir(self._repo_root))
        self._inspector = CICheckInspector(
            get_pr_branch=lambda pr: get_pr_head_branch(pr) or "",
            # Reads stay live even under pipeline dry-run so CI classification
            # is truthful; only mutators log-and-skip.
            options_provider=lambda: SimpleNamespace(dry_run=False),
        )

    @property
    def _repo_slug(self) -> str | None:
        if not self.repo:
            return None
        return f"{self.org}/{self.repo}"

    def _owner_name(self) -> tuple[str, str]:
        """Return explicit owner/name for repo-scoped GitHub API calls."""
        if self.repo is None:
            raise RuntimeError("repo-scoped GitHub operation requires a repo")
        return self.org, self.repo

    def _graphql(self, query: str, **fields: int | str) -> dict[str, Any]:
        """Run a repo-scoped GraphQL query with explicit owner/repo fields."""
        owner, name = self._owner_name()
        argv = ["api", "graphql", "-f", f"query={query}"]
        for key, value in {"owner": owner, "name": name, **fields}.items():
            argv.extend(["-F", f"{key}={value}"])
        result = gh_call(argv)
        data = json.loads(result.stdout or "{}")
        if not isinstance(data, dict):
            raise RuntimeError("GraphQL response was not an object")
        github_api._check_graphql_errors(data, "repo-scoped pipeline GraphQL")
        return data

    def _with_repo(self, argv: list[str]) -> list[str]:
        """Append an explicit repo selector when this accessor is repo-scoped."""
        if self._repo_slug is None:
            return argv
        return [*argv, "--repo", self._repo_slug]

    def _gh(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return gh_call(self._with_repo(argv), **kwargs)

    def _label_names(self) -> set[str]:
        if self._repo_slug is None:
            # Org-scoped fallback: always re-fetch so a multithreaded
            # coordinator never trusts another repo's slug-keyed entry (#1858).
            return github_api.gh_list_labels(refresh=True)
        result = self._gh(["label", "list", "--json", "name", "--limit", "200"])
        data = json.loads(result.stdout or "[]")
        return {str(item["name"]) for item in data if isinstance(item, dict) and item.get("name")}

    def _create_label(self, name: str) -> None:
        spec = STATE_LABEL_SPECS.get(name, {})
        cmd = ["label", "create", name, "--color", spec.get("color", "ededed"), "--force"]
        if desc := spec.get("description", ""):
            cmd.extend(["--description", desc])
        self._gh(cmd)

    def _add_labels(self, issue_number: int, labels: list[str]) -> None:
        if not labels:
            return
        existing = self._label_names()
        for label in labels:
            if label not in existing:
                self._create_label(label)
                existing.add(label)
        cmd = ["issue", "edit", str(issue_number)]
        for label in labels:
            cmd.extend(["--add-label", label])
        self._gh(cmd)

    def _remove_labels(self, issue_number: int, labels: list[str]) -> None:
        if not labels:
            return
        existing = self._label_names()
        labels_to_remove = [label for label in labels if label in existing]
        if not labels_to_remove:
            return
        cmd = ["issue", "edit", str(issue_number)]
        for label in labels_to_remove:
            cmd.extend(["--remove-label", label])
        self._gh(cmd)

    @staticmethod
    def _label_names_from_payload(payload: dict[str, Any]) -> list[str]:
        labels = payload.get("labels")
        if not isinstance(labels, list):
            return []
        names: list[str] = []
        for label in labels:
            if isinstance(label, str):
                names.append(label)
            elif isinstance(label, dict) and isinstance(label.get("name"), str):
                names.append(str(label["name"]))
        return names

    @staticmethod
    def _latest_plan_review_body(comments: Any) -> str | None:
        if not isinstance(comments, list):
            return None
        latest_review_body: str | None = None
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body")
            if isinstance(body, str) and body.startswith(PLAN_REVIEW_PREFIX):
                latest_review_body = body
        return latest_review_body

    @staticmethod
    def _comments_have_plan(comments: Any) -> bool:
        if not isinstance(comments, list):
            return False
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body")
            if not isinstance(body, str):
                continue
            stripped = body.lstrip()
            if stripped.startswith(PLAN_REVIEW_PREFIX):
                continue
            if stripped.startswith(PLAN_COMMENT_MARKER):
                return True
        return False

    def _backfill_plan_go(self, issue_number: int) -> None:
        if self.dry_run:
            logger.info("[dry-run] would backfill %s on #%d", STATE_PLAN_GO, issue_number)
            return
        try:
            self._add_labels(issue_number, [STATE_PLAN_GO])
        except Exception as exc:
            logger.warning(
                "Issue #%d: failed to backfill %s in %s: %s",
                issue_number,
                STATE_PLAN_GO,
                self._repo_slug,
                exc,
            )

    def _find_pr_for_issue(self, issue_number: int, *, state: str) -> int | None:
        branch_name = f"{issue_number}-auto-impl"
        result = self._gh(
            [
                "pr",
                "list",
                "--head",
                branch_name,
                "--state",
                state,
                "--json",
                "number",
                "--limit",
                "1",
            ]
        )
        pr_data = json.loads(result.stdout or "[]")
        if pr_data:
            return int(pr_data[0]["number"])

        result = self._gh(
            [
                "pr",
                "list",
                "--state",
                state,
                "--search",
                f"Closes #{issue_number} in:body",
                "--json",
                "number,body",
                "--limit",
                "10",
            ]
        )
        closes_pattern = re.compile(rf"^Closes #{issue_number}\b", re.MULTILINE)
        for candidate in json.loads(result.stdout or "[]"):
            if closes_pattern.search(candidate.get("body") or ""):
                return int(candidate["number"])
        return None

    def _repo_unresolved_threads(self, pr_number: int) -> list[dict[str, Any]]:
        """List unresolved PR review threads for this accessor's explicit repo."""
        query = (
            "query($owner:String!,$name:String!,$number:Int!){"
            "  repository(owner:$owner,name:$name){"
            "    pullRequest(number:$number){"
            "      reviewThreads(first:100){"
            "        nodes{ id isResolved path line side:diffSide "
            "comments(first:20){ nodes{ body author{ login } } } }"
            "      }"
            "    }"
            "  }"
            "}"
        )
        data = self._graphql(query, number=int(pr_number))
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        threads: list[dict[str, Any]] = []
        for node in nodes:
            if node.get("isResolved"):
                continue
            comment_nodes = node.get("comments", {}).get("nodes", [])
            first_comment = comment_nodes[0] if comment_nodes else {}
            comments: list[dict[str, str]] = []
            authors: list[str] = []
            for comment in comment_nodes:
                author_node = comment.get("author")
                author = author_node.get("login") if isinstance(author_node, dict) else ""
                author = author or ""
                if author:
                    authors.append(author)
                comments.append({"body": comment.get("body") or "", "author": author})
            threads.append(
                {
                    "id": node["id"],
                    "path": node.get("path", ""),
                    "line": node.get("line"),
                    "side": node.get("side") or "RIGHT",
                    "body": first_comment.get("body", ""),
                    "author": authors[0] if authors else "",
                    "authors": authors,
                    "comments": comments,
                }
            )
        return threads

    def _repo_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        """Fetch issue comment ids/bodies for explicit-repo marker upserts."""
        query = (
            "query($owner:String!,$name:String!,$number:Int!){"
            "  repository(owner:$owner,name:$name){"
            "    issue(number:$number){"
            "      comments(first:100){ nodes{ databaseId body } }"
            "    }"
            "  }"
            "}"
        )
        data = self._graphql(query, number=int(issue_number))
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("issue", {})
            .get("comments", {})
            .get("nodes", [])
        )
        return [node for node in nodes if isinstance(node, dict)]

    def _repo_review_threads_for_review(self, pr_number: int, review_id: str) -> list[str]:
        """Return unresolved review-thread ids created by one REST review."""
        query = (
            "query($owner:String!,$name:String!,$number:Int!){"
            "  repository(owner:$owner,name:$name){"
            "    pullRequest(number:$number){"
            "      reviewThreads(first:100){"
            "        nodes{ id isResolved comments(first:1){ "
            "nodes{ pullRequestReview{ id } } } }"
            "      }"
            "    }"
            "  }"
            "}"
        )
        try:
            data = self._graphql(query, number=int(pr_number))
        except (subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as exc:
            logger.warning("Could not fetch review threads for PR #%s: %s", pr_number, exc)
            return []
        nodes = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        seen: dict[str, None] = {}
        for node in nodes:
            if node.get("isResolved"):
                continue
            first_comments = node.get("comments", {}).get("nodes", [])
            if not first_comments:
                continue
            review = first_comments[0].get("pullRequestReview") or {}
            if review.get("id") != review_id:
                continue
            thread_id = node.get("id")
            if thread_id:
                seen[thread_id] = None
        return list(seen)

    def _skip(self, what: str) -> bool:
        """Return True (and log) when dry-run should skip a mutation."""
        if self.dry_run:
            logger.info("[dry-run] would %s", what)
            return True
        return False

    # -- read surface --------------------------------------------------------

    def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
        """Fetch issue JSON (``github_api.issues.gh_issue_json``)."""
        if self._repo_slug is not None:
            result = self._gh(
                ["issue", "view", str(issue_number), "--json", "number,title,state,labels,body"]
            )
            data = json.loads(result.stdout or "{}")
            if not isinstance(data, dict):
                raise RuntimeError(f"Failed to fetch issue #{issue_number}: non-object response")
            for field in ("title", "body"):
                value = data.get(field)
                if isinstance(value, str):
                    data[field] = github_api.strip_null_bytes(value)
            return data
        return github_api.gh_issue_json(issue_number)

    def find_merged_closing_pr(self, issue_number: int) -> int | None:
        """Return the merged PR closing this issue (``_review_utils``)."""
        if self._repo_slug is not None:
            return self._find_pr_for_issue(issue_number, state="merged")
        return find_merged_closing_pr(issue_number)

    def find_merged_pr_for_issue(self, issue_number: int) -> int | None:
        """Return the merged PR for this issue (tri-state seeding lookup)."""
        if self._repo_slug is not None:
            return self._find_pr_for_issue(issue_number, state="merged")
        return find_merged_pr_for_issue(issue_number)

    def find_pr_for_issue(self, issue_number: int) -> int | None:
        """Return an open PR covering this issue (``_review_utils``)."""
        if self._repo_slug is not None:
            return self._find_pr_for_issue(issue_number, state="open")
        return find_pr_for_issue(issue_number)

    def has_existing_plan(self, issue_number: int) -> bool:
        """Labels-first plan gate incl. comment-scan backfill (``is_plan_review_go``)."""
        if self._repo_slug is not None:
            try:
                result = self._gh(
                    ["issue", "view", str(issue_number), "--json", "labels,comments"],
                    check=False,
                )
                data = json.loads(result.stdout or "{}")
            except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError):
                return False
            if not isinstance(data, dict):
                return False

            labels = self._label_names_from_payload(data)
            if is_plan_go(labels):
                return True
            if has_label(labels, STATE_PLAN_NO_GO):
                return False

            comments = data.get("comments")
            latest_review_body = self._latest_plan_review_body(comments)
            if latest_review_body is not None:
                if latest_verdict(latest_review_body) != "GO":
                    return False
                self._backfill_plan_go(issue_number)
                return True

            return bool(self._comments_have_plan(comments))
        return bool(is_plan_review_go(issue_number))

    def get_pr_head_branch(self, pr_number: int) -> str | None:
        """Return the PR's head branch (``_review_utils.get_pr_head_branch``)."""
        if self._repo_slug is not None:
            try:
                result = self._gh(
                    ["pr", "view", str(pr_number), "--json", "headRefName"],
                    check=False,
                )
                data = json.loads(result.stdout or "{}")
                value = data.get("headRefName") if isinstance(data, dict) else None
                return str(value) if value else None
            except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError):
                return None
        return get_pr_head_branch(pr_number)

    def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
        """Return ``(has_go, has_no_go)`` (``pr_manager``)."""
        if self._repo_slug is not None:
            try:
                result = self._gh(["pr", "view", str(pr_number), "--json", "labels"], check=False)
                data = json.loads(result.stdout or "{}")
                labels = self._label_names_from_payload(data if isinstance(data, dict) else {})
            except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError):
                return (False, False)
            return is_implementation_go(labels), has_label(labels, STATE_IMPLEMENTATION_NO_GO)
        return pr_manager.pr_has_implementation_state_label(pr_number)

    def _unresolved_threads(self, pr_number: int) -> list[dict[str, Any]]:
        """Fetch unresolved threads (repo-scoped or legacy). Fails open to []."""
        if self._repo_slug is not None:
            return self._repo_unresolved_threads(pr_number)
        try:
            return github_api.gh_pr_list_unresolved_threads(pr_number, dry_run=False)
        except Exception as exc:  # legacy path fails open (parity with existing code)
            logger.warning("PR #%s: could not list unresolved threads: %s", pr_number, exc)
            return []

    def count_unresolved_threads(self, pr_number: int) -> tuple[int, int]:
        """Return ``(automation_unresolved, human_unresolved)`` thread counts.

        Mirrors ``_review_phase._count_unresolved_threads_blocking_go``
        (#1152): resolves nothing. Current-repo legacy helpers still fail
        open on fetch errors; repo-scoped pipeline runs query the configured
        repo directly so unresolved human threads are not hidden.
        """
        threads = self._unresolved_threads(pr_number)
        if not threads:
            return (0, 0)
        current_login = github_api.gh_current_login()
        automation = sum(1 for t in threads if _is_automation_owned_thread(t, current_login))
        return (automation, len(threads) - automation)

    def count_unresolved_threads_by_severity(self, pr_number: int) -> tuple[int, int, int]:
        """Return ``(blocking_automation, minor_automation, human)`` (#1856).

        Severity is read from the ``<!-- hephaestus-severity: X -->`` marker
        prepended at post time; an automation thread with a missing/garbled marker
        counts as BLOCKING (fail-safe). Resolves nothing.
        """
        threads = self._unresolved_threads(pr_number)
        if not threads:
            return (0, 0, 0)
        current_login = github_api.gh_current_login()
        blocking = minor = human = 0
        for t in threads:
            if _is_automation_owned_thread(t, current_login):
                if _thread_severity_is_blocking(t):
                    blocking += 1
                else:
                    minor += 1
            else:
                human += 1
        return (blocking, minor, human)

    def resolve_automation_threads(self, pr_number: int) -> int:
        """Resolve unresolved AUTOMATION-owned threads; return the count (#1856).

        Never resolves human threads. Used by the GO gate to clear advisory
        minor/nitpick threads the reviewer waved so ``required_review_thread_
        resolution`` does not re-block the armed PR at merge (merge_wait.py:427).
        """
        if self._skip(f"resolve automation threads on PR #{pr_number}"):
            return 0
        threads = self._unresolved_threads(pr_number)
        current_login = github_api.gh_current_login()
        resolved = 0
        for t in threads:
            if _is_automation_owned_thread(t, current_login) and t.get("id"):
                github_api.gh_pr_resolve_thread(str(t["id"]), dry_run=self.dry_run)
                resolved += 1
        return resolved

    def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
        """Return the merge_wait PR-state read, or ``None`` on failure.

        Re-housed from ``ci_driver.CIDriver._gh_pr_state``: one ``gh pr view``
        returning ``{state, headRefOid, mergedAt, mergeStateStatus,
        baseRefName}``.
        """
        try:
            result = self._gh(
                [
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "state,headRefOid,mergedAt,mergeStateStatus,baseRefName",
                ]
            )
            data = json.loads(result.stdout or "{}")
            return data if isinstance(data, dict) else None
        except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            logger.warning("PR #%s: gh_pr_state read failed: %s", pr_number, exc)
            return None

    def failing_required_check_names(self, pr_number: int) -> list[str]:
        """Names of required checks currently failing (``CICheckInspector``)."""
        if self._repo_slug is not None:
            checks = self.pr_checks(pr_number)
            required = [c for c in checks if c.get("required")] or checks
            return [
                c.get("name", "")
                for c in required
                if c.get("status") == "completed" and c.get("conclusion") == "failure"
            ]
        return self._inspector.failing_required_check_names(pr_number)

    def pending_required_check_names(self, pr_number: int) -> list[str]:
        """Names of required checks still in flight (``CICheckInspector``)."""
        if self._repo_slug is not None:
            checks = self.pr_checks(pr_number)
            required = [c for c in checks if c.get("required")] or checks
            return [c.get("name", "") for c in required if c.get("status") != "completed"]
        return self._inspector.pending_required_check_names(pr_number)

    def pr_checks(self, pr_number: int) -> list[dict[str, Any]]:
        """All checks for the PR (``gh_pr_checks``)."""
        if self._repo_slug is not None:
            try:
                result = self._gh(
                    ["pr", "checks", str(pr_number), "--json", "name,state,bucket,workflow"],
                    log_on_error=False,
                )
            except subprocess.CalledProcessError as exc:
                if github_api._is_gh_pr_checks_no_checks_error(exc):
                    return []
                raise
            raw = json.loads(result.stdout or "[]")
            return [github_api._map_pr_check(item) for item in raw]
        return github_api.gh_pr_checks(pr_number, dry_run=False)

    def pr_is_genuinely_stuck(self, pr_number: int) -> bool:
        """Return True iff the PR cannot merge without manual action (``pr_manager``)."""
        if self._repo_slug is not None:
            try:
                result = self._gh(
                    [
                        "pr",
                        "view",
                        str(pr_number),
                        "--json",
                        "mergeStateStatus,mergeable,statusCheckRollup",
                    ],
                    check=False,
                )
                pr = json.loads(result.stdout or "{}")
            except (subprocess.SubprocessError, RuntimeError, OSError, json.JSONDecodeError):
                return False
            merge_state = str(pr.get("mergeStateStatus") or "").upper()
            mergeable = str(pr.get("mergeable") or "").upper()
            if merge_state in {"DIRTY", "CONFLICTING"} or mergeable == "CONFLICTING":
                return True
            rollup = pr.get("statusCheckRollup")
            return isinstance(rollup, list) and any(
                isinstance(check, dict)
                and check.get("conclusion") in {"FAILURE", "CANCELLED", "TIMED_OUT"}
                for check in rollup
            )
        return pr_manager.pr_is_genuinely_stuck(pr_number)

    def drive_green_learn_terminal(self, issue_number: int) -> bool:
        """Return True when the post-merge ``/learn`` is already terminal.

        Mirrors ``ci_driver.CIDriver._learn_record_terminal`` over the issue's
        arming record: captured/succeeded timestamps or a terminal
        ``learn_status`` mean ``/learn`` must never fire again (#848).
        """
        record = self._arming.load(issue_number) or {}
        if record.get("learn_captured_at") or record.get("learn_succeeded_at"):
            return True
        return str(record.get("learn_status") or "").lower() in {"succeeded", "failed"}

    # -- mutator surface (dry-run honored here) -------------------------------

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably add labels (``gh_issue_add_labels``)."""
        if self._skip(f"add labels {labels} to #{issue_number}"):
            return
        if self._repo_slug is not None:
            self._add_labels(issue_number, labels)
            return
        github_api.gh_issue_add_labels(issue_number, labels)

    def remove_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably remove labels (``gh_issue_remove_labels``)."""
        if self._skip(f"remove labels {labels} from #{issue_number}"):
            return
        if self._repo_slug is not None:
            self._remove_labels(issue_number, labels)
            return
        github_api.gh_issue_remove_labels(issue_number, labels)

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        """Atomically add+remove labels in a single ``gh issue edit``."""
        if self._skip(f"edit labels on #{issue_number} (+{add} -{remove})"):
            return
        if self._repo_slug is not None:
            if add:
                existing = self._label_names()
                for label in add:
                    if label not in existing:
                        self._create_label(label)
                        existing.add(label)
            cmd = ["issue", "edit", str(issue_number)]
            for label in add:
                cmd.extend(["--add-label", label])
            for label in remove:
                cmd.extend(["--remove-label", label])
            if add or remove:
                self._gh(cmd)
            return
        if add:
            github_api.gh_issue_add_labels(issue_number, add)
        if remove:
            github_api.gh_issue_remove_labels(issue_number, remove)

    def close_issue_as_covered(self, issue_number: int, pr_number: int) -> None:
        """Close the issue as covered by a merged PR (``_review_utils``)."""
        if self._skip(f"close #{issue_number} as covered by PR #{pr_number}"):
            return
        if self._repo_slug is not None:
            self._gh(
                [
                    "issue",
                    "close",
                    str(issue_number),
                    "--comment",
                    f"Closed by merged PR #{pr_number} (Closes #{issue_number}).",
                ],
                check=False,
            )
            return
        close_issue_as_covered(issue_number, pr_number)

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        """Upsert the single plan comment keyed on ``PLAN_COMMENT_MARKER``."""
        if self._skip(f"upsert plan comment on #{issue_number}"):
            return
        if self._repo_slug is not None:
            comments = self._repo_issue_comments(issue_number)
            matching = [
                c
                for c in comments
                if str(c.get("body", "")).startswith(PLAN_COMMENT_MARKER)
                and c.get("databaseId") is not None
            ]
            if not matching:
                with github_api._body_file(body) as path:
                    self._gh(["issue", "comment", str(issue_number), "--body-file", path])
                return

            owner, name = self._owner_name()
            target_id = int(matching[-1]["databaseId"])
            for dup in matching[:-1]:
                dup_id = dup.get("databaseId")
                if dup_id is not None:
                    gh_call(
                        [
                            "api",
                            "--method",
                            "DELETE",
                            f"/repos/{owner}/{name}/issues/comments/{int(dup_id)}",
                        ]
                    )
            with github_api._body_file(body) as path:
                gh_call(
                    [
                        "api",
                        "--method",
                        "PATCH",
                        f"/repos/{owner}/{name}/issues/comments/{target_id}",
                        "-F",
                        f"body=@{path}",
                    ]
                )
            return
        github_api.gh_issue_upsert_comment(issue_number, PLAN_COMMENT_MARKER, body)

    def create_pr(self, issue_number: int, branch: str, title: str, body: str) -> int:
        """Durably ensure the PR exists and return its number (idempotent).

        ``find_pr_for_issue`` first (reuse an existing open PR), then
        ``gh_pr_create`` with the *given* title/body — NOT
        ``pr_manager.ensure_pr_created``, which would discard the stage's
        composed body (protocol docstring). Dry-run returns 0 (no PR).
        """
        existing = self.find_pr_for_issue(issue_number)
        if existing:
            return existing
        if self._skip(f"create PR for #{issue_number} from {branch!r}"):
            return 0
        if self._repo_slug is not None:
            github_api._assert_body_has_closes(body)
            github_api._assert_branch_commits_signed(branch, base="main")
            with github_api._body_file(body) as body_path:
                result = self._gh(
                    [
                        "pr",
                        "create",
                        "--head",
                        branch,
                        "--base",
                        "main",
                        "--title",
                        github_api.strip_null_bytes(title),
                        "--body-file",
                        body_path,
                    ]
                )
            output = result.stdout.strip()
            match = re.search(r"/pull/(\d+)", output)
            if match:
                return int(match.group(1))
            return int(output.split("/")[-1])
        return github_api.gh_pr_create(branch, title, body)

    def post_pr_comment(self, pr_number: int, body: str) -> None:
        """Post an explanatory PR comment (``gh_issue_comment`` channel)."""
        if self._skip(f"post comment on PR #{pr_number}"):
            return
        if self._repo_slug is not None:
            with github_api._body_file(body) as path:
                self._gh(["issue", "comment", str(pr_number), "--body-file", path])
            return
        github_api.gh_issue_comment(pr_number, body)

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        """Apply ``state:implementation-no-go`` (``pr_manager``)."""
        if self._skip(f"mark PR #{pr_number} implementation-no-go"):
            return
        if self._repo_slug is not None:
            self._add_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
            self._remove_labels(pr_number, [STATE_IMPLEMENTATION_GO])
            return
        pr_manager.mark_pr_implementation_no_go(pr_number)

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        """Apply ``state:implementation-go`` (``pr_manager``)."""
        if self._skip(f"mark PR #{pr_number} implementation-go"):
            return
        if self._repo_slug is not None:
            self._add_labels(pr_number, [STATE_IMPLEMENTATION_GO])
            self._remove_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
            return
        pr_manager.mark_pr_implementation_go(pr_number)

    def defer_auto_merge(self, pr_number: int) -> None:
        """Keep auto-merge disabled until implementation GO (``pr_manager``)."""
        if self._skip(f"defer auto-merge on PR #{pr_number}"):
            return
        if self._repo_slug is not None:
            result = self._gh(
                ["pr", "view", str(pr_number), "--json", "autoMergeRequest"],
                check=False,
            )
            data = json.loads(result.stdout or "{}")
            if data.get("autoMergeRequest") is not None:
                self._gh(["pr", "merge", str(pr_number), "--disable-auto"])
            return
        pr_manager.ensure_pr_auto_merge_deferred(pr_number)

    def arm_auto_merge(self, pr_number: int) -> None:
        """Arm squash auto-merge after implementation GO (``pr_manager``)."""
        if self._skip(f"arm auto-merge on PR #{pr_number}"):
            return
        if self._repo_slug is not None:
            self._gh(["pr", "merge", str(pr_number), "--auto", "--squash"])
            return
        pr_manager.enable_auto_merge_after_implementation_go(pr_number)

    def post_review_threads(
        self, pr_number: int, threads: list[dict[str, Any]], summary: str
    ) -> list[str]:
        """Post surviving review threads (``gh_pr_review_post``)."""
        if self._skip(f"post {len(threads)} review thread(s) on PR #{pr_number}"):
            return []
        if self._repo_slug is not None:
            if threads:
                diff_result = self._gh(["pr", "diff", str(pr_number)], check=False)
                threads = github_api._filter_comments_to_diff(threads, diff_result.stdout or "")
            review_comments = [
                {
                    "path": c["path"],
                    "line": c["line"],
                    "side": c.get("side", "RIGHT"),
                    "body": _with_severity_marker(c),
                }
                for c in threads
            ]
            owner, name = self._owner_name()
            request_body = json.dumps(
                {"body": summary, "event": "COMMENT", "comments": review_comments}
            )
            with github_api._body_file(request_body) as input_path:
                result = gh_call(
                    [
                        "api",
                        "-X",
                        "POST",
                        f"repos/{owner}/{name}/pulls/{pr_number}/reviews",
                        "--input",
                        input_path,
                    ]
                )
            review = json.loads(result.stdout or "{}")
            review_node_id = review.get("node_id")
            if not review_node_id:
                logger.warning("Posted PR review on #%s but no review node id returned", pr_number)
                return []
            return self._repo_review_threads_for_review(pr_number, str(review_node_id))
        return github_api.gh_pr_review_post(pr_number, threads, summary)

    def arm_drive_green(self, issue_number: int, pr_number: int, head_sha: str) -> None:
        """Persist the drive-green arming record (``ArmingStateStore.save``).

        Record shape mirrors ``ci_driver.CIDriver._arm_drive_green``; an
        already-terminal record is never overwritten (its learn evidence is
        the /learn dedupe key).
        """
        if self._skip(f"arm drive-green record for #{issue_number} (PR #{pr_number})"):
            return
        if self.drive_green_learn_terminal(issue_number):
            return
        self._arming.save(
            issue_number,
            {
                "pr_number": pr_number,
                "pr_head_branch": self.get_pr_head_branch(pr_number) or "",
                "head_sha_at_arming": head_sha,
                "armed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "learn_attempted_at": None,
                "learn_captured_at": None,
                "learn_status": None,
                "learn_succeeded_at": None,
            },
        )

    def mark_drive_green_learn_result(self, issue_number: int, *, succeeded: bool) -> None:
        """Record the post-merge ``/learn`` outcome on the arming record.

        Mirrors ``post_merge_processor.mark_drive_green_learn_result`` (minus
        the session-evidence enrichment, which stays with the legacy driver
        until the cutover issue): written before FINISH_PASS so a restart can
        never replay ``/learn`` for the same merged PR.
        """
        if self._skip(f"record drive-green learn result for #{issue_number}"):
            return
        record = self._arming.load(issue_number) or {}
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["learn_attempted_at"] = timestamp
        if succeeded:
            record["learn_status"] = "succeeded"
            record["learn_succeeded_at"] = timestamp
            record["learn_captured_at"] = timestamp
        else:
            record["learn_status"] = "failed"
            record["learn_succeeded_at"] = None
            record["learn_captured_at"] = None
        self._arming.save(issue_number, record)

    # -- repo-stage surface (#1817) -------------------------------------------

    def skip_epics(self, epics_labels: dict[int, list[str]]) -> None:
        """Tag epics ``state:skip`` via the sanctioned chokepoint.

        The ONE seeding write (doc row "Epic tagging is the one seeding
        write; done BEFORE excluding"), executed by the coordinator through
        ``github_api.skip_epics``.
        """
        if self._skip(f"tag epics {sorted(epics_labels)} {STATE_SKIP}"):
            return
        if self._repo_slug is not None:
            for number, labels in epics_labels.items():
                if STATE_SKIP not in labels:
                    self._add_labels(number, [STATE_SKIP])
            return
        github_api.skip_epics(epics_labels)

    def ensure_state_labels(self) -> None:
        """Ensure the ``state:*`` label vocabulary exists on the repo.

        Repo-stage step 1 [M] (doc section 1): idempotent
        ``_ensure_labels_exist`` over the full ``state_labels`` vocabulary.
        """
        wanted = [*ALL_STATE_LABELS, *ALL_IMPLEMENTATION_STATE_LABELS, STATE_SKIP]
        if self._skip(f"ensure state labels exist: {wanted}"):
            return
        if self._repo_slug is not None:
            existing = self._label_names()
            for label in wanted:
                if label not in existing:
                    self._create_label(label)
            return
        github_api._ensure_labels_exist(wanted)
