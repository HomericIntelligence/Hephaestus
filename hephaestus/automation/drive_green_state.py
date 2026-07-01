"""Drive-green state stores and arming lifecycle helpers."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import file_lock

from .arming_state import ArmingStateStore
from .git_utils import issue_ref
from .models import WorkerResult

logger = logging.getLogger(__name__)


class LastCIFixStore:
    """Persists PR head SHAs for CI fixes pushed by drive-green."""

    def __init__(
        self,
        *,
        state_dir_provider: Callable[[], Path],
        gh_pr_state: Callable[[int], dict[str, Any] | None],
    ) -> None:
        """Initialise the marker store."""
        self._state_dir = state_dir_provider
        self._gh_pr_state = gh_pr_state

    def marker_path(self, pr_number: int) -> Path:
        """Return the marker path for a PR's last CI-fix head."""
        return self._state_dir() / f"last-ci-fix-{pr_number}.json"

    def record_head(self, pr_number: int) -> None:
        """Persist the current PR head SHA after a successful CI fix push."""
        gh_state = self._gh_pr_state(pr_number) or {}
        head_sha = str(gh_state.get("headRefOid") or "")
        if not head_sha:
            return
        try:
            write_secure(
                self.marker_path(pr_number),
                json.dumps({"pr_number": pr_number, "head_sha": head_sha}) + "\n",
            )
        except OSError as exc:
            logger.warning(
                "Issue: failed to write last-ci-fix marker for PR #%s: %s",
                pr_number,
                exc,
            )

    def already_pushed_for_current_head(self, issue_number: int, pr_number: int) -> bool:
        """Return True when the current PR head matches the last CI-fix marker."""
        marker = self.marker_path(pr_number)
        if not marker.exists():
            return False
        try:
            recorded = str(dict(json.loads(marker.read_text())).get("head_sha") or "")
        except (OSError, json.JSONDecodeError):
            return False
        if not recorded:
            return False
        gh_state = self._gh_pr_state(pr_number) or {}
        current = str(gh_state.get("headRefOid") or "")
        return bool(current) and current == recorded


