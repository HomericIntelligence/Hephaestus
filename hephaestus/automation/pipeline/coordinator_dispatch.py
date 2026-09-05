import logging

import hephaestus.automation.pipeline.admission as _admission
import hephaestus.automation.pipeline.coordinator_types as ct
from hephaestus.agents.runtime import requires_plan_scope_guard
from hephaestus.automation.models import IssueInfo

from .coordinator_contract import _CoordinatorHost

logger = logging.getLogger("hephaestus.automation.pipeline.coordinator")


class ImplementationDispatcher(_CoordinatorHost):
    """Own dependency-safe implementation admission and file reservations."""

    def _drain_implementation(self) -> None:
        """Drain the implementation queue under topo order + file-overlap gating.

        REUSES :func:`admission.order_for_implementation` for dependency-safe
        normal-item ordering, then applies one age-prioritized, repo-scoped
        file-overlap admission pass to both normal and cross-repo
        same-number items. The overlap gate is engaged only when real
        parallelism is possible (#1623/#2451), mirroring the legacy
        ``serialize_file_overlap`` gate.

        The queue is STAGE-keyed, so one drain round can hold issues from
        several repos (#1795), and dependency ordering runs across the whole
        round on a shared issue-number space (``IssueInfo`` docstring). Two
        distinct work items therefore only conflict when they share the SAME
        ``(repo, issue)`` — that is the transient retry/fail-back re-enqueue we
        collapse below. Two DIFFERENT repos that happen to share an issue number
        (``A#71`` vs ``B#71``) are NOT duplicates and must both dispatch; the
        old code (and the pre-#2057 assert) keyed on issue number alone and
        would have silently dropped one or crashed (#2057).
        """
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
        dispatch_items, submission_claims = self._select_implementation_dispatch(items)
        for item in dispatch_items:
            if self.shutdown.is_set() or not self._admit(item):
                continue
            if not self._claim_selected_implementation_item(item):
                continue
            # Preserve the exact immutable snapshot used by the overlap gate.
            # A plan comment can change between admission and submission, but
            # it must not change the reservation that made this item eligible.
            if (claims := submission_claims.get(id(item))) is not None:
                item.payload[ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD] = set(claims)
            item.payload.pop(ct._FILE_OVERLAP_DEFERRALS_KEY, None)
            item.payload.pop(ct._FILE_OVERLAP_BLOCKED_CLAIMS_KEY, None)
            self._record_event("drain", ct.StageName.IMPLEMENTATION.value, self._item_key(item))
            self._run_item(item)

    def _select_implementation_dispatch(
        self, items: list[ct.WorkItem]
    ) -> tuple[list[ct.WorkItem], dict[int, set[_admission.PlanFileClaim]]]:
        """Order queued work and reserve immutable snapshots for this drain.

        Normal items keep their dependency-safe topological order. Ambiguous
        cross-repo same-number items have no safe number-keyed dependency
        representation, so they interleave with that normal sequence by
        deferral age. The common overlap selector then gives every candidate
        identical repo-scoped claim, snapshot, deferral, and warning behavior.
        """
        issue_items, ambiguous = self._index_issue_items(items)
        infos = [
            IssueInfo(
                number=number,
                title=str(item.payload.get("issue_title", "")),
                dependencies=list(item.payload.get("dependencies", [])),
            )
            for number, item in issue_items.items()
        ]
        # Stable sorting gives age its input priority. The priority-ready topo
        # traversal preserves it whenever a dependent becomes ready, while
        # still enforcing every in-queue dependency edge.
        infos.sort(
            key=lambda info: int(
                issue_items[info.number].payload.get(ct._FILE_OVERLAP_DEFERRALS_KEY, 0)
            ),
            reverse=True,
        )
        ordered = _admission.order_for_implementation(infos)
        normal_items = [issue_items[number] for number in ordered]
        ambiguous_items = [item for group in ambiguous.values() for item in group]
        if not self._overlap_serialization_enabled():
            # Cross-repo same-number items are distinct work even though the
            # shared issue-number dependency model cannot rank them (#2057).
            return [*normal_items, *ambiguous_items], {}
        candidates = self._merge_implementation_admission_priority(normal_items, ambiguous_items)
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

    def _merge_implementation_admission_priority(
        self,
        normal_items: list[ct.WorkItem],
        ambiguous_items: list[ct.WorkItem],
    ) -> list[tuple[ct.WorkItem, str]]:
        """Merge normal and ambiguous candidates without reversing dependencies.

        ``normal_items`` is already dependency-safe, so its relative order is
        immutable. Ambiguous items are independent of the number-keyed graph;
        stable age ordering lets an aged one run before a younger normal peer
        without allowing any normal dependent to overtake its prerequisite.
        Equal ages retain the historical normal-before-ambiguous order.
        """
        ambiguous_by_age = sorted(
            ambiguous_items,
            key=self._file_overlap_deferral_age,
            reverse=True,
        )
        normal_index = 0
        ambiguous_index = 0
        candidates: list[tuple[ct.WorkItem, str]] = []
        while normal_index < len(normal_items) or ambiguous_index < len(ambiguous_by_age):
            if ambiguous_index >= len(ambiguous_by_age):
                item = normal_items[normal_index]
                candidates.append((item, f"#{item.issue}"))
                normal_index += 1
                continue
            if normal_index >= len(normal_items):
                item = ambiguous_by_age[ambiguous_index]
                candidates.append((item, f"{item.repo}#{item.issue}"))
                ambiguous_index += 1
                continue
            normal_item = normal_items[normal_index]
            ambiguous_item = ambiguous_by_age[ambiguous_index]
            if self._file_overlap_deferral_age(normal_item) >= self._file_overlap_deferral_age(
                ambiguous_item
            ):
                candidates.append((normal_item, f"#{normal_item.issue}"))
                normal_index += 1
            else:
                candidates.append((ambiguous_item, f"{ambiguous_item.repo}#{ambiguous_item.issue}"))
                ambiguous_index += 1
        return candidates

    def _select_file_overlap_implementation_items(
        self,
        candidates: list[tuple[ct.WorkItem, str]],
    ) -> tuple[list[ct.WorkItem], dict[int, set[_admission.PlanFileClaim]]]:
        """Apply one repo-scoped overlap safety rule to every candidate class."""
        dispatch: list[ct.WorkItem] = []
        snapshots: dict[int, set[_admission.PlanFileClaim]] = {}
        selected_claims: set[_admission.PlanFileClaim] = set()
        candidate_ids = {id(item) for item, _identity in candidates}
        external_claims = self._active_implementation_file_claims(exclude_item_ids=candidate_ids)
        for item, identity in candidates:
            if item.issue is None:  # defensive: candidate construction excludes this case
                continue
            # Queued candidates do not block one another before selection.
            # The first selected item owns its claims for the rest of this pass.
            claimed = set(external_claims)
            claimed.update(selected_claims)
            blocked_claims = item.payload.get(ct._FILE_OVERLAP_BLOCKED_CLAIMS_KEY)
            if blocked_claims is not None and set(blocked_claims) == claimed:
                # The same active reservation still blocks this item. Polling
                # must remain quiet until that reservation changes.
                continue
            repo = (self.config.org, item.repo)
            payload_claims = item.payload.get(ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD)
            if payload_claims is None:
                planned = _admission._fetch_planned_files(item.issue, repo=repo)
                item_claims = {(repo, path) for path in planned} if planned else set()
                # Freeze the plan on first admission attempt, including while
                # blocked, so coordinator polling never repeats GitHub reads.
                item.payload[ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD] = set(item_claims)
            else:
                item_claims = set(payload_claims)
            # A PR may return from review before a coordinator-wide claim
            # refresh materializes its verified diff paths.  Its selected
            # reservation must include those realized paths immediately so a
            # later candidate in this same admission batch cannot overlap.
            changed_paths = item.payload.get("review_changed_paths")
            if isinstance(changed_paths, list):
                for changed_path in changed_paths:
                    if isinstance(changed_path, str) and changed_path:
                        item_claims.add((repo, changed_path))
                item.payload[ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD] = set(item_claims)
            if item_claims and (item_claims & claimed):
                self._record_file_overlap_deferral(item, identity)
                item.payload[ct._FILE_OVERLAP_BLOCKED_CLAIMS_KEY] = set(claimed)
                continue
            item.payload.pop(ct._FILE_OVERLAP_BLOCKED_CLAIMS_KEY, None)
            # Keep the host-owned reservation in sync with the selected
            # snapshot.  A returning PR already has its planned claims in
            # this map; without updating it here, its verified review paths
            # would disappear when the implementation job captures claims.
            self._implementation_file_claims[id(item)] = set(item_claims)
            selected_claims.update(item_claims)
            # Preserve an empty snapshot too: unknown plans fail open, but
            # must not be fetched again after being admitted.
            snapshots[id(item)] = set(item_claims)
            dispatch.append(item)
        return dispatch, snapshots

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
        for item_id, item_claims in self._implementation_file_claims.items():
            if item_id in excluded_ids:
                continue
            claims.update(item_claims)
        for active_claims in self._inflight_implementation_claims.values():
            claims.update(active_claims)
        for item in self.items:
            if id(item) in excluded_ids:
                continue
            if item.stage not in ct._REALIZED_DIFF_CLAIM_STAGES:
                continue
            item_claims = set(item.payload.get(ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD, ()))
            changed_paths = item.payload.get("review_changed_paths")
            if isinstance(changed_paths, list):
                repo = (self.config.org, item.repo)
                for changed_path in changed_paths:
                    if isinstance(changed_path, str) and changed_path:
                        item_claims.add((repo, changed_path))
            if item_claims:
                item.payload[ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD] = set(item_claims)
                self._implementation_file_claims[id(item)] = set(item_claims)
                claims.update(item_claims)
        return claims

    def _capture_implementation_file_claims(
        self, item: ct.WorkItem
    ) -> set[_admission.PlanFileClaim]:
        """Return the immutable claims reserved for an implementation sub-job.

        The host stores the exact approved-plan snapshot in the work-item
        payload. It stays with the work item for each implementation sub-job.
        This snapshot also bounds host publication when overlap serialization
        is disabled.
        """
        if item.stage is not ct.StageName.IMPLEMENTATION or item.issue is None:
            return set()
        item_id = id(item)
        selected = self._implementation_file_claims.get(item_id)
        if selected is None:
            payload_claims = item.payload.get(ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD)
            if payload_claims is None:
                capture_for_overlap = (
                    self.config.max_workers > 1 and self.config.serialize_file_overlap
                )
                if not capture_for_overlap and not requires_plan_scope_guard(self.config.agent):
                    return set()
                repo = (self.config.org, item.repo)
                planned = _admission._fetch_planned_files(item.issue, repo=repo)
                payload_claims = {(repo, path) for path in planned} if planned else set()
            selected = set(payload_claims)
            if not selected and requires_plan_scope_guard(self.config.agent):
                raise RuntimeError("approved plan manifest is unavailable or empty")
            self._implementation_file_claims[item_id] = selected
            item.payload[ct._IMPLEMENTATION_FILE_CLAIMS_PAYLOAD] = set(selected)
        if not selected and requires_plan_scope_guard(self.config.agent):
            raise RuntimeError("approved plan manifest is unavailable or empty")
        return set(selected)

    def _clear_implementation_file_claims_on_exit(
        self, item: ct.WorkItem, target: ct.StageName
    ) -> None:
        """Drop reservations only after the active PR lifecycle really exits."""
        if item.stage in ct._FILE_CLAIM_STAGES and target not in ct._FILE_CLAIM_STAGES:
            self._implementation_file_claims.pop(id(item), None)
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

    @staticmethod
    def _index_issue_items(
        items: list[ct.WorkItem],
    ) -> tuple[dict[int, ct.WorkItem], dict[int, list[ct.WorkItem]]]:
        """Index issue items by number for number-keyed topo/overlap dispatch.

        Returns ``(issue_items, ambiguous)``. Dispatch is driven by ordered issue
        NUMBERS, so items are indexed by number to look back up. A cross-repo
        same-number pair collides in that dict — those numbers move to
        ``ambiguous`` (issue number → its distinct items) and dispatch directly,
        bypassing the number-keyed topo/overlap gates, which cannot represent two
        items under one number. The ambiguity is inherent to the shared
        issue-number-space dependency model (``IssueInfo``); dispatching both is
        correct — neither is a duplicate (#2057). ``issue is None`` items are
        skipped (dispatched elsewhere / re-queued).
        """
        issue_items: dict[int, ct.WorkItem] = {}
        ambiguous: dict[int, list[ct.WorkItem]] = {}
        for it in items:
            if it.issue is None:
                continue
            if it.issue in ambiguous:
                ambiguous[it.issue].append(it)
            elif it.issue in issue_items:
                ambiguous[it.issue] = [issue_items.pop(it.issue), it]
            else:
                issue_items[it.issue] = it
        return issue_items, ambiguous

    def _admit(self, item: ct.WorkItem) -> bool:
        """Admission control: per-repo in-flight cap (O(1) Counter lookup)."""
        if getattr(self, "_auxiliary_pool_separate", False) and self._is_auxiliary_stage(
            item.stage
        ):
            return len(self.auxiliary_in_flight) < self.config.learning_workers
        return len(self.in_flight) < ct._work_window(self.config) and self.inflight_per_repo[
            item.repo
        ] < max(1, self.config.max_workers)
