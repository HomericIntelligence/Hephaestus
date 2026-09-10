"""Compact the provider sessions owned by queue jobs."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from hephaestus.agents.execution_policy import ExecutionRequest
from hephaestus.agents.model_selection import parse_model_selection
from hephaestus.agents.pi_session import AgentSessionBinding
from hephaestus.agents.runtime import agent_compaction_resume, resolve_agent, resume_agent_session
from hephaestus.automation.agent_config import (
    learn_claude_timeout,
    session_uuid,
)
from hephaestus.config.child_environments import build_claude_child_env

logger = logging.getLogger(__name__)


def compact_session(
    repo: str,
    issue: int | str,
    agent: str,
    cwd: Path,
    timeout: int | None = None,
    model: str | None = None,
) -> bool:
    """Send ``/compact`` to one Claude session.

    A failed compaction returns False. The next turn can still use the
    complete transcript.

    Args:
        repo: Repository slug.
        issue: Issue or PR number.
        agent: Session role name.
        cwd: Working directory for session lookup.
        timeout: Subprocess timeout in seconds.
        model: Optional ``MODEL[:EFFORT]`` value. Session lookup uses the model.

    Returns:
        True if compaction succeeds; otherwise False.

    """
    effective_model = parse_model_selection(model).model if model is not None else None
    sid = session_uuid(repo, issue, agent, effective_model, cwd=cwd)
    timeout_s = learn_claude_timeout() if timeout is None else timeout
    try:
        result = subprocess.run(
            [
                "claude",
                "--resume",
                sid,
                "--output-format",
                "text",
                "--print",
            ],
            input="/compact",
            cwd=str(cwd),
            timeout=timeout_s,
            check=False,
            capture_output=True,
            text=True,
            env=build_claude_child_env(),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(
            "Issue #%s: /compact failed for agent=%s (non-fatal): %s",
            issue,
            agent,
            e,
        )
        return False

    if result.returncode != 0:
        stderr = result.stderr or ""
        # A session can be absent when its role did no work.
        if "No conversation found with session ID" in stderr:
            logger.debug(
                "Issue #%s: no %s session to compact (session %s); skipping",
                issue,
                agent,
                sid,
            )
            return False
        logger.warning(
            "Issue #%s: /compact for agent=%s exited %s (non-fatal); stderr=%s",
            issue,
            agent,
            result.returncode,
            stderr[:200],
        )
        return False

    logger.info("Issue #%s: /compact completed for agent=%s (session %s)", issue, agent, sid)
    return True


def compact_agent_session(
    repo: str,
    issue: int | str,
    provider: str,
    session_agent: str,
    cwd: Path,
    timeout: int | None = None,
    model: str | None = None,
    session_id: str | None = None,
    sandbox: str = "read-only",
    execution_request: ExecutionRequest | None = None,
    session_binding: AgentSessionBinding | None = None,
    disable_pi_automation: bool = False,
    auth_status_timeout: int = 10,
    pi_isolation_adapter: str | None = None,
    pi_dir: Path | None = None,
) -> bool:
    """Compact a stored provider session.

    Claude uses the session name derived from its role and repository.
    Other providers use the stored session ID and required session binding.
    Return False when the session cannot be resumed or compaction fails.
    """
    if provider == "claude":
        return compact_session(repo, issue, session_agent, cwd, timeout, model)
    provider = resolve_agent(
        provider,
        cwd=cwd,
        disable_pi_automation=disable_pi_automation,
        auth_status_timeout=auth_status_timeout,
        pi_isolation_adapter=pi_isolation_adapter,
        pi_dir=pi_dir,
        model_references=(model or "",),
    )
    resume = agent_compaction_resume(
        provider,
        session_agent=session_agent,
        session_id=session_id,
        session_binding=session_binding,
        execution_request=execution_request,
    )
    if resume is None:
        logger.debug(
            "Issue #%s: no resumable %s session to compact for agent=%s; skipping",
            issue,
            provider,
            session_agent,
        )
        return False
    resume_session_id, resume_options = resume
    timeout_s = learn_claude_timeout() if timeout is None else timeout
    try:
        resume_agent_session(
            agent=provider,
            session_id=resume_session_id,
            prompt="/compact",
            cwd=cwd,
            timeout=timeout_s,
            model=model or "",
            sandbox=sandbox,
            approval="never",
            disable_pi_automation=disable_pi_automation,
            pi_dir=pi_dir,
            **resume_options,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError, ValueError) as exc:
        logger.warning(
            "Issue #%s: /compact failed for provider=%s agent=%s (non-fatal): %s",
            issue,
            provider,
            session_agent,
            exc,
        )
        return False
    logger.info(
        "Issue #%s: /compact completed for provider=%s agent=%s",
        issue,
        provider,
        session_agent,
    )
    return True
