import sys

from hephaestus.automation.issue_waves import WAVE_NON_CODE_INTENT_PAYLOAD

from .coordinator_contract import _CoordinatorHost
from .coordinator_types import *
from .stages.repo import SYNCED_MAIN_SHA_KEY

# This collaborator consumes the façade's shared type namespace by design.
# ruff: noqa: F403, F405


def _compat(name: str) -> Any:
    """Resolve mutable coordinator constants from the façade at call time."""
    return getattr(sys.modules["hephaestus.automation.pipeline.coordinator"], name)


logger = logging.getLogger("hephaestus.automation.pipeline.coordinator")


class SourceCoordinator(_CoordinatorHost):
    """Own bounded repository, issue, and PR source cursors."""

    _repo_entry_source: _RepoEntrySource | None

    def _externalize_repo_issue_source(self, item: WorkItem, source: RepoIssueSource) -> bool:
        """Retire setup work and enroll its cursor in the bounded FIFO registry."""
        if len(self._repo_issue_sources) >= _work_window(self.config):
            return False

        item.payload.pop("_repo_issue_source", None)
        self._wave_mode_active |= source.wave_lease is not None
        self._repo_issue_sources.append(_ActiveRepoIssueSource(repo=item.repo, source=source))
        self._release_source_lease(item)
        self._release_work_permit(item)
        self._seen_item_ids.discard(id(item))
        with suppress(ValueError):  # provisional item is normally tracked
            self.items.remove(item)
        self._record_event("repo_source_activate", item.repo, len(self._repo_issue_sources))
        return True

    def _repo_source_slots_used(self) -> int:
        """Count active cursors and live REPO setup reservations."""
        candidates: dict[int, WorkItem] = {}
        for queue in self.queues.values():
            for item in queue.snapshot():
                candidates[id(item)] = item
        for _wake, _sequence, item in self.timers:
            candidates[id(item)] = item
        for item in self.in_flight.values():
            candidates[id(item)] = item
        for lease in self._leases.values():
            candidates[id(lease.item)] = lease.item
        reservations = sum(
            item.kind is ItemKind.REPO and id(item) in self._live_work_permit_ids
            for item in candidates.values()
        )
        return len(self._repo_issue_sources) + reservations

    def _repo_source_can_admit(self) -> bool:
        """Return whether a detached repo cursor may classify and admit one issue."""
        return self.live_work_count < _work_window(self.config) and all(
            self.queues[stage].can_offer() for stage in _DIRECT_ISSUE_ENTRY_STAGES
        )

    def _drain_repo_issue_sources(self) -> None:
        """Run one round-robin admission attempt for every active repo cursor."""
        for _ in range(len(self._repo_issue_sources)):
            active = self._repo_issue_sources.popleft()
            if self._drain_repo_issue_source(active):
                self._repo_issue_sources.append(active)

    def _drain_repo_issue_source(  # noqa: C901 - source lifecycle is intentionally linear
        self, active: _ActiveRepoIssueSource
    ) -> bool:
        """Consume one detached repository cursor until one child is admitted.

        A pending row stays in ``source.pending`` until every possible entry
        queue and the global permit budget can accept it; no classified
        product is retained. Tracker-shaped issues are admitted for semantic
        planning instead of being skipped from metadata alone.

        Returns:
            ``True`` while this cursor remains active, otherwise ``False``
            after normal exhaustion or a recorded discovery failure.

        """
        repo = active.repo
        source = active.source
        while True:
            metadata = source.pending
            if metadata is None:
                try:
                    metadata = next(source.metadata)
                except StopIteration:
                    self._record_event("repo_source_complete", repo, source.seeded_count)
                    return False
                except Exception as exc:
                    logger.warning("repo:%s: discovery source failed: %s", repo, exc)
                    self._record_repo_source_failure(repo, f"discovery failed: {exc}")
                    return False

            try:
                number = int(metadata["number"])
            except (KeyError, TypeError, ValueError) as exc:
                self._record_repo_source_failure(
                    repo, f"discovery failed: malformed metadata: {exc}"
                )
                return False
            if (
                self.live_work_count >= _work_window(self.config)
                or not self._repo_source_can_admit()
            ):
                source.pending = metadata
                return True
            github = self._ctx_for_repo(repo).github
            try:
                facts = _seeding.seed_issue_from_github(number, github)
                if source.wave_lease is None and STATE_PLAN_BLOCKED in facts.labels:
                    github.ensure_blocked_audit(number)
                entry = _seeding.seed_entry_from_facts(facts)
                if source.wave_lease is not None:
                    entry = wave_entry_from_facts(
                        source.wave_lease,
                        facts,
                        entry,
                        repo_root=Path(str(self._ctx_for_repo(repo).paths.repo_root)),
                        org=self.config.org,
                        repo=repo,
                    )
                scope_stages = self.config.scope.stages if self.config.scope is not None else None
                if source.wave_lease is None or entry.stage is not StageName.FINISHED:
                    stage, reason, passed = self._scope_seed_decision(
                        number, entry.stage, entry.reason, scope_stages
                    )
                    entry = replace(entry, stage=stage, reason=reason, passed=passed)
                if entry.stage is None:
                    logger.info("[%s] excluded: %s", repo, entry.reason)
                    source.pending = None
                    self._progress = True
                    return True
                new_item = self._entry_to_item(entry, repo)
                if new_item.stage is StageName.FINISHED and new_item.result is None:
                    new_item.result = ItemResult(
                        passed=entry.passed,
                        reason=entry.reason,
                        final_stage=StageName.FINISHED,
                    )
                self._restore_learning_intents(new_item, entry.stage, entry.reason)
                if new_item.stage not in {StageName.REPO, StageName.FINISHED, StageName.LEARNING}:
                    self._pass_work_count += 1
                if source.wave_lease is not None:
                    new_item.payload[WAVE_LEASE_PAYLOAD] = source.wave_lease
                    if entry.non_code:
                        if entry.stage is StageName.FINISHED:
                            new_item.payload[WAVE_NON_CODE_PAYLOAD] = True
                        else:
                            new_item.payload[WAVE_NON_CODE_INTENT_PAYLOAD] = {
                                "reason": entry.reason,
                                "extra_labels": list(entry.non_code_labels),
                                "evidence_digest": entry.non_code_evidence_digest,
                                "repository_revision": entry.non_code_repository_revision,
                                "explanation": entry.non_code_explanation,
                                "retired": entry.non_code_retired,
                            }
                if source.base_main_sha is not None:
                    new_item.payload[SYNCED_MAIN_SHA_KEY] = source.base_main_sha
                if self._push_item(new_item, new_item.stage, enter=True, defer_if_full=True):
                    source.pending = None
                    source.seeded_count += 1
                    self._progress = True
                    return True
                source.pending = metadata
                return True
            except Exception as exc:
                logger.warning("repo:%s: issue #%d classification failed: %s", repo, number, exc)
                self._record_repo_source_failure(repo, f"discovery failed: {exc}")
                return False

    def _record_repo_source_failure(self, repo: str, reason: str) -> None:
        """Retain a bounded terminal failure after a detached cursor aborts."""
        item = WorkItem(repo=repo, kind=ItemKind.REPO, stage=StageName.FINISHED)
        item.result = ItemResult(passed=False, reason=reason, final_stage=StageName.REPO)
        item.payload["entry_stage"] = StageName.REPO.value
        self.items.append(item)
        self._record_terminal_result(item)

    def _seed_products(self, item: WorkItem) -> None:
        """Push a terminal repo item's discovered products into entry queues."""
        if item.kind is not ItemKind.REPO:
            return
        for product in item.payload.pop("products", []):
            if product.get("stage") is None:
                logger.info("[%s] excluded: %s", item.repo, product.get("reason", ""))
                continue
            new_item = product_to_work_item(item.repo, product)
            if new_item is None:  # pragma: no cover - guarded by stage check above
                continue
            if new_item.stage is StageName.FINISHED:
                new_item.result = ItemResult(
                    passed=True,
                    reason=product.get("reason", "already finished"),
                    final_stage=StageName.FINISHED,
                )
            elif new_item.stage is not StageName.REPO:
                self._pass_work_count += 1
            self._push_item(new_item, new_item.stage, enter=True)

    def _live_issue_keys(self) -> set[tuple[str, int]]:
        """Return ``(repo, issue)`` keys currently queued (any stage) or in-flight.

        The identity set the upstream idempotency guard consults: an ISSUE item is
        "live" if a WorkItem for the same ``(repo, issue)`` sits in any stage queue
        or in ``in_flight``. Cross-repo same-number issues are distinct (#2058).
        """
        keys: set[tuple[str, int]] = set()
        for q in self.queues.values():
            for it in q.snapshot():
                if it.kind is ItemKind.ISSUE and it.issue is not None:
                    keys.add((it.repo, it.issue))
        for it in self.in_flight.values():
            if it.kind is ItemKind.ISSUE and it.issue is not None:
                keys.add((it.repo, it.issue))
        for it in self.auxiliary_in_flight.values():
            if it.kind is ItemKind.ISSUE and it.issue is not None:
                keys.add((it.repo, it.issue))
        for lease in self._leases.values():
            it = lease.item
            if it.kind is ItemKind.ISSUE and it.issue is not None:
                keys.add((it.repo, it.issue))
        return keys

    def _push_item(
        self,
        item: WorkItem,
        stage: StageName,
        enter: bool,
        *,
        defer_if_full: bool = False,
    ) -> bool:
        """Push *item* into *stage*'s queue (the single push chokepoint).

        Every durable GitHub mutation for this transition already happened
        inside the stage, immediately before the outcome that got us here.

        Upstream idempotency guard (#2107): a genuinely NEW ISSUE work item whose
        ``(repo, issue)`` is already queued (any stage) or in-flight is refused —
        it never enters the pipeline, so the drain-level dedup (#2058) is not even
        exercised. Object identity (``_seen_item_ids``) distinguishes a new item
        from an already-tracked item re-pushing itself on timer/retry/fail-back/
        advance, which must always be allowed through.

        ``defer_if_full`` makes bounded capacity an ordinary backpressure
        result rather than an exception. It is reserved for timer wake-ups,
        whose heap entry must remain the item's owner until the stage accepts
        it. All ordinary callers retain the strict overflow invariant.

        Returns:
            ``True`` when the queue accepted the item; ``False`` when a
            duplicate seed was skipped or a deferred queue is full.

        """
        is_new_item = id(item) not in self._seen_item_ids
        if (
            is_new_item
            and item.kind is ItemKind.ISSUE
            and item.issue is not None
            and (item.repo, item.issue) in self._live_issue_keys()
        ):
            logger.info("seed skipped: #%s already queued/in-flight in %s", item.issue, item.repo)
            return False
        if is_new_item and not self._try_acquire_work_permit(item, stage):
            logger.debug(
                "global live-work capacity reached (%d); deferring %s",
                _work_window(self.config),
                self._item_key(item),
            )
            return False
        acquired_permit = is_new_item
        if not self.queues[stage].offer(item):
            if acquired_permit:
                self._release_work_permit(item)
            if defer_if_full:
                return False
            raise OverflowError("StageQueue is full")
        self._clear_implementation_file_claims_on_exit(item, stage)
        item.stage = stage
        if enter:
            item.state = "ENTER"
            item.payload["_enter_pending"] = True
            item.add_history_event(stage, item.state, note="enqueued")
        if is_new_item:
            self._seen_item_ids.add(id(item))
            self.items.append(item)
            item.payload.setdefault("entry_stage", stage.value)
        self._record_event("push", stage.value, self._item_key(item))
        return True

    @staticmethod
    def _item_key(item: WorkItem) -> str:
        """Human-readable item identity for logs and the event log."""
        if item.kind is ItemKind.REPO:
            return item.repo
        if item.kind is ItemKind.PR:
            return f"{item.repo}!{item.pr}"
        return f"{item.repo}#{item.issue}"

    def _seed_pass(self) -> int:
        """Seed one pass from CLI scope (repos / --issues / --prs).

        Returns:
            The number of items pushed (repo seeds included).

        """
        # A reseed pass creates fresh WorkItems for the same logical issues.
        # Keep final status semantics aligned with _effective_items(): the
        # latest pass supersedes earlier attempts without retaining an
        # unbounded logical-identity map in the terminal aggregate.
        self._terminal_summary.reset()
        self._pass_work_count = 0
        has_direct_scope = bool(self.config.issues or self.config.prs)
        discovery_repos = [] if has_direct_scope else self.config.repos
        # Repository discovery is a source, not a list of pre-built
        # ``SeedEntry``/``WorkItem`` values.  Keep the legacy empty call so
        # direct test seams and any non-repository synthetic entries retain
        # their established contract; production returns no entries here.
        entries = _seeding.seed_from_cli([], [], [])
        self._begin_repo_entry_source(discovery_repos if not entries else [])
        default_repo = self.config.repos[0] if self.config.repos else ""
        pushed = 0
        for entry in entries:
            if entry.stage is None:
                logger.info("seed excluded: %s", entry.reason)
                continue
            item = self._entry_to_item(entry, self.config.repos[0] if self.config.repos else "")
            if item.stage not in (StageName.REPO, StageName.FINISHED):
                self._pass_work_count += 1
            if item.stage is StageName.FINISHED and item.result is None:
                item.result = ItemResult(
                    passed=entry.passed, reason=entry.reason, final_stage=StageName.FINISHED
                )
            if self._push_item(item, item.stage, enter=True):
                pushed += 1
        if has_direct_scope:
            pushed += self._begin_direct_scope_bootstrap(default_repo)
        else:
            # No explicit cursor exists in an unscoped pass. Keep the calls
            # for their established empty-source cleanup semantics without
            # manufacturing a checkout pin outside the direct bootstrap.
            self._begin_direct_pr_source(default_repo, "")
            self._begin_direct_issue_source(default_repo, "")
        return (
            pushed
            + self._drain_repo_entry_source()
            + (0 if has_direct_scope else self._drain_direct_pr_source())
            + (0 if has_direct_scope else self._drain_direct_issue_source())
        )

    def _begin_direct_scope_bootstrap(self, repo: str) -> int:
        """Enqueue the one checkout proof required by an explicit CLI scope."""
        self._direct_scope_bootstrap_pending = True
        if not repo:
            self._direct_scope_bootstrap_pending = False
            item = WorkItem(repo="", kind=ItemKind.REPO, stage=StageName.FINISHED)
            item.result = ItemResult(
                passed=False,
                reason="explicit --issues/--prs scope requires exactly one repository",
                final_stage=StageName.FINISHED,
            )
            return int(self._push_item(item, StageName.FINISHED, enter=True))
        item = WorkItem(repo=repo, kind=ItemKind.REPO, stage=StageName.REPO)
        item.payload[DIRECT_SCOPE_BOOTSTRAP_KEY] = True
        return int(self._push_item(item, StageName.REPO, enter=True))

    def _begin_repo_entry_source(self, repos: list[str]) -> None:
        """Initialize one FIFO source for this pass's repository discovery.

        Strict source-order fairness is intentional: an active repository
        holds its REPO-stage lease until its bounded issue cursor finishes, so
        the next repository is admitted only after that stage slot is free.
        This prevents both an unbounded source spill and starvation from
        repeatedly retrying a later repository ahead of an earlier one.
        """
        if self.config.repo_source_factory is not None:
            self._repo_entry_source = _RepoEntrySource(repos=self.config.repo_source_factory())
        elif repos:
            self._repo_entry_source = _RepoEntrySource(repos=iter(repos))
        else:
            self._repo_entry_source = None

    def _drain_repo_entry_source(self) -> int:
        """Admit repository discovery items from the FIFO source when safe."""
        source = self._repo_entry_source
        if source is None:
            return 0

        pushed = 0
        while (
            self.live_work_count < _work_window(self.config)
            and self.queues[StageName.REPO].can_offer()
            and self._repo_source_slots_used() < _work_window(self.config)
        ):
            repo = source.pending
            if repo is None:
                try:
                    repo = next(source.repos)
                except StopIteration:
                    self._repo_entry_source = None
                    break
                except Exception as exc:
                    raise RuntimeError(f"repository discovery source failed: {exc}") from exc
            item = WorkItem(repo=repo, kind=ItemKind.REPO, stage=StageName.REPO)
            if not self._push_item(item, StageName.REPO, enter=True):
                # The coordinator is single-threaded and the predicate above
                # reserves both capacities. Retain exactly one retry value if
                # an injected/custom queue declines unexpectedly.
                source.pending = repo
                break
            source.pending = None
            pushed += 1
        return pushed

    def _begin_direct_issue_source(self, repo: str, base_sha: str) -> None:
        """Initialize one bounded cursor for this pass's ``--issues`` input."""
        self._direct_issue_source = None
        if not self.config.issues:
            return
        open_issues = _admission._filter_open_issues(repo, self.config.issues)
        unique_open_issues = list(dict.fromkeys(open_issues))
        self._direct_issue_source = _DirectIssueSource(
            repo=repo,
            issues=deque(unique_open_issues),
            base_sha=base_sha,
            run_nonce=uuid.uuid4().hex,
            wave_lease=self._direct_wave_lease,
        )

    def _begin_direct_pr_source(self, repo: str, base_sha: str) -> None:
        """Initialize one bounded cursor for this pass's ``--prs`` input."""
        self._direct_pr_source = None
        if self.config.prs:
            self._direct_pr_source = _DirectPrSource(
                repo=repo,
                prs=iter(self.config.prs),
                base_sha=base_sha,
                wave_lease=self._direct_wave_lease,
            )

    def _direct_issue_queues_can_accept(self) -> bool:
        """Return whether one direct issue can be classified and enqueued now."""
        return self.live_work_count < _work_window(self.config) and all(
            self.queues[stage].can_offer() for stage in _DIRECT_ISSUE_ENTRY_STAGES
        )

    def _prepare_direct_issue_item(
        self,
        source: _DirectIssueSource,
        issue: int,
        active_claims: frozenset[_admission.PlanFileClaim],
        *,
        overlap_enabled: bool,
    ) -> tuple[WorkItem | None, bool]:
        """Classify one source issue and snapshot its overlap reservation."""
        existing_pr, branch = self._direct_issue_identity(source.repo, issue, source.run_nonce)
        github = self._ctx_for_repo(source.repo).github
        if source.wave_lease is None:
            entry = self._seed_direct_issue_entry(source.repo, issue, github=github)
        else:
            facts = _seeding.seed_issue_from_github(issue, github)
            entry = wave_entry_from_facts(
                source.wave_lease,
                facts,
                _seeding.seed_entry_from_facts(facts),
                repo_root=Path(str(self._ctx_for_repo(source.repo).paths.repo_root)),
                org=self.config.org,
                repo=source.repo,
            )
        if entry.stage is None:
            logger.info("seed excluded: %s", entry.reason)
            return None, False
        item = self._prepare_direct_item(entry, source.repo, source.base_sha, source.run_nonce)
        self._restore_learning_intents(item, entry.stage, entry.reason)
        if existing_pr is not None:
            item.branch = branch
        if source.wave_lease is not None:
            item.payload[WAVE_LEASE_PAYLOAD] = source.wave_lease
            if entry.non_code:
                if entry.stage is StageName.FINISHED:
                    item.payload[WAVE_NON_CODE_PAYLOAD] = True
                else:
                    item.payload[WAVE_NON_CODE_INTENT_PAYLOAD] = {
                        "reason": entry.reason,
                        "extra_labels": list(entry.non_code_labels),
                        "evidence_digest": entry.non_code_evidence_digest,
                        "repository_revision": entry.non_code_repository_revision,
                        "explanation": entry.non_code_explanation,
                        "retired": entry.non_code_retired,
                    }
        if overlap_enabled and item.stage is StageName.IMPLEMENTATION:
            repo = (self.config.org, item.repo)
            planned = _admission._fetch_planned_files(issue, repo=repo)
            item_claims = {(repo, path) for path in planned} if planned else set()
            if item_claims and item_claims.intersection(active_claims):
                return None, True
            item.payload[_IMPLEMENTATION_FILE_CLAIMS_PAYLOAD] = set(item_claims)
        return item, False

    def _drain_direct_issue_source(self) -> int:
        """Pull explicit issues directly into queues without a seed spill buffer.

        An issue is classified only after its eventual destination is
        guaranteed to have capacity. With parallel overlap serialization, a
        bounded deque lets one full source pass rotate conflicting issues and
        admit the first independent candidate. A fully blocked pass is cached
        against the immutable active-claim set, preventing hot-loop GitHub
        plan fetches until implementation ownership changes.
        """
        source = self._direct_issue_source
        if source is None:
            return 0
        active_claims = frozenset(self._active_implementation_file_claims())
        overlap_enabled = self._overlap_serialization_enabled()
        if overlap_enabled and source.overlap_blocked_claims == active_claims:
            return 0
        source.overlap_blocked_claims = None
        pushed = 0
        scanned = 0
        scan_limit = len(source.issues)
        blocked_by_overlap = False
        while self._direct_issue_queues_can_accept() and source.issues and scanned < scan_limit:
            issue = source.issues.popleft()
            scanned += 1
            item, overlaps = self._prepare_direct_issue_item(
                source,
                issue,
                active_claims,
                overlap_enabled=overlap_enabled,
            )
            if overlaps:
                source.issues.append(issue)
                blocked_by_overlap = True
                continue
            if item is None:
                continue
            if self._push_item(item, item.stage, enter=True, defer_if_full=True):
                pushed += 1
                # Let the implementation drain establish ownership before a
                # later source item is considered, otherwise two overlapping
                # plans can be queued in the same bootstrap tick.
                if overlap_enabled and item.stage is StageName.IMPLEMENTATION:
                    break
            else:
                source.issues.appendleft(issue)
                break
        if not source.issues:
            self._direct_issue_source = None
        elif pushed == 0 and blocked_by_overlap and scanned == scan_limit:
            source.overlap_blocked_claims = active_claims
            logger.info(
                "direct issue source blocked by active file claims; candidates=%d",
                scan_limit,
            )
        return pushed

    def _drain_direct_pr_source(self) -> int:
        """Pull explicit PRs into queues without an eager seed-entry spill.

        Explicit PRs retain their historical precedence over explicit issues
        when both flags are supplied.  As a source advances only after the
        prior PR leaves the global live-work window, that precedence cannot
        discard later PRs on a saturated queue.
        """
        source = self._direct_pr_source
        if source is None:
            return 0

        pushed = 0
        while self._direct_issue_queues_can_accept():
            if source.pending_pr is not None:
                pr = source.pending_pr
                source.pending_pr = None
            else:
                try:
                    pr = next(source.prs)
                except StopIteration:
                    self._direct_pr_source = None
                    break

            raw_github = self._ctx_for_repo(source.repo).github
            entry = self._seed_direct_pr_scope(source.repo, (pr,), github=raw_github)[0]
            if entry.stage is None:
                logger.info("seed excluded: %s", entry.reason)
                continue

            item = self._prepare_direct_item(entry, source.repo, source.base_sha)
            self._restore_learning_intents(item, entry.stage, entry.reason)
            if source.wave_lease is not None:
                item.payload[WAVE_LEASE_PAYLOAD] = source.wave_lease
            if self._push_item(item, item.stage, enter=True, defer_if_full=True):
                pushed += 1
            else:
                source.pending_pr = pr
                break
        return pushed

    def _clamp_seed_stage_to_scope(
        self,
        issue: int,
        stage: StageName | None,
        reason: str,
        scope_stages: frozenset[StageName] | None,
    ) -> tuple[StageName | None, str]:
        """Compatibility wrapper returning only stage/reason for callers."""
        stage, reason, _passed = self._scope_seed_decision(issue, stage, reason, scope_stages)
        return stage, reason

    def _scope_seed_decision(
        self,
        issue: int,
        stage: StageName | None,
        reason: str,
        scope_stages: frozenset[StageName] | None,
    ) -> tuple[StageName | None, str, bool]:
        """Reconcile a classified entry stage with the run's pipeline scope.

        Full-pipeline runs (``scope_stages is None``) pass the classification
        through unchanged. Under a partial scope (e.g. the planner CLI's
        planning -> plan_review scope) an issue can classify PAST the scope —
        an at-or-past ``state:plan-go`` issue seeds to IMPLEMENTATION, which is
        out of scope. Two reconciliations:

        - ``--force``: re-route any in-pipeline (non-excluded) stage that is not
          already the scope's entry stage back to the scope's FIRST stage so
          the work is redone (for the planner scope, re-plan from PLANNING).
        - default: an issue that classifies past the scope has already
          completed the scoped work, so clamp it to FINISHED (pass) rather than
          push it into an out-of-scope stage the trimmed route table has no row
          for. In-scope classifications (e.g. PLANNING/PLAN_REVIEW) are kept.

        Exclusions (``stage is None``: ``state:skip``) are never
        overridden — force is a re-plan knob, not a skip bypass.

        Args:
            issue: The issue number (for the reason string).
            stage: The classified entry stage (or None when excluded).
            reason: The classification reason.
            scope_stages: The scope's stage set, or None for a full run.

        Returns:
            The reconciled ``(stage, reason, passed)``. ``passed`` is used when
            the stage is clamped directly to ``FINISHED``.

        """
        if stage is None or scope_stages is None:
            return stage, reason, True

        first_in_scope = next((s for s in PIPELINE_ORDER if s in scope_stages), None)
        if self.config.force:
            # Force re-routes an at-or-past-scope stage back to the scope's
            # entry so the scoped work is redone. A PRE-scope stage (earlier in
            # PIPELINE_ORDER than first_in_scope) is left untouched — force is a
            # redo knob for work already in/past the scope, not a fast-forward
            # that pulls un-started upstream work into the scope. (For the
            # planner planning->plan_review scope direct seeding produces no
            # pre-scope items, but a later scope, e.g. implementation->pr_review,
            # has repo/planning/plan_review upstream.)
            if first_in_scope is not None and stage != first_in_scope:
                first_idx = PIPELINE_ORDER.index(first_in_scope)
                if PIPELINE_ORDER.index(stage) >= first_idx:
                    return first_in_scope, f"#{issue} force re-plan ({reason})", True
            return stage, reason, True

        if stage not in scope_stages:
            if first_in_scope is not None and PIPELINE_ORDER.index(stage) < PIPELINE_ORDER.index(
                first_in_scope
            ):
                return (
                    StageName.FINISHED,
                    f"#{issue} not ready for selected scope ({reason})",
                    False,
                )
            # Classified past the scope: the scoped work is already done.
            return StageName.FINISHED, f"#{issue} already past selected scope ({reason})", True
        return stage, reason, True

    def _seed_direct_scope(self, repo: str) -> list[_seeding.SeedEntry]:
        """Classify direct entries for legacy direct-classifier callers.

        Runtime seeding never materializes the explicit issue scope here;
        :meth:`_drain_direct_issue_source` owns its bounded cursor.  This
        compatibility helper remains for direct classifier tests.
        """
        entries: list[_seeding.SeedEntry] = []
        issue_numbers = _admission._filter_open_issues(repo, self.config.issues)
        for issue in issue_numbers:
            entries.append(self._seed_direct_issue_entry(repo, issue))
        entries.extend(self._seed_direct_pr_scope(repo))
        return entries

    def _seed_direct_pr_scope(
        self, repo: str, prs: Iterable[int] | None = None, *, github: StageGitHub | None = None
    ) -> list[_seeding.SeedEntry]:
        """Classify explicit PRs for compatibility callers or a one-PR source pull."""
        github = github or (self._ctx_for_repo(repo).github if repo else self.github)
        entries: list[_seeding.SeedEntry] = []
        scope_stages = self.config.scope.stages if self.config.scope is not None else None
        for pr in self.config.prs if prs is None else prs:
            issue_number = github.find_issue_for_pr(pr)
            if issue_number is None:
                entries.append(
                    _seeding.SeedEntry(
                        kind="pr",
                        identifier=pr,
                        stage=StageName.FINISHED,
                        reason=(
                            f"PR #{pr} has no linked issue; refusing review without "
                            "requirements context"
                        ),
                        pr_number=pr,
                        passed=False,
                    )
                )
                continue
            scope_identifier = issue_number if issue_number is not None else pr
            pr_state = github.gh_pr_state(pr)
            pr_state_name = ((pr_state or {}).get("state") or "").upper()
            if pr_state_name == "MERGED":
                entries.append(
                    _seeding.SeedEntry(
                        kind="pr",
                        identifier=pr,
                        stage=StageName.FINISHED,
                        reason=f"PR #{pr} already merged",
                        pr_number=pr,
                        issue_number=issue_number,
                        passed=True,
                    )
                )
                continue
            if pr_state_name == "CLOSED":
                entries.append(
                    _seeding.SeedEntry(
                        kind="pr",
                        identifier=pr,
                        stage=StageName.FINISHED,
                        reason=f"PR #{pr} already closed without merging",
                        pr_number=pr,
                        issue_number=issue_number,
                        passed=False,
                    )
                )
                continue
            has_go, _has_no_go = github.pr_has_implementation_state_label(pr)
            pending_audit = _seeding.read_pending_implementation_go_audit(github, pr)
            if pending_audit is not None or has_go:
                stage_name = (
                    StageName.PR_REVIEW if pending_audit is not None else StageName.MERGE_WAIT
                )
                reason = (
                    f"PR #{pr} has a pending implementation-go audit"
                    if pending_audit is not None
                    else f"PR #{pr} carries {STATE_IMPLEMENTATION_GO}"
                )
                stage, reason, passed = self._scope_seed_decision(
                    scope_identifier, stage_name, reason, scope_stages
                )
                entries.append(
                    _seeding.SeedEntry(
                        kind="pr",
                        identifier=pr,
                        stage=stage,
                        reason=reason,
                        pr_number=pr,
                        issue_number=issue_number,
                        passed=passed,
                        pending_implementation_go_audit=pending_audit,
                        pending_implementation_go_label_confirmed=has_go,
                    )
                )
            else:
                issue_facts: _seeding.IssueFacts | None
                review_context: dict[str, str] | None
                try:
                    issue_facts = _seeding.seed_issue_from_github(issue_number, github)
                    review_context = github.pr_review_context(pr)
                except Exception as exc:
                    logger.warning("PR #%d: review context read failed: %s", pr, exc)
                    review_context = None
                    issue_facts = None
                if issue_facts is None or review_context is None:
                    entries.append(
                        _seeding.SeedEntry(
                            kind="pr",
                            identifier=pr,
                            stage=StageName.FINISHED,
                            reason=f"PR #{pr} review context could not be read",
                            pr_number=pr,
                            issue_number=issue_number,
                            passed=False,
                        )
                    )
                    continue
                stage, reason, passed = self._scope_seed_decision(
                    scope_identifier,
                    StageName.PR_REVIEW,
                    f"PR #{pr} without {STATE_IMPLEMENTATION_GO} — awaiting review",
                    scope_stages,
                )
                entries.append(
                    _seeding.SeedEntry(
                        kind="pr",
                        identifier=pr,
                        stage=stage,
                        reason=reason,
                        pr_number=pr,
                        issue_number=issue_number,
                        issue_title=issue_facts.title,
                        issue_body=issue_facts.body,
                        pr_description=review_context["pr_description"],
                        passed=passed,
                    )
                )
        return entries

    @staticmethod
    def _entry_to_item(entry: _seeding.SeedEntry, default_repo: str) -> WorkItem:
        """Turn one :class:`~.seeding.SeedEntry` into a queue-ready WorkItem.

        Args:
            entry: The seed entry (never an exclusion — caller filters).
            default_repo: Repo context for ``--issues`` / ``--prs`` entries
                (the legacy loop scopes an explicit issue list to the first
                resolved repo the same way).

        """
        assert entry.stage is not None  # noqa: S101  # caller filters exclusions
        if entry.kind == "repo":
            item = WorkItem(repo=str(entry.identifier), kind=ItemKind.REPO, stage=entry.stage)
        elif entry.kind == "pr":
            item = WorkItem(
                repo=default_repo,
                kind=ItemKind.PR,
                # Only a resolved, linked issue supplies requirements context.
                # A PR number is not a safe substitute: its author controls
                # the PR body, so an unlinked direct PR must remain orphaned
                # and fail closed before PR review.
                issue=entry.issue_number,
                pr=entry.pr_number or int(entry.identifier),
                stage=entry.stage,
            )
            item.payload["issue_title"] = entry.issue_title
            item.payload["issue_body"] = entry.issue_body
            item.payload["pr_description"] = entry.pr_description
            if entry.pending_implementation_go_audit is not None:
                item.payload["pending_implementation_go_audit"] = (
                    entry.pending_implementation_go_audit.audit
                )
                item.payload["pending_implementation_go_audit_head"] = (
                    entry.pending_implementation_go_audit.head_sha
                )
                item.payload["pending_implementation_go_label_confirmed"] = (
                    entry.pending_implementation_go_label_confirmed
                )
        else:
            item = WorkItem(
                repo=default_repo,
                kind=ItemKind.ISSUE,
                issue=int(entry.identifier),
                pr=entry.pr_number,
                stage=entry.stage,
            )
            item.payload["issue_title"] = entry.issue_title
            item.payload["issue_body"] = entry.issue_body
            # A scoped issue with an open PR enters PR_REVIEW as an issue
            # work item. Preserve that provenance so the stage adopts a
            # dedicated PR checkout rather than falling back to the shared
            # repository root.
            if entry.stage is StageName.PR_REVIEW and entry.pr_number is not None:
                item.payload["existing_pr"] = True
            if entry.pending_implementation_go_audit is not None:
                item.payload["pending_implementation_go_audit"] = (
                    entry.pending_implementation_go_audit.audit
                )
                item.payload["pending_implementation_go_audit_head"] = (
                    entry.pending_implementation_go_audit.head_sha
                )
                item.payload["pending_implementation_go_label_confirmed"] = (
                    entry.pending_implementation_go_label_confirmed
                )
        item.state = "ENTER"
        item.payload["entry_reason"] = entry.reason
        return item

    def _has_pending_seed_source(self) -> bool:
        """Return whether this pass still owns an unclassified input cursor.

        A source may have admitted no issue yet, so ``_pass_work_count`` alone
        cannot distinguish a converged pass from one awaiting its first safe
        queue slot.  Sources themselves remain bounded: one direct cursor,
        one repository-entry cursor, and at most C active repo cursors.
        """
        return any(
            (
                self._repo_entry_source is not None,
                bool(self._repo_issue_sources),
                self._direct_issue_source is not None,
                self._direct_pr_source is not None,
                self._direct_scope_bootstrap_pending,
            )
        )

    def _reseed_if_converged(self) -> bool:
        """Re-seed repository discovery after full drain; False = stop.

        Mirrors the legacy zero-work early-exit (loop_runner
        ``_CONVERGENCE_PHASES``): when the just-finished pass produced zero
        actionable (non-repo, non-finished) work, the run converged — exit
        even if ``--loops`` remain.

        Explicit ``--issues`` or ``--prs`` selections are a finite operator
        scope, not a discovery source. Their stage queues own all retries and
        fail-backs, so a completed pass must never recreate their cursors.
        """
        if self.config.issues or self.config.prs:
            logger.info("explicit issue/PR selection drained; skipping discovery re-seed")
            return False
        if self._wave_mode_active:
            logger.info("checkpointed issue wave drained; suppressing --loops reseed")
            return False
        if self._loops_run >= self.config.loops:
            logger.info("loop budget exhausted (%d/%d)", self._loops_run, self.config.loops)
            return False
        if self._pass_work_count == 0 and not self._has_pending_seed_source():
            logger.info(
                "zero-work convergence: pass %d produced no actionable items; exiting early",
                self._loops_run,
            )
            return False
        self._loops_run += 1
        logger.info("re-seeding: loop %d/%d", self._loops_run, self.config.loops)
        self._seed_pass()
        return self._pass_work_count > 0 or self._has_pending_seed_source()
