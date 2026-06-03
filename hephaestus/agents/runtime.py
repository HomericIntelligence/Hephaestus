"""Shared Claude/Codex process helpers for agent-driven CLIs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

AgentName = Literal["claude", "codex"]
AGENT_CHOICES: tuple[AgentName, ...] = ("claude", "codex")
DEFAULT_AGENT: AgentName = "claude"


@dataclass(frozen=True)
class AgentRunResult:
    """Text output plus optional provider session id."""

    stdout: str
    stderr: str
    session_id: str | None = None


def add_agent_argument(parser: argparse.ArgumentParser) -> None:
    """Add the common provider selector to an agent-driven CLI parser."""
    parser.add_argument(
        "--agent",
        choices=AGENT_CHOICES,
        default=None,
        help=(
            "Agent backend to invoke for model-driven steps "
            "(default: auto-detect, preferring claude when available)"
        ),
    )


def resolve_agent(agent: str | None) -> AgentName:
    """Resolve an optional provider selection into a concrete backend.

    When the operator omits ``--agent``, prefer Claude if both provider CLIs
    are present. Codex is the fallback when Claude is absent.
    """
    if agent is not None:
        if agent not in AGENT_CHOICES:
            raise ValueError(f"Unsupported agent: {agent}")
        return agent
    if shutil.which("claude"):
        return "claude"
    if shutil.which("codex"):
        return "codex"
    raise RuntimeError(
        "No supported agent backend found on PATH. Install `claude` or `codex`, "
        "or pass --agent after installing the selected backend."
    )


def is_codex(agent: str) -> bool:
    """Return True when the selected provider is Codex."""
    return agent == "codex"


def session_agent_matches(session_agent: str | None, selected_agent: str) -> bool:
    """Return True when a persisted session belongs to the selected provider.

    Legacy state files predate provider metadata and only stored Claude session
    ids, so missing metadata is treated as Claude.
    """
    return (session_agent or "claude") == selected_agent


def run_claude_text(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    allowed_tools: str = "Read,Write,Edit,Glob,Grep,Bash",
) -> subprocess.CompletedProcess[str]:
    """Run Claude Code non-interactively and return a text completed process."""
    cmd = ["claude", "--print", "--output-format", "text"]
    if model:
        cmd.extend(["--model", model])
    if sandbox != "read-only":
        cmd.extend(
            [
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                allowed_tools,
            ]
        )

    env = os.environ.copy()
    env["CLAUDECODE"] = ""
    # Propagate correlation ID to subprocess if set (for gh tracing).
    from hephaestus.logging.utils import get_current_correlation_id

    cid = get_current_correlation_id()
    if cid:
        env["GH_TRACE_ID"] = cid
    return subprocess.run(
        cmd,
        input=prompt,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        env=env,
        check=False,
    )


def codex_approval_args(approval: str) -> list[str]:
    """Return approval arguments supported by the installed Codex CLI."""
    try:
        result = subprocess.run(
            ["codex", "exec", "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    help_text = result.stdout or ""
    if "--approval-policy" in help_text:
        return ["--approval-policy", approval]
    if "--ask-for-approval" in help_text:
        return ["--ask-for-approval", approval]
    if "--config <key=value>" in help_text or "-c, --config" in help_text:
        return ["-c", f"approval_policy={json.dumps(approval)}"]
    return []


def run_codex_text(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
) -> subprocess.CompletedProcess[str]:
    """Run Codex non-interactively and return a text completed process."""
    result = run_codex_session(
        prompt,
        cwd=cwd,
        timeout=timeout,
        model=model,
        sandbox=sandbox,
        approval=approval,
    )
    return subprocess.CompletedProcess(
        args=["codex", "exec"],
        returncode=0,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _codex_base_cmd(
    *,
    cwd: Path | None = None,
    model: str = "",
    sandbox: str | None = "workspace-write",
    approval: str = "never",
    resume_id: str | None = None,
) -> list[str]:
    """Build a Codex exec or exec-resume command."""
    cmd = (
        [
            "codex",
            "exec",
            "resume",
            resume_id,
        ]
        if resume_id
        else [
            "codex",
            "exec",
        ]
    )
    if model:
        cmd.extend(["--model", model])
    if resume_id is None:
        if cwd is None:
            raise ValueError("cwd is required for new Codex exec sessions")
        cmd.extend(["--cd", str(cwd)])
        if sandbox is not None:
            cmd.extend(["--sandbox", sandbox])
        cmd.extend(codex_approval_args(approval))
    cmd.extend(["--json"])
    return cmd


def _parse_codex_json_events(text: str) -> tuple[str | None, str]:
    """Extract session id and final text from Codex JSONL output."""
    session_id: str | None = None
    messages: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "session_meta":
            payload = event.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                session_id = payload["id"]
        if event.get("type") == "agent_message" and isinstance(event.get("message"), str):
            messages.append(event["message"])
        payload = event.get("payload")
        if (
            event.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "agent_message"
            and isinstance(payload.get("message"), str)
        ):
            messages.append(payload["message"])
    return session_id, "\n".join(messages).strip()


def run_codex_session(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
) -> AgentRunResult:
    """Run a new persisted Codex exec session and capture its UUID."""
    cmd = _codex_base_cmd(cwd=cwd, model=model, sandbox=sandbox, approval=approval)
    return _run_codex_command(cmd, prompt=prompt, cwd=cwd, timeout=timeout)


def resume_codex_session(
    session_id: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
) -> AgentRunResult:
    """Resume a persisted Codex exec session and capture its latest output."""
    cmd = _codex_base_cmd(model=model, sandbox=None, resume_id=session_id)
    return _run_codex_command(cmd, prompt=prompt, cwd=cwd, timeout=timeout)


def _run_codex_command(
    cmd: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
) -> AgentRunResult:
    """Execute Codex with JSON events and return final text plus session id."""
    with tempfile.NamedTemporaryFile(prefix="codex-last-", suffix=".txt") as output_file:
        cmd.extend(["--output-last-message", output_file.name, "-"])
        env = os.environ.copy()
        env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))
        result = subprocess.run(
            cmd,
            input=prompt,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            env=env,
        )
        last_message = Path(output_file.name).read_text(encoding="utf-8")

    session_id, event_message = _parse_codex_json_events(result.stdout or "")
    stdout = (last_message or event_message or result.stdout or "").strip()
    return AgentRunResult(stdout=stdout, stderr=result.stderr or "", session_id=session_id)


def codex_exec_resume_args(
    session_id: str,
    *,
    model: str = "",
) -> list[str]:
    """Return the Codex command prefix used to resume a non-interactive session."""
    cmd = ["codex", "exec", "resume", session_id]
    if model:
        cmd.extend(["--model", model])
    return cmd


def codex_json_stdout(text: str, session_id: str | None = None) -> str:
    """Wrap Codex text output in the JSON shape expected by Claude callers."""
    return json.dumps({"result": text, "session_id": session_id, "is_error": False})


def extract_agent_text(stdout: str) -> str:
    """Extract model text from either Claude JSON output or raw Codex text."""
    try:
        payload: Any = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return stdout or ""
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, str):
            return result
    return stdout or ""
