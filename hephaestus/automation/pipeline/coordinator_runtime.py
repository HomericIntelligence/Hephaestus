import sys
from typing import Any, cast

import hephaestus.automation.pipeline.coordinator_observability as _observability
from hephaestus.diagnostics import bounded_git_diagnostic

from .coordinator_contract import _CoordinatorHost
from .coordinator_handoffs import PendingHandoffCoordinator
from .coordinator_shutdown import shutdown_signal_message
from .coordinator_types import *

# ruff: noqa: F403, F405


def _compat(name: str) -> Any:
    """Resolve mutable coordinator constants from the façade at call time."""
    return getattr(sys.modules["hephaestus.automation.pipeline.coordinator"], name)


class _CompatModule:
    """Proxy a module whose test seams live on the coordinator façade."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, name: str) -> Any:
        return getattr(_compat(self._name), name)


time = cast(Any, _CompatModule("time"))
logger = logging.getLogger("hephaestus.automation.pipeline.coordinator")

_DYNAMIC_METRIC_SERIES_CAP = 100
_PIPELINE_STAGE_LABELS = frozenset(stage.value for stage in StageName)
_JOB_OUTCOME_LABELS = frozenset({"ok", "failed", "interrupted"})
_LANE_LABELS = frozenset({"main", "auxiliary"})
_BREAKER_STATE_LABELS = frozenset({"closed", "open", "half_open"})
_ALERT_NAME_LABELS = frozenset({"circuit_breaker_open", "queue_depth_exceeds", "pipeline_stalled"})


class CoordinatorRuntime(PendingHandoffCoordinator, _CoordinatorHost):
    """Own the event loop, timers, completions, routing, and shutdown."""

    _event_log_disabled: bool
    _grace_deadline: float | None
    _observed_circuit_breaker_states: dict[str, str]
    _observed_inflight_repos: set[str]
    _auxiliary_job_failure_count: int
    _pool_shut_down: bool

    def _default_stages(self) -> dict[StageName, Stage]:
        """Build the full production stage map."""
        return {
            StageName.REPO: RepoStage(),
            StageName.PLANNING: PlanningStage(),
            StageName.PLAN_REVIEW: PlanReviewStage(),
            StageName.IMPLEMENTATION: ImplementationStage(),
            StageName.PR_REVIEW: PrReviewStage(),
            StageName.MERGE_WAIT: MergeWaitStage(),
            StageName.LEARNING: LearningStage(),
            StageName.FINISHED: FinishedStage(
                self.ledger,
                self.preserved,
                self.recovery_preserved,
            ),
        }

    def _ctx_for_repo(self, repo: str) -> StageContext:
        """Return the (cached, per-repo) StageContext for *repo*."""
        ctx = self._ctx_cache.get(repo)
        if ctx is not None:
            self._ctx_cache.move_to_end(repo)
        else:
            root = _effective_repo_root(self.config, repo)
            from hephaestus.automation.arming_state import LearningJournalStore
            from hephaestus.automation.plan_review_session import PlanReviewSessionStore
            from hephaestus.automation.source_worktree import SourceWorkspaceManager

            def learning_state_dir() -> Path:
                return root / "build" / ".automation-state"

            ctx = StageContext(
                config=self.config,
                org=self.config.org,
                dry_run=self.config.dry_run,
                github=(
                    self._github_factory(repo, root)
                    if self._github_factory is not None
                    else self.github
                ),
                paths=_Paths(
                    repo_root=root,
                    worktree=root,
                    projects_dir=Path(self.config.projects_dir),
                    source_workspaces=lambda: (
                        SourceWorkspaceManager(root, repository=repo)
                        if (root / ".git").exists()
                        else None
                    ),
                ),
                now_fn=time.monotonic,
                budget_fn=self._budget_for,
                event_fn=self._record_stage_event,
                learning_journal=LearningJournalStore(
                    learning_state_dir,
                    claim_registry=self._learning_claim_registry,
                ),
                plan_review_sessions=PlanReviewSessionStore(learning_state_dir),
                branch_worktree_owner_status=self._branch_worktree_owner_status,
            )
            if len(self._ctx_cache) >= self._ctx_cache_capacity:
                self._ctx_cache.popitem(last=False)
            self._ctx_cache[repo] = ctx
        return ctx

    def _ctx_for(self, item: WorkItem) -> StageContext:
        """Return the (cached, per-repo) StageContext for *item*."""
        return self._ctx_for_repo(item.repo)

    def _budget_for(self, name: str) -> int:
        """Return a configured budget override or the route default."""
        override = self.config.budget_overrides.get(name)
        if override is not None:
            return override
        return _budget_lookup(name)

    def _record_stage_event(self, event: StageEvent) -> None:
        """Validate and persist a closed-schema event emitted by a stage."""
        event_name, fields = encode_stage_event(event)
        self._record_event(event_name, fields)

    def _record_event(self, event: str, *fields: Any) -> None:
        """Append an event to memory and, when configured, to JSONL on disk."""
        _observability.record_event(self, event, *fields, now_fn=time.time, logger=logger)

    def _observability_snapshot(self) -> dict[str, Any]:
        """Read the coordinator lifecycle values that observability exposes."""
        return _observability.observability_snapshot(self, logger=logger)

    def _health_snapshot(self) -> dict[str, Any]:
        """Return the local server's JSON readiness response without external I/O."""
        return _observability.health_snapshot(
            self,
            logger=logger,
            stalled_ticks_threshold=_compat("_STALL_TICKS_BEFORE_FORCE"),
        )

    def _emit_observability_tick(self) -> None:
        """Update live gauges and durably record alert state transitions."""
        registry = self._metrics_registry
        tracker = self._alert_tracker
        if registry is None or tracker is None:
            return
        snapshot = self._observability_snapshot()
        for stage, depth in snapshot["queue_depths"].items():
            registry.gauge(
                "hephaestus_pipeline_queue_depth",
                "Queued pipeline work items by stage.",
                allowed_labels={"stage": _PIPELINE_STAGE_LABELS},
                series_cap=len(_PIPELINE_STAGE_LABELS),
            ).set(depth, labels={"stage": stage})
        registry.gauge(
            "hephaestus_pipeline_inflight_jobs",
            "Pipeline jobs currently owned by the worker pool.",
            allowed_labels={},
            series_cap=1,
        ).set(snapshot["inflight_jobs"])
        self._emit_lane_gauges(registry, snapshot)
        inflight_by_repo = registry.gauge(
            "hephaestus_pipeline_inflight_per_repo",
            "Main-lane pipeline jobs currently in flight by repository.",
            allowed_labels={"repo": None},
            series_cap=_DYNAMIC_METRIC_SERIES_CAP,
        )
        current_repos: set[str] = set()
        for repo, count in snapshot["inflight_per_repo"].items():
            repo_name = str(repo)
            current_repos.add(repo_name)
            inflight_by_repo.set(count, labels={"repo": repo_name})
        for repo in self._observed_inflight_repos - current_repos:
            inflight_by_repo.set(0, labels={"repo": repo})
        self._observed_inflight_repos = current_repos

        registry.gauge(
            "hephaestus_pipeline_loops_total",
            "Reseed passes run by this coordinator process.",
            allowed_labels={},
            series_cap=1,
        ).set(snapshot["loops_run"])

        registry.gauge(
            "hephaestus_pipeline_stalled_ticks",
            "Consecutive drain ticks without pipeline progress.",
            allowed_labels={},
            series_cap=1,
        ).set(snapshot["stalled_ticks"])

        breaker_states = registry.gauge(
            "hephaestus_circuit_breaker_state",
            "Circuit-breaker lifecycle state (active state has value 1).",
            allowed_labels={"name": None, "state": _BREAKER_STATE_LABELS},
            series_cap=_DYNAMIC_METRIC_SERIES_CAP,
        )
        current_breaker_states: dict[str, str] = {}
        for name, breaker in snapshot["circuit_breakers"].items():
            breaker_name = str(name)
            if not isinstance(breaker, dict):
                logger.warning("ignoring malformed circuit-breaker snapshot for %s", breaker_name)
                continue
            state = breaker.get("state")
            if not isinstance(state, str) or state not in _BREAKER_STATE_LABELS:
                logger.warning(
                    "ignoring circuit-breaker snapshot with invalid state for %s: %r",
                    breaker_name,
                    state,
                )
                continue
            previous_state = self._observed_circuit_breaker_states.get(breaker_name)
            if previous_state is not None and previous_state != state:
                breaker_states.set(0, labels={"name": breaker_name, "state": previous_state})
            breaker_states.set(1, labels={"name": breaker_name, "state": state})
            current_breaker_states[breaker_name] = state
        for name, state in self._observed_circuit_breaker_states.items():
            if name not in current_breaker_states:
                breaker_states.set(0, labels={"name": name, "state": state})
        self._observed_circuit_breaker_states = current_breaker_states

        self._record_event("metrics_snapshot", snapshot)
        for event in tracker.observe(snapshot):
            registry.gauge(
                "hephaestus_pipeline_alert_active",
                "Current active pipeline alert state (1 active, 0 resolved).",
                allowed_labels={"name": _ALERT_NAME_LABELS},
                series_cap=len(_ALERT_NAME_LABELS),
            ).set(int(event.status == "fired"), labels={"name": event.name})
            self._record_event(
                f"alert_{event.status}",
                {
                    "name": event.name,
                    "severity": event.severity,
                    "message": event.message,
                },
            )

    @staticmethod
    def _emit_lane_gauges(registry: Any, snapshot: dict[str, Any]) -> None:
        """Update the two fixed-cardinality auxiliary-lane gauges."""
        queue_gauge = registry.gauge(
            "hephaestus_pipeline_lane_queue_depth",
            "Queued pipeline work items by worker lane.",
            allowed_labels={"lane": _LANE_LABELS},
            series_cap=len(_LANE_LABELS),
        )
        inflight_gauge = registry.gauge(
            "hephaestus_pipeline_lane_inflight_jobs",
            "Pipeline jobs currently in flight by worker lane.",
            allowed_labels={"lane": _LANE_LABELS},
            series_cap=len(_LANE_LABELS),
        )
        for lane in _LANE_LABELS:
            queue_gauge.set(snapshot["lane_queue_depths"][lane], labels={"lane": lane})
            inflight_gauge.set(snapshot["inflight_by_lane"][lane], labels={"lane": lane})

    def _wake_completion_wait(self) -> None:
        """Wake the coordinator without writing a sentinel into its bounded queue."""
        self._completion_wakeup.set()

    def run(self) -> int:
        """Run the pipeline to quiescence (or interrupt) and return the exit code."""
        started = time.monotonic()
        if self._install_signals:
            self._install_signal_handlers()
        try:
            if self._metrics_server is not None:
                self._metrics_server.start()
            self._record_event(
                "run_start",
                {
                    "org": self.config.org,
                    "repos": self.config.repos,
                    "repo_source": "streamed" if self.config.repo_source_factory else "explicit",
                    "issues": self.config.issues,
                    "prs": self.config.prs,
                    "loops": self.config.loops,
                    "max_workers": self.config.max_workers,
                },
            )
            self._loops_run = 1
            self._seed_pass()
            while True:
                if self._immediate or self._grace_exceeded():
                    self._teardown_immediate()
                    break
                self._wake_timers()
                self._drain_completions()
                self._emit_observability_tick()
                if self.shutdown.is_set():
                    # Graceful: stop admitting; drain in-flight to RESUMABLE.
                    if not self.in_flight and not self.auxiliary_in_flight:
                        break
                    self._wait_for_completion(timeout=0.2)
                    continue
                self._drain_queues()
                self._drain_repo_entry_source()
                self._drain_repo_issue_sources()
                self._drain_direct_pr_source()
                self._drain_direct_issue_source()
                if self._all_idle():
                    if not self._reseed_if_converged():
                        break
                    continue
                self._idle_wait()
        except Exception:
            logger.exception("pipeline run failed")
            self._fatal = True
        finally:
            # Reap the pool on EVERY exit path — a fatal exception never sets
            # self.shutdown, so without this the executor and in-flight AgentJob
            # subprocesses (e.g. claude reviewers) would leak (#2059). Idempotent
            # via _pool_shut_down, so the signal path's earlier call is a no-op.
            self._shutdown_pool()
            self._finalize_resumable()
            exit_code = self._exit_code()
            stats = RunStats(
                exit_code=exit_code,
                loops_run=self._loops_run,
                agent_job_count=self._agent_job_count,
                agent_job_time_s=self._agent_job_time_s,
                wall_s=time.monotonic() - started,
                auxiliary_job_count=self._auxiliary_job_count,
                auxiliary_job_time_s=self._auxiliary_job_time_s,
                auxiliary_job_failure_count=self._auxiliary_job_failure_count,
            )
            summary_items = self._effective_items()
            preserved = self._active_preserved_worktrees()
            recovery_preserved = self._active_recovery_worktrees()
            try:
                self._record_event(
                    "run_end",
                    {
                        "exit_code": exit_code,
                        "interrupted": stats.interrupted,
                        "items": len(summary_items),
                        "agent_jobs": self._agent_job_count,
                        "wall_s": stats.wall_s,
                    },
                )
                print_summary(
                    summary_items,
                    stats,
                    preserved,
                    json_out=self.config.json_out,
                    recovery_preserved=recovery_preserved,
                    terminal_summary=(
                        self._terminal_summary if self._terminal_summary.total else None
                    ),
                )
            finally:
                if self._metrics_server is not None:
                    self._metrics_server.stop()
        return exit_code

    def _effective_items(self) -> list[WorkItem]:
        """Return latest logical items, collapsing superseded re-seed attempts."""
        return latest_logical_items(self.items)

    def _active_preserved_worktrees(self) -> list[PreservedWorktree]:
        """Return extant failed-item worktrees for the latest logical items."""
        failed_items = {
            (item.repo, item.issue or item.pr or 0)
            for item in self._effective_items()
            if item.result is not None and not item.result.passed
        }
        active: list[PreservedWorktree] = []
        seen: set[PreservedWorktree] = set()
        for repo, issue_or_pr, path in self.preserved:
            entry = (repo, issue_or_pr, path)
            if entry in seen or (repo, issue_or_pr) not in failed_items or not Path(path).exists():
                continue
            seen.add(entry)
            active.append(entry)
        return active

    def _active_recovery_worktrees(self) -> list[PreservedWorktree]:
        """Return extant receipt-backed detached-review recovery worktrees."""
        active: list[PreservedWorktree] = []
        seen: set[PreservedWorktree] = set()
        for repo, issue_or_pr, path in self.recovery_preserved:
            entry = (repo, issue_or_pr, path)
            if entry in seen or not Path(path).exists():
                continue
            seen.add(entry)
            active.append(entry)
        return active

    def _exit_code(self) -> int:
        """130 on interrupt; 1 on any effective fail/skip/blocked; 0 clean."""
        if self.shutdown.is_set():
            # Interrupt deliberately takes priority over non-passing ledger
            # entries and fatal coordinator errors: a signal means the run did
            # not complete, so wrappers must classify it as cancellation even
            # if earlier work had already failed.
            return 130
        if self._fatal:
            return 1
        if self._terminal_summary.total:
            passing = self._terminal_summary.dispositions.get("pass", 0)
            return 1 if passing < self._terminal_summary.total else 0
        effective_results = [item.result for item in self._effective_items() if item.result]
        results = effective_results or self.ledger
        if any(not result.passed for result in results):
            return 1
        return 0

    def _all_idle(self) -> bool:
        """Return True when no ready, leased, timed, or in-flight work remains."""
        return (
            all(len(q) == 0 for q in self.queues.values())
            and not self.timers
            and not self.in_flight
            and not self.auxiliary_in_flight
            and not self._leases
            and not self._pending_handoffs
            and self._direct_issue_source is None
            and self._direct_pr_source is None
            and self._repo_entry_source is None
            and not self._repo_issue_sources
        )

    def _record_terminal_result(self, item: WorkItem) -> None:
        """Aggregate one completed/resumable item and trim detailed retention.

        The local result collections are an operator convenience, not recovery
        state.  Keep only the configured newest completed details while a
        constant-space aggregate preserves the full run's pass/fail/total
        reporting and exit status.
        """
        if item.result is None or item.payload.get("_summary_recorded", False):
            return
        item.payload["_summary_recorded"] = True
        self._terminal_summary.record(item)
        self._seen_item_ids.discard(id(item))

        # Move a completed item to the tail so the bounded window is ordered
        # by terminal completion rather than by its initial seed time.  An
        # item can be absent in narrow direct unit tests; runtime items are
        # always tracked by _push_item first.
        for index, candidate in enumerate(self.items):
            if candidate is item:
                self.items.pop(index)
                self.items.append(item)
                break

        retained = self.config.terminal_detail_capacity
        completed = [
            index
            for index, candidate in enumerate(self.items)
            if candidate.payload.get("_summary_recorded", False)
        ]
        for index in reversed(completed[:-retained]):
            self.items.pop(index)

        # The sink may record a result before a cleanup job completes.  At
        # most C such items can coexist, so these lists are bounded by N + C
        # between records and by N after every terminal completion.
        if len(self.ledger) > retained:
            del self.ledger[:-retained]
        if len(self.preserved) > retained:
            del self.preserved[:-retained]
        if len(self.recovery_preserved) > retained:
            del self.recovery_preserved[:-retained]

    def _claim_item(self, stage_name: StageName, *, index: int = 0) -> WorkItem | None:
        """Claim one ready item while retaining its source-stage capacity."""
        lease = self.queues[stage_name].claim_at(index)
        if lease is None:
            return None
        item = cast(WorkItem, lease.item)
        item_id = id(item)
        if item_id in self._leases:  # pragma: no cover - internal invariant
            lease.restore()
            raise RuntimeError(f"duplicate active lease for {self._item_key(item)}")
        self._leases[item_id] = lease
        return item

    def _restore_source_lease(self, item: WorkItem) -> bool:
        """Return an active item lease to its source queue without a transition."""
        self._pending_handoffs.pop(id(item), None)
        lease = self._leases.pop(id(item), None)
        if lease is None:
            return False
        lease.restore()
        return True

    def _release_source_lease(self, item: WorkItem) -> bool:
        """Release an active lease when work leaves every stage queue.

        Timers and terminal sink completion are neither source restores nor
        destination-first handoffs.  The external owner takes responsibility
        for the item, so release its source slot directly without disturbing a
        different queue head selected ahead of it by topo scheduling.
        """
        self._pending_handoffs.pop(id(item), None)
        lease = self._leases.pop(id(item), None)
        if lease is None:
            return False
        lease.release()
        return True

    def _activate_handoff(
        self,
        item: WorkItem,
        target: StageName,
        *,
        enter: bool,
        result: ItemResult | None,
    ) -> None:
        """Publish a route only after its destination accepted the item."""
        source = item.stage
        self._clear_implementation_file_claims_on_exit(item, target)
        source_auxiliary = self._is_auxiliary_stage(source)
        target_auxiliary = self._is_auxiliary_stage(target)
        if not source_auxiliary and target_auxiliary:
            self._live_work_permit_ids.discard(id(item))
            self._learning_work_permit_ids.add(id(item))
        elif source_auxiliary and not target_auxiliary:
            self._learning_work_permit_ids.discard(id(item))
            self._live_work_permit_ids.add(id(item))
        item.stage = target
        if enter:
            item.state = "ENTER"
            item.payload["_enter_pending"] = True
            item.add_history_event(target, item.state, note="enqueued")
        if result is not None:
            item.result = result
        self._record_event("push", target.value, self._item_key(item))

    def _handoff_item(
        self,
        item: WorkItem,
        target: StageName,
        *,
        enter: bool,
        result: ItemResult | None = None,
    ) -> bool:
        """Route an item destination-first, retaining a full-target intent.

        Direct unit-test calls do not own a source lease and retain the
        historical push behavior.  Normal drains always carry a lease, so a
        full destination only records a bounded intent and the completed
        stage action is never replayed.
        """
        lease = self._leases.get(id(item))
        if lease is None:
            if result is not None:
                item.result = result
            source = item.stage
            self._push_item(item, target, enter=enter)
            source_auxiliary = self._is_auxiliary_stage(source)
            target_auxiliary = self._is_auxiliary_stage(target)
            if not source_auxiliary and target_auxiliary:
                self._live_work_permit_ids.discard(id(item))
                self._learning_work_permit_ids.add(id(item))
            elif source_auxiliary and not target_auxiliary:
                self._learning_work_permit_ids.discard(id(item))
                self._live_work_permit_ids.add(id(item))
            return True

        if target is item.stage:
            # RETRY to the source is a restore, not a self-handoff: a held
            # lease deliberately blocks its source's ``offer`` method.
            if result is not None:  # pragma: no cover - terminal target is FINISHED
                raise RuntimeError("terminal result cannot route to the source stage")
            return self._restore_source_lease(item)

        if not self._lane_handoff_capacity(item, target):
            accepted = False
        else:
            accepted = lease.handoff(self.queues[target])
        if accepted:
            self._leases.pop(id(item), None)
            self._pending_handoffs.pop(id(item), None)
            self._activate_handoff(item, target, enter=enter, result=result)
            self._progress = True
            return True

        pending = _PendingHandoff(item=item, target=target, enter=enter, result=result)
        existing = self._pending_handoffs.setdefault(id(item), pending)
        if existing != pending:  # pragma: no cover - no item can route twice while leased
            raise RuntimeError(f"conflicting pending handoff for {self._item_key(item)}")
        self._record_event(
            "handoff_pending",
            item.stage.value,
            target.value,
            self._item_key(item),
        )
        return False

    def _drain_pending_handoffs(self) -> None:
        """Retry every retained route whose destination may have opened."""
        self._drain_complementary_handoff_pairs()
        for item_id, pending in list(self._pending_handoffs.items()):
            lease = self._leases.get(item_id)
            item = pending.item
            if not self._lane_handoff_capacity(item, pending.target):
                continue
            accepted = (
                lease.handoff(self.queues[pending.target])
                if lease is not None
                else self.queues[pending.target].offer(item)
            )
            if not accepted:
                continue
            if lease is not None:
                self._leases.pop(item_id, None)
            self._pending_handoffs.pop(item_id, None)
            self._activate_handoff(
                item,
                pending.target,
                enter=pending.enter,
                result=pending.result,
            )
            self._record_event(
                "handoff_retry",
                item.stage.value,
                self._item_key(item),
            )
            self._progress = True

    def _grace_exceeded(self) -> bool:
        """Return True when a graceful shutdown has outlived its grace window."""
        return (
            self.shutdown.is_set()
            and self._grace_deadline is not None
            and time.monotonic() >= self._grace_deadline
        )

    def _idle_wait(self) -> None:
        """Block on the completion queue (the loop's only sleep).

        Also breaks a theoretical no-progress stall: if a full tick made no
        progress with nothing in flight and no timers pending, force-run the
        most-downstream queued item ignoring admission (liveness guarantee —
        admission can only defer while something else is running or parked).
        """
        if self._progress:
            self._progress = False
            self._stalled_ticks = 0
        elif not self.in_flight and not self.auxiliary_in_flight and not self.timers:
            self._stalled_ticks += 1
            if self._stalled_ticks >= _compat("_STALL_TICKS_BEFORE_FORCE"):
                self._force_run_one()
                return
        timeout = _compat("_IDLE_POLL_S")
        if self.timers:
            timeout = min(timeout, max(0.01, self.timers[0][0] - time.monotonic()))
        self._wait_for_completion(timeout=timeout)

    def _force_run_one(self) -> None:
        """Run the first item of the most-downstream non-empty queue."""
        assert not self.in_flight and not self.auxiliary_in_flight, (  # noqa: S101
            "force-run requires no in-flight work"
        )
        self._stalled_ticks = 0
        for stage_name in _DRAIN_ORDER:
            q = self.queues[stage_name]
            if len(q):
                item = self._claim_item(stage_name)
                if item is None:  # pragma: no cover - len/claim are coordinator-thread atomic
                    continue
                logger.error(
                    "pipeline stalled with no in-flight work; "
                    "force-running %s item %s; inflight_per_repo=%s",
                    stage_name.value,
                    self._item_key(item),
                    dict(self.inflight_per_repo),
                )
                self._run_item(item)
                return

    def _timer_park(self, item: WorkItem, delay_s: float) -> None:
        """Park *item* on the timer heap for ``delay_s`` seconds."""
        # A timer is outside every stage queue.  Release a normal drain's
        # source lease before parking so its capacity is not stranded while
        # preserving the timer's single owner for the work item.
        self._release_source_lease(item)
        wake = time.monotonic() + max(0.0, delay_s)
        heapq.heappush(self.timers, (wake, self._seq, item))
        self._seq += 1
        item.add_history_event(item.stage, item.state, note=f"timer-parked {delay_s:.1f}s")
        self._record_event("timer_park", item.stage.value, self._item_key(item), delay_s)

    def _wake_timers(self) -> None:
        """Move expired timer entries back only after their stage accepts them.

        The timer heap owns an item until its stage queue accepts it.  An
        expired entry whose stage is at capacity remains at the heap head for a
        later tick; it is ordinary bounded backpressure, not a pipeline fault.
        """
        now = time.monotonic()
        while self.timers and self.timers[0][0] <= now:
            _, _, item = self.timers[0]
            if not self._push_item(item, item.stage, enter=False, defer_if_full=True):
                return
            heapq.heappop(self.timers)
            self._progress = True

    def _park_resumable(self, item: WorkItem) -> None:
        """Park *item* as RESUMABLE at its current stage (interrupt semantics).

        Never FAILED: durable writes precede queue pushes, so a restart's
        seeding reconstruction resumes exactly here with no shutdown
        bookkeeping.
        """
        self._record_resumable_recovery_worktrees(item)
        self._release_source_lease(item)
        item.result = ItemResult(
            passed=False,
            reason=f"resumable at {item.stage.value}",
            final_stage=item.stage,
        )
        self._record_terminal_result(item)
        item.add_history_event(item.stage, item.state, note="interrupted; resumable")
        self._record_event("resumable", self._item_key(item), item.stage.value, item.state)
        logger.info(
            "interrupt: item %s RESUMABLE at %s (never failed)",
            self._item_key(item),
            item.stage.value,
        )

    def _record_resumable_recovery_worktrees(self, item: WorkItem) -> None:
        """Retain receipt-backed recovery paths when shutdown skips FinishedStage.

        A worker writes a remote-drift receipt before returning the result that
        triggers a fresh review.  A shutdown can park that item before it ever
        reaches ``FinishedStage``, so collect the same durable evidence here
        for the interrupt summary rather than losing the operator guidance.
        """
        direct_publication_interrupted = (
            item.worktree
            and item.payload.get("direct_pr_worktree") == item.worktree
            and item.state in {"ADDRESS_WAIT", "PUSH_WAIT"}
        )
        if direct_publication_interrupted or (
            item.worktree
            and is_inspection_only_detached_push_failure(item.payload.get("detached_push_failure"))
        ):
            entry = (item.repo, item.issue or item.pr or 0, item.worktree)
            if entry not in self.recovery_preserved:
                self.recovery_preserved.append(entry)
        if item.issue is None or item.pr is None:
            return
        try:
            worktrees = list_direct_review_recovery_paths(
                repo_root=_effective_repo_root(self.config, item.repo),
                issue=item.issue,
                pr=item.pr,
            )
        except (OSError, RuntimeError) as error:
            logger.warning(
                "interrupt:%s: could not read detached-review recovery receipts: %s",
                item.issue or item.repo,
                error,
            )
            return
        for worktree in worktrees:
            entry = (item.repo, item.issue, str(worktree))
            if entry not in self.recovery_preserved:
                self.recovery_preserved.append(entry)

    @staticmethod
    def _job_result_event_fields(result: JobResult) -> dict[str, Any]:
        """Return bounded, output-free job result fields for durable event logs."""
        fields: dict[str, Any] = {
            "ok": result.ok,
            "interrupted": result.interrupted,
            "error": CoordinatorRuntime._job_result_error_class(result),
            "duration_s": round(result.duration_s, 3),
        }
        if result.worker_id:
            fields["worker_id"] = result.worker_id
        value = result.value
        if (
            isinstance(value, dict)
            and value.get("failure_kind") in {"signing", "continuation"}
            and value.get("phase") in {"stage_conflicts", "validate_index", "rebase_continue"}
        ):
            fields["rebase_failure_diagnostic"] = {
                "failure_kind": value["failure_kind"],
                "phase": value["phase"],
                "returncode": value.get("returncode"),
                "receipt_error": bounded_git_diagnostic(value.get("receipt_error"), limit=500),
                "stdout_tail": bounded_git_diagnostic(result.stdout_tail, limit=4000),
                "stderr_tail": bounded_git_diagnostic(result.stderr_tail, limit=4000),
            }
        return fields

    @staticmethod
    def _job_result_error_class(result: JobResult) -> str | None:
        """Classify job failures without persisting raw error text."""
        if result.error is None:
            return None
        if result.interrupted:
            return "interrupted"
        if result.error.startswith("worker_crash:"):
            return "worker_crash"
        return "error"

    def _drain_queues(self) -> None:
        """Drain queues downstream-first with admission control."""
        # A transition that previously found a full target owns its source
        # lease.  Retry it before ordinary draining, then again after each
        # stage: draining a target can open exactly the slot it needs.
        self._drain_pending_handoffs()
        for stage_name in _DRAIN_ORDER:
            if self.shutdown.is_set():
                return
            if stage_name is StageName.IMPLEMENTATION:
                self._drain_implementation()
                self._drain_pending_handoffs()
                continue
            q = self.queues[stage_name]
            for _ in range(len(q)):
                if self.shutdown.is_set():
                    return
                item = self._claim_item(stage_name)
                if item is None:  # pragma: no cover - len/claim are coordinator-thread atomic
                    break
                if not self._admit(item):
                    self._restore_source_lease(item)
                    continue
                self._record_event("drain", stage_name.value, self._item_key(item))
                self._run_item(item)
            self._drain_pending_handoffs()

    def _run_item(self, item: WorkItem) -> None:
        """Drive one item: on_enter, then step until JobRequest or outcome.

        Per-item try/except — a poisoned item routes to finished(fail) and
        never kills the loop.
        """
        self._progress = True
        stage = self.stages[item.stage]
        ctx = self._ctx_for(item)
        try:
            if item.payload.pop("_enter_pending", False):
                outcome = stage.on_enter(item, ctx)
                if outcome is not None:
                    self._route(item, outcome)
                    return
            for _ in range(_MAX_STEPS_PER_TICK):
                result = self._step_with_watchdog(stage, item, ctx)
                if isinstance(result, Continue):
                    item.state = result.next_state
                    item.add_history_event(item.stage, item.state)
                    if (
                        item.kind is ItemKind.REPO
                        and item.state == "SOURCE"
                        and isinstance(item.payload.get("_repo_issue_source"), RepoIssueSource)
                    ):
                        source = item.payload["_repo_issue_source"]
                        # Setup work has completed. Move only its bounded
                        # cursor into the fair registry, releasing the REPO
                        # lease and provisional permit so one repository
                        # cannot monopolize the stage while it streams.
                        if not self._externalize_repo_issue_source(item, source):
                            # Admission reserves a registry slot before a
                            # setup item enters REPO. This fallback preserves
                            # liveness for an injected/custom queue that
                            # violates that invariant: a bounded timer owns
                            # the item until a cursor slot becomes free.
                            logger.debug("repo:%s: waiting for a source-registry slot", item.repo)
                            self._timer_park(item, _SOURCE_REGISTRY_RETRY_DELAY_S)
                        return
                    continue
                if isinstance(result, JobRequest):
                    if self.config.dry_run:
                        descr = getattr(result.job, "descr", "") or type(result.job).__name__
                        logger.info(
                            "[dry-run] would submit %s: %s", type(result.job).__name__, descr
                        )
                        self._route(
                            item, StageOutcome(Disposition.ADVANCE, f"[dry-run] would {descr}")
                        )
                        return
                    self._submit(item, result)
                    return
                self._route(item, result)
                return
            raise RuntimeError(
                f"stage {item.stage.value} exceeded {_MAX_STEPS_PER_TICK} steps in one tick"
            )
        except Exception as exc:
            logger.exception(
                "pipeline item %s poisoned at %s; routing to finished(fail)",
                self._item_key(item),
                item.stage.value,
            )
            self._finish(item, passed=False, reason=f"poisoned: {exc}")

    def _step_with_watchdog(
        self, stage: Stage, item: WorkItem, ctx: StageContext
    ) -> StageStepResult:
        """Run one stage.step, warning when it breaches the <~60s contract."""
        t0 = time.monotonic()
        result = stage.step(item, ctx)
        elapsed = time.monotonic() - t0
        if elapsed > _compat("_STEP_WATCHDOG_S"):
            logger.warning(
                "stage.step stalled: %s %s took %.1fs (contract: <%.0fs)",
                item.stage.value,
                self._item_key(item),
                elapsed,
                _compat("_STEP_WATCHDOG_S"),
            )
        return result

    def _route(  # noqa: C901 - disposition table includes an auxiliary detour
        self, item: WorkItem, outcome: StageOutcome
    ) -> None:
        """Apply the Disposition -> action table (plan #1817)."""
        if item.stage is StageName.REPO and item.payload.get(DIRECT_SCOPE_BOOTSTRAP_KEY, False):
            self._route_direct_scope_bootstrap(item, outcome)
            return
        route = self._routes.get(item.stage)
        if route is None:
            # A stage absent from this run's (possibly scope-trimmed) route
            # table has no next/fail mapping — routing it would KeyError. This
            # happens when a seeder-created REPO item is poisoned under a
            # partial ``--phases`` scope whose ``trimmed_routes`` omits REPO
            # (#2294). Fail closed to the sink instead of crashing the whole
            # run, which the poison handler that called us already intends.
            logger.error(
                "coordinator: %s has no route in this run's stage scope; "
                "finishing failed instead of crashing (%s)",
                item.stage.value,
                outcome.note or "unroutable",
            )
            reason = outcome.note or f"unroutable:{item.stage.value}"
            self._finish(item, passed=False, reason=reason)
            return
        disposition = outcome.disposition

        if item.stage is StageName.FINISHED:
            # Sink outcomes are terminal: the result is already recorded.
            self._record_event("done", self._item_key(item), outcome.note)
            self._record_terminal_result(item)
            self._release_source_lease(item)
            self._release_work_permit(item)
            return

        if disposition is Disposition.ADVANCE:
            self._seed_products(item)
            if item.stage is StageName.PLAN_REVIEW and item.learning_intents:
                # Preserve the scope-trimmed primary destination before the
                # auxiliary detour. A planner-only run must return to its sink
                # instead of escaping into implementation.
                item.learning_resume_stage = route.next
                self._persist_learning_intents(item)
                target = StageName.LEARNING
            else:
                target = route.next
            if target is StageName.FINISHED:
                reason = str(item.payload.pop("_learning_primary_reason", ""))
                if item.post_processing is not None:
                    self._handoff_item(
                        item,
                        StageName.FINISHED,
                        enter=True,
                        result=item.post_processing.result,
                    )
                else:
                    self._finish(item, passed=True, reason=reason or outcome.note or "advance")
            else:
                self._handoff_item(item, target, enter=True)
            return

        if disposition is Disposition.RETRY:
            self._route_retry(item, outcome)
            return

        if disposition is Disposition.FAIL_BACK:
            self._route_fail_back(item, outcome, route)
            return

        if disposition is Disposition.SKIP:
            self._finish(item, passed=False, reason=f"skip: {outcome.note}")
            return
        if disposition is Disposition.EJECT:
            item.result = ItemResult(
                passed=True,
                reason=f"ejected: {outcome.note}",
                final_stage=item.stage,
            )
            self._record_terminal_result(item)
            self._release_source_lease(item)
            self._release_work_permit(item)
            return
        if disposition is Disposition.BLOCKED:
            self._finish(item, passed=False, reason=f"blocked: {outcome.note}")
            return
        if disposition is Disposition.FINISH_PASS:
            self._seed_products(item)
            if item.stage is StageName.MERGE_WAIT and item.learning_intents:
                primary_reason = outcome.note or "merged"
                item.payload["_learning_primary_reason"] = primary_reason
                terminal_result = ItemResult(
                    passed=True,
                    reason=primary_reason,
                    final_stage=StageName.MERGE_WAIT,
                )
                try:
                    item.compact_for_post_processing(terminal_result)
                    self._persist_learning_intents(item)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    logger.warning(
                        "merge_wait:%s: could not persist ancillary learning: %s",
                        item.issue or item.repo,
                        exc,
                    )
                    item.payload.setdefault("learning_failures", []).append(
                        {"key": "post_merge", "error": str(exc)[:1000]}
                    )
                    item.learning_intents.clear()
                    self._handoff_item(
                        item,
                        StageName.FINISHED,
                        enter=True,
                        result=terminal_result,
                    )
                    return
                self._handoff_item(
                    item,
                    StageName.LEARNING,
                    enter=True,
                    result=terminal_result,
                )
                return
            self._finish(item, passed=True, reason=outcome.note or "pass")
            return
        # FINISH_FAIL (exhaustive over Disposition)
        self._finish(item, passed=False, reason=outcome.note or "fail")

    def _route_direct_scope_bootstrap(self, item: WorkItem, outcome: StageOutcome) -> None:
        """Activate direct cursors only after their checkout proof succeeds.

        A partial pipeline scope deliberately omits ``REPO`` from its route
        table.  This internal setup item therefore has its own tiny terminal
        protocol instead of widening the operator-selected stage scope.  It
        never reaches an agent-capable stage unless the repo stage completed
        clone-plus-sync and then prepared the label vocabulary.
        """
        if outcome.disposition is Disposition.RETRY:
            self._route_retry(item, outcome)
            return
        if outcome.disposition is not Disposition.FINISH_PASS:
            self._direct_scope_bootstrap_pending = False
            self._finish(
                item,
                passed=False,
                reason=outcome.note or "direct scope checkout preparation failed",
            )
            return

        base_sha = item.payload.get(DIRECT_SCOPE_BASE_SHA_KEY)
        if not is_full_commit_sha(base_sha):
            if not self.config.dry_run:
                self._direct_scope_bootstrap_pending = False
                self._finish(
                    item,
                    passed=False,
                    reason="direct scope checkout returned an invalid default-branch SHA",
                )
                return
            # Dry-run never submits checkout or implementation jobs, so it
            # cannot truthfully obtain an immutable checkout SHA. Do not
            # attach a fake pin: direct items retain normal preview behavior
            # while real runs still fail closed without the proof.
            base_sha = ""
        try:
            repo_root = Path(str(self._ctx_for_repo(item.repo).paths.repo_root))
            if not repo_root.is_dir():
                # Test-only/fake worker paths do not represent a reusable
                # checkout. Preserve legacy explicit recovery semantics there;
                # production sync always materializes the repository first.
                self._direct_wave_lease = None
                self._ctx_for_repo(item.repo).github.ensure_state_labels()
                self._begin_direct_pr_source(item.repo, base_sha)
                self._begin_direct_issue_source(item.repo, base_sha)
                raise StopIteration
            store = IssueWaveStore(repo_root, self.config.org, item.repo)
            checkpoint = store.load()
            if checkpoint is not None and checkpoint.status == "active":
                linked_issues = set(self.config.issues)
                for pr_number in self.config.prs:
                    issue_number = self._ctx_for_repo(item.repo).github.find_issue_for_pr(pr_number)
                    if issue_number is None:
                        raise IssueWaveError(f"PR #{pr_number} has no linked wave issue")
                    linked_issues.add(issue_number)
                self._direct_wave_lease = store.bind_recovery_scope(
                    issue_numbers=linked_issues,
                    current_main_sha=base_sha,
                )
            else:
                self._direct_wave_lease = None
            if self._direct_wave_lease is not None:
                self._wave_mode_active = True
            # This is intentionally after recovery membership validation: an
            # invalid direct scope must not create or mutate state labels.
            self._ctx_for_repo(item.repo).github.ensure_state_labels()
            self._begin_direct_pr_source(item.repo, base_sha)
            self._begin_direct_issue_source(item.repo, base_sha)
        except StopIteration:
            pass
        except Exception as exc:
            self._direct_scope_bootstrap_pending = False
            logger.warning(
                "direct scope for %s could not initialize after sync: %s", item.repo, exc
            )
            self._finish(item, passed=False, reason=f"direct scope initialization failed: {exc}")
            return

        # A successful bootstrap is coordinator plumbing, not an issue/repo
        # result.  Free its sole bounded slot before admitting the first
        # direct entry, mirroring successful REPO-source externalization.
        self._release_source_lease(item)
        self._release_work_permit(item)
        self._direct_scope_bootstrap_pending = False
        self._seen_item_ids.discard(id(item))
        with suppress(ValueError):
            self.items.remove(item)
        self._record_event("direct_scope_ready", item.repo)
        self._drain_direct_pr_source()
        self._drain_direct_issue_source()

    def _route_retry(self, item: WorkItem, outcome: StageOutcome) -> None:
        """Apply the RETRY row: heap-park on a recorded delay, else next tick.

        RETRY timer contract (base.py): the stage records its backoff in
        ``payload["retry_delay_s"]`` immediately before returning; a missing
        key means "retry on the next drain tick". Under dry-run a DELAYED
        retry waits on real-world progress (for example, PR merges) the preview
        will never make, so the item finishes instead of stalling the heap.
        """
        delay = item.payload.pop("retry_delay_s", None)
        if delay is None:
            if not self._restore_source_lease(item):
                self._push_item(item, item.stage, enter=False)
        elif self.config.dry_run:
            self._finish(
                item, passed=False, reason=f"[dry-run] would wait {delay}s: {outcome.note}"
            )
        else:
            self._timer_park(item, float(delay))

    def _register_pipeline_writer_worktree(
        self,
        item: WorkItem,
        job: object,
        result: JobResult,
    ) -> None:
        """Register a successful implementation writer worktree.

        The registry is coordinator-owned evidence: a future collision may
        supersede only when its holder path exactly matches this live,
        successful pipeline allocation.  It deliberately does not infer
        ownership from a conventional path such as ``issue-123``.
        """
        materialized = (
            isinstance(result.value, dict) and result.value.get(WORKTREE_MATERIALIZED_KEY) is True
        )
        if (
            not isinstance(job, GitJob)
            or job.op != "create_worktree"
            or (not result.ok and not materialized)
        ):
            return
        if item.stage is not StageName.IMPLEMENTATION or not item.branch or not item.worktree:
            return
        expected_branch = job.kwargs.get("branch_name")
        if expected_branch != item.branch or bool(job.kwargs.get("isolated", False)):
            return
        key = (item.repo, item.branch)
        existing = self._pipeline_writer_worktrees.get(key)
        if existing is not None and existing is not item and existing.result is None:
            logger.error(
                "coordinator: refusing to replace live writer registration for %s at %s",
                item.repo,
                item.branch,
            )
            return
        self._pipeline_writer_worktrees[key] = item

    def _branch_worktree_owner_status(
        self, item: WorkItem, branch: str, owner_path: str
    ) -> BranchWorktreeOwnerStatus:
        """Classify a branch holder as verified, pending, or unverified.

        A Git holder result by itself is not evidence of pipeline ownership.
        It becomes eligible for the documented first-writer-wins policy only
        when this run recorded the exact path after a successful writer
        worktree job and that sibling is still live. A matching in-flight
        create-worktree job may have obtained the holder before its completion
        is dequeued, so it is retried without consuming a work budget. All
        absent, stale, external, malformed, or cross-repository holders route
        to a terminal fail instead of silently dropping work.
        """
        if not branch or branch != item.branch or not owner_path:
            return "unverified"
        owner = self._pipeline_writer_worktrees.get((item.repo, branch))
        if (
            owner is not None
            and owner is not item
            and owner.result is None
            and owner.stage is not StageName.FINISHED
            and owner.branch == branch
            and owner.worktree
            and Path(owner.worktree).resolve() == Path(owner_path).resolve()
        ):
            return "verified"
        for handle, candidate in self.in_flight.items():
            job = handle.job
            if (
                candidate is not item
                and candidate.repo == item.repo
                and candidate.branch == branch
                and candidate.stage is StageName.IMPLEMENTATION
                and isinstance(job, GitJob)
                and job.op == "create_worktree"
                and job.kwargs.get("branch_name") == branch
                and not bool(job.kwargs.get("isolated", False))
            ):
                return "pending"
        return "unverified"

    def _route_fail_back(self, item: WorkItem, outcome: StageOutcome, route: Route) -> None:
        """Apply the FAIL_BACK row: reason-keyed regression, globally capped.

        Dry-run mutators never write the gate labels the earlier stage would
        re-check, so a dry-run regression would ping-pong until the safety
        cap while burning live reads — the item finishes with the
        would-regress note instead (its entry classification is the dry-run
        deliverable).
        """
        if self.config.dry_run:
            self._finish(item, passed=False, reason=f"[dry-run] would fail_back: {outcome.note}")
            return
        fail_backs = int(item.payload.get("_fail_backs", 0)) + 1
        item.payload["_fail_backs"] = fail_backs
        if fail_backs > _FAIL_BACK_CAP:
            self._finish(
                item,
                passed=False,
                reason=f"fail_back safety cap ({_FAIL_BACK_CAP}) exceeded: {outcome.note}",
            )
            return
        target = route.fail_routes.get(outcome.note, route.fail_routes.get("*", StageName.FINISHED))
        if target is StageName.FINISHED:
            self._finish(item, passed=False, reason=outcome.note or "fail_back")
        else:
            self._handoff_item(item, target, enter=True)

    def _finish(self, item: WorkItem, *, passed: bool, reason: str) -> None:
        """Set the item's result and hand it to the finished sink."""
        writer_key = (item.repo, item.branch)
        if self._pipeline_writer_worktrees.get(writer_key) is item:
            self._pipeline_writer_worktrees.pop(writer_key, None)
        if item.stage is StageName.REPO and item.payload.get(DIRECT_SCOPE_BOOTSTRAP_KEY, False):
            self._direct_scope_bootstrap_pending = False
        if item.stage is StageName.FINISHED:
            # Poisoned inside the sink: record directly, never re-queue.
            item.result = ItemResult(passed=passed, reason=reason, final_stage=item.stage)
            if not item.payload.get("_recorded", False):
                self.ledger.append(item.result)
                item.payload["_recorded"] = True
            self._record_terminal_result(item)
            self._release_source_lease(item)
            self._release_work_permit(item)
            return
        result = ItemResult(passed=passed, reason=reason, final_stage=item.stage)
        self._handoff_item(item, StageName.FINISHED, enter=True, result=result)

    def _install_signal_handlers(self) -> None:
        """SIGINT/SIGTERM/SIGHUP -> one shutdown Event (first graceful, second immediate)."""

        def _handler(signum: int, frame: object) -> None:
            if self.shutdown.is_set():
                logger.warning(shutdown_signal_message(signum, self.config.grace_s, immediate=True))
                self._immediate = True
                self._wake_completion_wait()
            else:
                logger.warning(
                    shutdown_signal_message(signum, self.config.grace_s, immediate=False)
                )
                self.shutdown.set()
                self._grace_deadline = time.monotonic() + self.config.grace_s
                self._wake_completion_wait()

        sigs = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):  # pragma: no branch - always true on POSIX
            sigs.append(signal.SIGHUP)
        for sig in sigs:
            try:
                signal.signal(sig, _handler)
            except ValueError:  # pragma: no cover - not on the main thread
                logger.debug("cannot install handler for %s off the main thread", sig)

    def _teardown_immediate(self) -> None:
        """Cancel the pool and synthesize interrupted results for in-flight items."""
        self.shutdown.set()
        self._force_shutdown.set()
        self._shutdown_pool()

    def _shutdown_pool(self) -> None:
        """Cancel the pool and park in-flight items RESUMABLE. Idempotent.

        Called on BOTH exit paths: the signal path (via
        :meth:`_teardown_immediate`) and — critically — the ``run()`` ``finally``
        block, so a fatal exception (which never sets ``self.shutdown``) still
        cancels the executor and reaps in-flight ``AgentJob`` subprocesses
        instead of leaking them (#2059). Guarded by ``_pool_shut_down`` so a
        signal-then-fatal (or double ``finally``) sequence shuts down once.
        """
        if self._pool_shut_down:
            return
        self._pool_shut_down = True
        try:
            # ``self.shutdown`` belongs to the coordinator's signal path. A
            # normal ``finally`` must reap worker resources without changing
            # its exit outcome to an interruption (#2431), while a genuine
            # signal preserves the pool's direct cancellation semantics.
            self.pool.shutdown(mark_interrupted=self.shutdown.is_set())
        except Exception:  # pragma: no cover - defensive
            logger.exception("pool shutdown raised")
        if self._auxiliary_pool_separate:
            try:
                self.auxiliary_pool.shutdown(mark_interrupted=self.shutdown.is_set())
            except Exception:  # pragma: no cover - defensive
                logger.exception("auxiliary pool shutdown raised")
        try:
            self._drain_completions()
        except RuntimeError as exc:
            if str(exc) != "completion queue saturated":
                raise
        for item in list(self.in_flight.values()):
            self._park_resumable(item)
        for item in list(self.auxiliary_in_flight.values()):
            self._park_resumable(item)
        self.in_flight.clear()
        self.auxiliary_in_flight.clear()
        self._inflight_implementation_claims.clear()
        self._implementation_file_claims.clear()
        self.inflight_per_repo.clear()

    def _finalize_resumable(self) -> None:
        """Mark every still-live item RESUMABLE at its stage (never FAILED)."""
        if not self.shutdown.is_set() and not self._fatal:
            return
        leftovers: list[WorkItem] = [item for _, _, item in self.timers]
        for stage_name, q in self.queues.items():
            if stage_name is StageName.FINISHED:
                continue
            leftovers.extend(q.snapshot())
        leftovers.extend(self.in_flight.values())
        leftovers.extend(self.auxiliary_in_flight.values())
        leftovers.extend(lease.item for lease in self._leases.values())
        leftovers.extend(pending.item for pending in self._pending_handoffs.values())
        seen: set[int] = set()
        for item in leftovers:
            if id(item) in seen:
                continue
            seen.add(id(item))
            if item.result is None:
                self._park_resumable(item)
