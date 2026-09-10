"""Session-result validation shared by the pipeline coordinator."""

from __future__ import annotations

from hephaestus.agents.execution_policy import SessionLifecycle
from hephaestus.agents.pi_session import validate_pi_binding
from hephaestus.agents.runtime import AgentExecutionError, resolve_pi_model_reference

from .jobs import AgentJob, CompactJob, JobResult
from .work_item import WorkItem


def store_agent_session_result(
    item: WorkItem,
    job: AgentJob,
    result: JobResult,
) -> str | None:
    """Persist a successful direct session, returning a fail-closed error."""
    # A provider may establish the conversation and then return malformed
    # output.  Persist that identity before parse/retry handling so a retry
    # cannot silently fork the conversation.
    session_key = job.session_key or job.session_agent or job.agent
    if result.session_binding is not None:
        if job.execution_request is None:
            return "Pi binding returned without execution request"
        try:
            effective_model = resolve_pi_model_reference(job.model, pi_dir=job.pi_dir)
            validate_pi_binding(
                result.session_binding,
                cwd=job.cwd,
                role=job.execution_request.role,
                model=effective_model,
            )
        except (AgentExecutionError, ValueError) as exc:
            return f"invalid Pi session binding: {exc}"
        item.session_bindings[session_key] = result.session_binding
    elif result.session_id:
        item.session_ids[session_key] = result.session_id
    if result.session_id or result.session_binding is not None:
        item.session_selections[session_key] = (job.agent, str(job.model))
    return None


def session_selection_error(item: WorkItem, job: AgentJob | CompactJob) -> str | None:
    """Reject a stored session when its tool or model selection changed."""
    key = (job.session_key if isinstance(job, AgentJob) else "") or job.session_agent or job.agent
    previous = item.session_selections.get(key)
    if isinstance(job, AgentJob):
        previous = previous or job.resume_selection
        explicit_resume = bool(job.resume_session_id or job.resume_binding)
        lifecycle = job.execution_request.lifecycle.value if job.execution_request else None
        implicit_resume = (
            job.agent == "claude" and previous is not None and lifecycle != "start_new"
        )
        if not explicit_resume and not implicit_resume:
            return None
    elif not (job.session_id or job.session_binding or job.agent == "claude"):
        return None
    if previous is None:
        return "session selection is missing; start a new session"
    if tuple(previous) != (job.agent, str(job.model)):
        return "session tool or model changed; start a new session"
    return None


def agent_session_lifecycle(item: WorkItem, key: str) -> SessionLifecycle:
    """Resume a recorded session; otherwise start a new conversation."""
    if key in item.session_bindings or key in item.session_ids:
        return SessionLifecycle.RESUME_REQUIRED
    return SessionLifecycle.START_NEW


__all__ = ["store_agent_session_result"]
