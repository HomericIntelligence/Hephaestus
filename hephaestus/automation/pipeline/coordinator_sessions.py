"""Session-result validation shared by the pipeline coordinator."""

from __future__ import annotations

from hephaestus.agents.pi_session import validate_pi_binding

from .jobs import AgentJob, JobResult
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
            validate_pi_binding(
                result.session_binding,
                cwd=job.cwd,
                role=job.execution_request.role,
                model=job.model,
            )
        except ValueError as exc:
            return f"invalid Pi session binding: {exc}"
        item.session_bindings[session_key] = result.session_binding
    elif result.session_id:
        item.session_ids[session_key] = result.session_id
    return None


__all__ = ["store_agent_session_result"]
