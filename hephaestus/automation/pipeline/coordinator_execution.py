"""Job submission and lane-permit ownership for the pipeline coordinator."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .coordinator_contract import _CoordinatorHost
from .coordinator_sessions import store_agent_session_result
from .coordinator_types import *
from .jobs import CompactJob
from .routing import AUXILIARY_PIPELINE_ORDER

# This collaborator consumes the facade's shared type namespace by design.
# ruff: noqa: F403, F405

_PIPELINE_STAGE_LABELS = frozenset(stage.value for stage in StageName)
_JOB_OUTCOME_LABELS = frozenset({"ok", "failed", "interrupted"})


class ExecutionCoordinator(_CoordinatorHost):
    """Own job dispatch and independent main/learning permit budgets."""

    _auxiliary_job_failure_count: int

    @property
    def live_work_count(self) -> int:
        """Return the number of nonterminal main-lane permits."""
        return len(self._live_work_permit_ids)

    @property
    def learning_work_count(self) -> int:
        """Return the number of nonterminal auxiliary permits."""
        return len(self._learning_work_permit_ids)

    def _try_acquire_work_permit(self, item: WorkItem, stage: StageName | None = None) -> bool:
        """Reserve capacity in the lane that owns the target stage."""
        item_id = id(item)
        target = stage or item.stage
        if self._is_auxiliary_stage(target):
            if item_id in self._learning_work_permit_ids:
                return True
            if self.learning_work_count >= self.config.learning_queue_capacity:
                return False
            self._learning_work_permit_ids.add(item_id)
            return True
        if item_id in self._live_work_permit_ids:
            return True
        if self.live_work_count >= _work_window(self.config):
            return False
        self._live_work_permit_ids.add(item_id)
        return True

    def _release_work_permit(self, item: WorkItem) -> None:
        """Release every lane permit held by an item."""
        self._live_work_permit_ids.discard(id(item))
        self._learning_work_permit_ids.discard(id(item))

    def _lane_handoff_capacity(self, item: WorkItem, target: StageName) -> bool:
        """Check destination capacity before source ownership is released."""
        # FINISHED is a terminal sink, not a working lane: it must never reject
        # a handoff or a burst of terminalizations would wedge the source drain
        # (its lease stays active and the drain bails). This is the
        # #2057 duplicate-collapse path — three copies of one issue terminalize
        # in a single drain round while the first copy dispatches.
        if target is StageName.FINISHED:
            return True
        source_auxiliary = self._is_auxiliary_stage(item.stage)
        target_auxiliary = self._is_auxiliary_stage(target)
        if not source_auxiliary and target_auxiliary:
            return self.learning_work_count < self.config.learning_queue_capacity
        if source_auxiliary and not target_auxiliary:
            return self.live_work_count < _work_window(self.config)
        return True

    @staticmethod
    def _is_auxiliary_stage(stage: StageName) -> bool:
        """Return whether a stage uses the auxiliary permit and worker lane."""
        return stage in AUXILIARY_PIPELINE_ORDER

    def _persist_learning_intents(self, item: WorkItem) -> None:
        """Write every intent before a destination queue owns the item."""
        journal = self._ctx_for_repo(item.repo).learning_journal
        for intent in item.learning_intents:
            journal.ensure_pending(
                intent.key,
                kind=intent.kind.value,
                identity=item.learning_journal_identity(intent),
            )

    def _submit(self, item: WorkItem, request: JobRequest) -> None:
        """Submit a frozen job to its lane and register its ownership."""
        assert not self.config.dry_run, "dry-run must never submit jobs"  # noqa: S101
        job: Any = request.job
        if isinstance(job, AgentJob):
            ok, delay = self._rate_budget_ok()
            if not ok:
                self._timer_park(item, delay)
                return
            if self.config.phase_timeout_s and self.config.phase_timeout_s > 0:
                job = replace(job, timeout_s=int(self.config.phase_timeout_s))
            job = replace(
                job,
                disable_pi_automation=self.config.disable_pi_automation,
                auth_status_timeout=self.config.auth_status_timeout,
                pi_isolation_adapter=self.config.pi_isolation_adapter,
                pi_dir=self.config.pi_dir,
                fallback_model=self.config.fallback_model,
                plugin_skills_dir=self.config.plugin_skills_dir,
            )
        elif isinstance(job, CompactJob):
            job = replace(
                job,
                disable_pi_automation=self.config.disable_pi_automation,
                auth_status_timeout=self.config.auth_status_timeout,
            )
        claims = self._capture_implementation_file_claims(item)
        auxiliary = self._auxiliary_pool_separate and self._is_auxiliary_stage(item.stage)
        if auxiliary:
            handle = self.auxiliary_pool.submit(job, request.on_done_state)
            self.auxiliary_in_flight[handle] = item
        else:
            handle = self.pool.submit(
                job,
                request.on_done_state,
                claim_key=self._item_key(item),
                claim_stage=item.stage.value,
            )
            self.in_flight[handle] = item
            self.inflight_per_repo[item.repo] += 1
        if claims:
            self._inflight_implementation_claims[handle] = claims
        self._record_event(
            "submit",
            type(job).__name__,
            self._item_key(item),
            request.on_done_state,
            {"lane": "auxiliary" if auxiliary else "main"},
        )

    def _rate_budget_ok(self) -> tuple[bool, float]:
        """Return the non-blocking GitHub rate-budget decision."""
        from hephaestus.automation.pipeline_github_transport import rate_budget_ok

        return rate_budget_ok(
            enabled=self.config.rate_guard_enabled,
            threshold=self.config.rate_guard_threshold,
            timeout=self.config.gh_timeout,
        )

    def _drain_completions(self) -> None:
        """Drain all ready completions from both worker lanes."""
        self._completion_wakeup.clear()
        while True:
            try:
                handle, result = self.completion_q.get_nowait()
            except queue_mod.Empty:
                break
            self._handle_completion(handle, result)
        if self._auxiliary_pool_separate:
            while True:
                try:
                    handle, result = self.auxiliary_completion_q.get_nowait()
                except queue_mod.Empty:
                    break
                self._handle_completion(handle, result, auxiliary=True)
        if self._completion_saturation.is_set():
            self._record_event("completion_saturation")
            raise RuntimeError("completion queue saturated")

    def _wait_for_completion(self, timeout: float) -> None:
        """Wait for a completion, saturation fault, or signal wake-up."""
        if self.completion_q.empty() and not self._completion_saturation.is_set():
            self._completion_wakeup.wait(timeout=timeout)
        self._drain_completions()

    def _handle_completion(  # noqa: C901 - one protocol serves both closed lanes
        self, handle: JobHandle, result: JobResult, *, auxiliary: bool = False
    ) -> None:
        """Record and route one result from the lane that owned the job."""
        self._progress = True
        registry = self.auxiliary_in_flight if auxiliary else self.in_flight
        item = registry.pop(handle, None)
        self._inflight_implementation_claims.pop(handle, None)
        if item is None:
            self._record_event(
                "complete_unknown",
                type(handle.job).__name__,
                handle.on_done_state,
                self._job_result_event_fields(result),
            )
            logger.warning("completion for unknown handle (already torn down?): %s", handle)
            return
        self._record_event(
            "complete",
            type(handle.job).__name__,
            self._item_key(item),
            item.stage.value,
            handle.on_done_state,
            {
                "descr": getattr(handle.job, "descr", ""),
                "lane": "auxiliary" if auxiliary else "main",
                **self._job_result_event_fields(result),
            },
        )
        if not auxiliary:
            self.inflight_per_repo[item.repo] -= 1
            if self.inflight_per_repo[item.repo] <= 0:
                del self.inflight_per_repo[item.repo]
        if isinstance(handle.job, AgentJob):
            self._agent_job_count += 1
            self._agent_job_time_s += result.duration_s
        if auxiliary:
            self._auxiliary_job_count += 1
            self._auxiliary_job_time_s += result.duration_s
            if not result.ok:
                self._auxiliary_job_failure_count += 1
        self._record_completion_metrics(item, handle, result, auxiliary=auxiliary)

        stage = self.stages[item.stage]
        ctx = self._ctx_for(item)
        if result.interrupted:
            if item.stage is StageName.LEARNING and result.error == "interrupted_before_start":
                cancelled = getattr(stage, "on_cancelled_before_start", None)
                if callable(cancelled):
                    cancelled(item, ctx)
            self._park_resumable(item)
            return
        if isinstance(handle.job, AgentJob):
            session_error = store_agent_session_result(item, handle.job, result)
            if session_error is not None:
                self._finish(item, passed=False, reason=session_error)
                return
        try:
            stage.on_job_done(item, result, ctx)
        except Exception:
            logger.exception(
                "on_job_done poisoned item %s at %s", self._item_key(item), item.stage.value
            )
            if item.stage is StageName.LEARNING:
                item.payload.setdefault("learning_failures", []).append(
                    {
                        "key": "journal",
                        "error": "journal_completion_failed",
                    }
                )
                self._park_resumable(item)
                return
            self._finish(item, passed=False, reason="poisoned: on_job_done raised")
            return
        self._register_pipeline_writer_worktree(item, handle.job, result)
        item.state = (
            handle.on_done_state.value
            if isinstance(handle.on_done_state, StageName)
            else handle.on_done_state
        )
        if self.shutdown.is_set():
            self._park_resumable(item)
            return
        self._run_item(item)

    def _record_completion_metrics(
        self,
        item: WorkItem,
        handle: JobHandle,
        result: JobResult,
        *,
        auxiliary: bool,
    ) -> None:
        """Update the bounded metric series for one completed job."""
        if self._metrics_registry is None:
            return
        outcome = "interrupted" if result.interrupted else ("ok" if result.ok else "failed")
        self._metrics_registry.counter(
            "hephaestus_pipeline_jobs_total",
            "Completed pipeline jobs by stage and outcome.",
            allowed_labels={
                "stage": _PIPELINE_STAGE_LABELS,
                "outcome": _JOB_OUTCOME_LABELS,
            },
            series_cap=len(_PIPELINE_STAGE_LABELS) * len(_JOB_OUTCOME_LABELS),
        ).inc(labels={"stage": item.stage.value, "outcome": outcome})
        if isinstance(handle.job, AgentJob):
            self._metrics_registry.counter(
                "hephaestus_pipeline_agent_job_seconds_total",
                "Cumulative agent job wall-clock seconds.",
                allowed_labels={},
                series_cap=1,
            ).inc(max(result.duration_s, 0.0))
        if auxiliary:
            self._metrics_registry.counter(
                "hephaestus_pipeline_auxiliary_job_seconds_total",
                "Cumulative auxiliary-lane job wall-clock seconds.",
                allowed_labels={},
                series_cap=1,
            ).inc(max(result.duration_s, 0.0))