class DriveGreenArmingCoordinator:
    """Coordinates drive-green arming records and post-merge learn capture."""

    def __init__(
        self,
        *,
        state_dir_provider: Callable[[], Path],
        status_tracker_provider: Callable[[], Any],
        shared_pr_issues_provider: Callable[[], dict[int, list[int]]],
        gh_pr_state: Callable[[int], dict[str, Any] | None],
        wait_for_pr_terminal: Callable[[int, int], str],
        run_drive_green_learnings: Callable[[int, int], bool],
        run_drive_green_compact: Callable[[int, int], bool],
        mark_drive_green_learn_result: Callable[[int, dict[str, Any], bool], None],
    ) -> None:
        """Initialise arming lifecycle dependencies."""
        self._state_dir = state_dir_provider
        self._status = status_tracker_provider
        self._shared_pr_issues = shared_pr_issues_provider
        self._gh_pr_state = gh_pr_state
        self._wait_for_pr_terminal = wait_for_pr_terminal
        self._run_learn = run_drive_green_learnings
        self._run_compact = run_drive_green_compact
        self._mark_learn = mark_drive_green_learn_result
        self.store = ArmingStateStore(state_dir_provider)

    def record_arming(self, pr_number: int, pr_head_branch: str, pr_head_sha: str) -> None:
        """Record arming for every issue that resolved to ``pr_number``."""
        siblings = self._shared_pr_issues().get(pr_number, [])
        if not siblings:
            return
        armed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for issue_num in siblings:
            existing = self.store.load(issue_num) or {}
            if self._learn_record_terminal(existing):
                continue
            record = {
                "pr_number": pr_number,
                "pr_head_branch": pr_head_branch,
                "head_sha_at_arming": pr_head_sha,
                "armed_at": armed_at,
                "learn_attempted_at": None,
                "learn_captured_at": None,
                "learn_status": None,
                "learn_succeeded_at": None,
            }
            self.store.save(issue_num, record)
            logger.info(
                "Issue #%s: armed for /learn on merge of PR #%s (head=%s @ %s)",
                issue_num,
                pr_number,
                pr_head_branch,
                pr_head_sha[:8] if pr_head_sha else "?",
            )

    def check_on_drive_start(self, issue_number: int, pr_number: int) -> WorkerResult | None:
        """Handle an existing arming record before normal drive work starts."""
        record = self.store.load(issue_number)
        if record is None:
            return None
        if self._learn_record_terminal(record):
            logger.info(
                "Issue #%s: /learn already terminal (%s at %s); skipping further drive",
                issue_number,
                record.get("learn_status") or "succeeded",
                record.get("learn_captured_at")
                or record.get("learn_succeeded_at")
                or record.get("learn_attempted_at"),
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        gh_state = self._gh_pr_state(pr_number)
        if gh_state is None:
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        state = (gh_state.get("state") or "").upper()
        current_sha = gh_state.get("headRefOid") or ""
        if state == "MERGED":
            return self._capture_learn(issue_number, pr_number, record)
        if state == "CLOSED":
            logger.info(
                "Issue #%s: PR #%s was CLOSED without merging; dropping arming record",
                issue_number,
                pr_number,
            )
            self.store.clear(issue_number)
            return None
        armed_sha = record.get("head_sha_at_arming") or ""
        if current_sha and armed_sha and current_sha != armed_sha:
            logger.info(
                "Issue #%s: PR #%s head advanced from %s to %s since arming; re-entering drive",
                issue_number,
                pr_number,
                armed_sha[:8],
                current_sha[:8],
            )
            self.store.clear(issue_number)
            return None
        outcome = self._wait_for_pr_terminal(issue_number, pr_number)
        if outcome == "MERGED":
            return self._capture_learn(issue_number, pr_number, record)
        if outcome == "CLOSED":
            self.store.clear(issue_number)
            return None
        if outcome in ("FAILING", "DIRTY"):
            self.store.clear(issue_number)
            return None
        return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)

    def sweep_orphaned_records(self) -> None:
        """Drop closed records and capture missed learn output for merged orphans."""
        with file_lock(self._state_dir() / "orphan-sweep.lock"):
            self._sweep_orphaned_records_locked()

    def _sweep_orphaned_records_locked(self) -> None:
        try:
            records = sorted(self._state_dir().glob("drive-green-armed-*.json"))
        except OSError as exc:
            logger.info("Arming sweep skipped: state_dir scan failed (%s)", exc)
            return
        if not records:
            return
        logger.info("Sweeping %s arming record(s) for orphan resolution", len(records))
        for path in records:
            try:
                issue_number = int(path.stem.rsplit("-", 1)[-1])
            except ValueError:
                logger.info("Arming sweep: ignoring malformed filename %s", path.name)
                continue
            record = self.store.load(issue_number)
            if record is None or self._learn_record_terminal(record):
                continue
            pr_number = record.get("pr_number")
            if not isinstance(pr_number, int):
                logger.info(
                    "Arming sweep: dropping record %s with non-integer pr_number",
                    path.name,
                )
                self.store.clear(issue_number)
                continue
            gh_state = self._gh_pr_state(pr_number)
            if gh_state is None:
                continue
            state = (gh_state.get("state") or "").upper()
            if state == "MERGED":
                self._capture_learn(issue_number, pr_number, record)
            elif state == "CLOSED":
                logger.info(
                    "Arming sweep: issue #%s / PR #%s CLOSED-not-merged; dropping record",
                    issue_number,
                    pr_number,
                )
                self.store.clear(issue_number)

    @staticmethod
    def _learn_record_terminal(record: dict[str, Any]) -> bool:
        if record.get("learn_captured_at") or record.get("learn_succeeded_at"):
            return True
        return str(record.get("learn_status") or "").lower() in {"succeeded", "failed"}

    def _capture_learn(
        self, issue_number: int, pr_number: int, record: dict[str, Any]
    ) -> WorkerResult:
        logger.info("Issue #%s: PR #%s detected as MERGED; capturing /learn", issue_number, pr_number)
        self._status().update_slot(0, f"{issue_ref(issue_number)}: capturing post-merge /learn")
        learn_succeeded = self._run_learn(issue_number, pr_number)
        self._run_compact(issue_number, pr_number)
        self._mark_learn(issue_number, record, learn_succeeded)
        return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
