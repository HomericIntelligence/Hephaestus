"""Console CLI for session-id-first learning (issue #3518).

The command resumes an existing harness session and sends the learn skill
directive as one new turn. The harness name comes from the catalog. No session
role is passed, so the provider namespace and the role namespace stay separate.
Durable Mnemosyne delivery keeps its queue-owned source-evidence rules.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from hephaestus.agents.claude_invoke import resume_claude_session
from hephaestus.agents.invocation_config import agent_default_timeout
from hephaestus.agents.runtime import (
    add_agent_argument,
    is_pi,
    resolve_agent,
    resume_agent_session,
)
from hephaestus.agents.session_errors import AgentSessionLostError
from hephaestus.cli.localization import text
from hephaestus.cli.utils import (
    add_json_arg,
    add_version_arg,
    emit_json_status,
    positive_int,
)
from hephaestus.errors import (
    ConfigurationError,
    ExitCode,
    InvalidInputError,
    NotFoundError,
    boundary_main,
)

LEARN_PROMPT = "$athena:learn"
"""The skill directive delivered as the resumed session's new turn."""

_AUTOMATION_REQUESTS = "hephaestus.automation.athena_skill_jobs"


def _learn_api() -> tuple[type, Any]:
    """Load the session-id-first request API behind the automation extra."""
    module: ModuleType = importlib.import_module(_AUTOMATION_REQUESTS)
    return module.LearnSessionRequest, module.session_learn_delivery


def _build_parser() -> argparse.ArgumentParser:
    """Build the parser for the ``hephaestus-learn`` console script."""
    parser = argparse.ArgumentParser(
        prog="hephaestus-learn",
        description=text("Resume one harness session and run one learn turn."),
    )
    parser.add_argument(
        "--session-id",
        required=True,
        metavar="ID",
        help=text("Opaque identifier of the session to resume"),
    )
    add_agent_argument(parser)
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path.cwd(),
        metavar="PATH",
        help=text("Project directory that holds the session transcript"),
    )
    parser.add_argument(
        "--model",
        default="",
        metavar="MODEL[:EFFORT]",
        help=text("Model reference used by direct-runner providers"),
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=None,
        metavar="SECONDS",
        help=text("Provider timeout in seconds"),
    )
    add_json_arg(parser)
    add_version_arg(parser)
    return parser


def _run(args: list[str] | None) -> int:
    """Parse arguments, run the learn turn, and report the delivery decision."""
    parsed = _build_parser().parse_args(args)
    session_id = parsed.session_id.strip()
    if not session_id or "\x00" in session_id:
        raise InvalidInputError(text("The session id must be a nonempty string without NUL bytes."))
    cwd = parsed.cwd
    if not cwd.is_dir():
        raise InvalidInputError(text("The --cwd path is not a directory: %(path)s", path=str(cwd)))

    if is_pi(parsed.agent or ""):
        raise ConfigurationError(
            "Pi session resume needs an external OS-isolation adapter and a "
            "queue-owned execution request; a bare session id cannot supply them",
            recovery=(
                "Use the automation queue learning lane, or configure a reviewed "
                "Pi isolation adapter before resuming a Pi session"
            ),
        )

    try:
        agent = resolve_agent(
            parsed.agent,
            cwd=cwd,
            auth_status_timeout=parsed.auth_status_timeout,
            disable_pi_automation=parsed.disable_pi_automation,
            pi_isolation_adapter=parsed.pi_isolation_adapter,
            pi_dir=parsed.pi_dir,
        )
    except RuntimeError as exc:
        raise ConfigurationError(
            str(exc),
            recovery=(
                "Install and authenticate the selected harness, or omit --agent to auto-detect"
            ),
        ) from exc

    request_cls, delivery_fn = _learn_api()
    request = request_cls(
        session_id=session_id,
        agent=agent,
        cwd=cwd,
        prompt=LEARN_PROMPT,
        model=parsed.model,
        timeout=parsed.timeout,
    )
    try:
        if agent == "claude":
            output = resume_claude_session(
                session_id=request.session_id,
                prompt=request.prompt,
                cwd=request.cwd,
                timeout=request.timeout,
            )
        else:
            timeout = request.timeout if request.timeout is not None else agent_default_timeout()
            result = resume_agent_session(
                agent,
                request.session_id,
                request.prompt,
                cwd=request.cwd,
                timeout=timeout,
                model=request.model,
            )
            output = result.stdout
    except AgentSessionLostError as exc:
        raise NotFoundError(str(exc)) from exc

    print(output)
    state = delivery_fn(request)
    if state.state == "deferred":
        print(
            f"{state.reason}: {state.recovery}",
            file=sys.stderr,
        )
    if parsed.json:
        extra: dict[str, Any] = {
            "session_id": request.session_id,
            "agent": agent,
            "delivery": state.state,
        }
        if state.reason is not None:
            extra["delivery_reason"] = state.reason
        emit_json_status(int(ExitCode.OK), **extra)
    return int(ExitCode.OK)


def main(argv: list[str] | None = None) -> int:
    """Run the console-script boundary for ``hephaestus-learn``."""
    return boundary_main(_run, argv)
