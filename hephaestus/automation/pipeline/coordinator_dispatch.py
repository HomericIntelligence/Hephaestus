import logging
from collections import deque

import hephaestus.automation.pipeline.admission as _admission
import hephaestus.automation.pipeline.coordinator_types as ct
from hephaestus.automation.comment_identity import CommentAliasConflictError
from hephaestus.automation.models import IssueInfo
from hephaestus.automation.review_journal import CommentJournalReadError

from .coordinator_contract import _CoordinatorHost
from .routing import Disposition

logger = logging.getLogger("hephaestus.automation.pipeline.coordinator")


class ImplementationDispatcher(_CoordinatorHost):
    """Own dependency-safe implementation admission and file reservations."""

    def _drain_implementation(self) -> None:
        """Apply repository dependency order and file reservations before dispatch."""
        q = self.queues[ct.StageName.IMPLEMENTATION]
        if not len(q):
            return
        # Derive topology from a bounded snapshot, then lease only selected
        # items. A raw pop makes an item ownerless; each lease preserves its
        # original FIFO ticket while other ready work may still dispatch.
        for duplicate in self._implementation_duplicates(q.snapshot()):
            if not self._claim_selected_implementation_item(duplicate):
                return
            logger.warning(
                "implementation %s#%s already queued; dropping duplicate work item",
                duplicate.repo,
                duplicate.issue,
            )
            self._finish(
                duplicate,
                passed=True,
                reason=f"{duplicate.repo}#{duplicate.issue} superseded by queued duplicate",
            )
            if id(duplicate) in self._leases:
                return

        items = q.snapshot()
        dispatch_items = self._select_implementation_dispatch(items)
        for item in dispatch_items:
            if self.shutdown.is_set() or not self._admit(item):
                continue
            if not self._claim_selected_implementation_item(item):
                continue
            item.payload.pop(ct._FILE_OVERLAP_DEFERRALS_KEY, None)
            item.payload.pop(ct._FILE_OVERLAP_BLOCKED_CLAIMS_KEY, None)
            self._record_event("drain", ct.StageName.IMPLEMENTATION.value, self._item_key(item))
            self._run_item(item)

    def _select_implementation_dispatch(self, items: list[ct.WorkItem]) -> list[ct.WorkItem]:
        """Order each repository, then select ready work by its deferral age."""
        by_repo: dict[str, dict[int, ct.WorkItem]] = {}
        positions = {id(item): index for index, item in enumerate(items)}
        for item in items:
            if item.issue is not None:
                by_repo.setdefault(item.repo, {})[item.issue] = item
        sequences: list[deque[ct.WorkItem]] = []
        for issue_items in by_repo.values():
            infos = [
                IssueInfo(
                    number=number,
                    title=str(item.payload.get("issue_title", "")),
                    dependencies=list(item.payload.get("dependencies", [])),
                )
                for number, item in issue_items.items()
            ]
            infos.sort(
                key=lambda info: self._file_overlap_deferral_age(issue_items[info.number]),
                reverse=True,
            )
            order = _admission.order_for_implementation(infos)
            if order:
                sequences.append(deque(issue_items[number] for number in order))
        ordered_items: list[ct.WorkItem] = []
        while sequences:
            sequence = max(
                sequences,
                key=lambda ready: (
                    self._file_overlap_deferral_age(ready[0]),
                    -positions[id(ready[0])],
                ),
            )
            ordered_items.append(sequence.popleft())
            if not sequence:
                sequences.remove(sequence)
        if not self._overlap_serialization_enabled():
            return ordered_items
        candidates = [(item, f"{item.repo}#{item.issue}") for item in ordered_items]
        return self._select_file_overlap_implementation_items(candidates)

    def _overlap_serialization_enabled(self) -> bool:
        """Return whether this run needs parallel file-overlap reservations."""
        return self.config.serialize_file_overlap and self.config.max_workers > 1

    def _record_file_overlap_deferral(self, item: ct.WorkItem, identity: str) -> None:
        """Age a deferred implementation item and report persistent contention."""
        deferrals = int(item.payload.get(ct._FILE_OVERLAP_DEFERRALS_KEY, 0)) + 1
        item.payload[ct._FILE_OVERLAP_DEFERRALS_KEY] = deferrals
        log_deferral = (
            logger.warning if deferrals > self._file_overlap_warning_threshold else logger.info
        )
        log_deferral(
            "implementation %s deferred (file overlap); deferrals=%s threshold=%s",
            identity,
            deferrals,
            self._file_overlap_warning_threshold,
        )

    @staticmethod
    def _file_overlap_deferral_age(item: ct.WorkItem) -> int:
        """Return an item's current overlap-deferral age for stable priority."""
        return int(item.payload.get(ct._FILE_OVERLAP_DEFERRALS_KEY, 0))

    def _select_file_overlap_implementation_items(
        self,
        candidates: list[tuple[ct.WorkItem, str]],
    ) -> list[ct.WorkItem]:
        """Apply one repo-scoped overlap safety rule to every candidate class."""
        dispatch: list[ct.WorkItem] = []
        selected_claims: set[_admission.PlanFileClaim] = set()
        candidate_ids = {id(item) for item, _identity in candidates}
        fresh_candidate_ids = {
            id(item)
            for item, _identity in candidates
            if item.pr is None and not item.worktree and item.state in ("", "ENTER")
        }
        for item, identity in candidates:
            if item.issue is None:  # defensive: candidate construction excludes this case
                continue
            # Fresh work respects every started owner. Returning owners can
            # compete with queued peers without releasing claims to fresh work.
            excluded_ids = fresh_candidate_ids if id(item) in fresh_candidate_ids else candidate_ids
            claimed = self._active_implementation_file_claims(exclude_item_ids=excluded_ids)
            claimed.update(selected_claims)
            blocked_claims = item.payload.get(ct._FILE_OVERLAP_BLOCKED_CLAIMS_KEY)
            if blocked_claims is not None and set(blocked_claims) == claimed:
                # The same active reservation still blocks this item. Polling
                # must remain quiet until that reservation changes.
                continue
            repo = (self.config.org, item.repo)
            try:
                item_claims = set(self._read_implementation_file_claims(item))
            except CommentAliasConflictError as error:
                if self._claim_selected_implementation_item(item):
                    self._finish(item, passed=False, reason=f"plan admission failed: {error}")
                continue
            except CommentJournalReadError as error:
                if self._claim_selected_implementation_item(item):
                    self._defer_implementation_plan_read(item, error)
                continue
            # A PR may return from review before a coordinator-wide claim
            # refresh materializes its verified diff paths.  Its selected
            # reservation must include those realized paths immediately so a
            # later candidate in this same admission batch cannot overlap.
            changed_paths = item.payload.get("review_changed_paths")
            if isinstance(changed_paths, list):
                for changed_path in changed_paths:
                    if isinstance(changed_path, str) and changed_path:
                        item_claims.add((repo, changed_path))
                item.payload[ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD] = frozenset(item_claims)
            if item_claims and (item_claims & claimed):
                self._record_file_overlap_deferral(item, identity)
                item.payload[ct._FILE_OVERLAP_BLOCKED_CLAIMS_KEY] = set(claimed)
                continue
            item.payload.pop(ct._FILE_OVERLAP_BLOCKED_CLAIMS_KEY, None)
            selected_claims.update(item_claims)
            dispatch.append(item)
        return dispatch

    def _read_implementation_file_claims(
        self, item: ct.WorkItem
    ) -> frozenset[_admission.PlanFileClaim]:
        """Read once and retain the item's repository-qualified file reservation."""
        frozen_claims = item.payload.get(ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD)
        if frozen_claims is not None:
            return frozenset(frozen_claims)
        if item.issue is None:
            raise ValueError("plan admission requires an issue number")
        planned = _admission._fetch_planned_files(
            item.issue,
            github=self._ctx_for(item).github,
            deadline_s=self._monotonic() + self.config.gh_timeout,
            shutdown=self.shutdown,
        )
        repo = (self.config.org, item.repo)
        claims = frozenset((repo, path) for path in planned or ())
        item.payload[ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD] = claims
        return claims

    def _defer_implementation_plan_read(
        self, item: ct.WorkItem, error: CommentJournalReadError
    ) -> None:
        """Retry a plan read on the existing timer within the implementation budget."""
        if self.shutdown.is_set():
            self._park_resumable(item)
            return
        attempt = item.attempts.get("plan_admission", 0) + 1
        item.attempts["plan_admission"] = attempt
        budget = self._ctx_for(item).budget("implement")
        if attempt >= budget:
            self._finish(
                item,
                passed=False,
                reason=f"plan admission read exhausted after {attempt} attempts: {error}",
            )
            return
        item.payload["retry_delay_s"] = float(2 ** (attempt - 1))
        self._route_retry(item, ct.StageOutcome(Disposition.RETRY, "plan admission read failed"))

    @staticmethod
    def _implementation_duplicates(items: list[ct.WorkItem]) -> list[ct.WorkItem]:
        """Return non-first queued duplicates keyed by ``(repo, issue)`` (#2057)."""
        seen: set[tuple[str, int]] = set()
        duplicates: list[ct.WorkItem] = []
        for item in items:
            if item.issue is None:
                continue
            key = (item.repo, item.issue)
            if key in seen:
                duplicates.append(item)
            else:
                seen.add(key)
        return duplicates

    def _active_implementation_file_claims(
        self,
        *,
        exclude_item: ct.WorkItem | None = None,
        exclude_item_ids: set[int] | None = None,
    ) -> set[_admission.PlanFileClaim]:
        """Return active claims without the specified candidate ownership."""
        claims: set[_admission.PlanFileClaim] = set()
        excluded_ids = set(exclude_item_ids or ())
        if exclude_item is not None:
            excluded_ids.add(id(exclude_item))
        for item in self.items:
            if (
                id(item) in excluded_ids
                or item.result is not None
                or item.stage not in ct._FILE_CLAIM_STAGES
            ):
                continue
            item_claims = set(item.payload.get(ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD, ()))
            changed_paths = item.payload.get("review_changed_paths")
            if isinstance(changed_paths, list):
                repo = (self.config.org, item.repo)
                item_claims.update(
                    (repo, path) for path in changed_paths if isinstance(path, str) and path
                )
                item.payload[ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD] = frozenset(item_claims)
            claims.update(item_claims)
        return claims

    def _clear_implementation_file_claims_on_exit(
        self, item: ct.WorkItem, target: ct.StageName
    ) -> None:
        """Drop reservations only after the active PR lifecycle really exits."""
        if item.stage in ct._FILE_CLAIM_STAGES and target not in ct._FILE_CLAIM_STAGES:
            item.payload.pop(ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD, None)

    def _claim_selected_implementation_item(self, item: ct.WorkItem) -> bool:
        """Claim *item* at its current position, preserving FIFO retry order."""
        for index, queued in enumerate(self.queues[ct.StageName.IMPLEMENTATION].snapshot()):
            if queued is item:
                claimed = self._claim_item(ct.StageName.IMPLEMENTATION, index=index)
                if claimed is not item:  # pragma: no cover - coordinator-thread invariant
                    raise RuntimeError("implementation queue selected a different item")
                return True
        return False

    def _admit(self, item: ct.WorkItem) -> bool:
        """Admission control: per-repo in-flight cap (O(1) Counter lookup)."""
        if self._is_auxiliary_stage(item.stage):
            return len(self.auxiliary_in_flight) < self.config.learning_workers
        return len(self.in_flight) < ct._work_window(self.config) and self.inflight_per_repo[
            item.repo
        ] < max(1, self.config.max_workers)
