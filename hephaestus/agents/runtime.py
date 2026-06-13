"""Shared Claude/Codex process helpers for agent-driven CLIs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

AgentName = Literal["claude", "codex"]
AGENT_CHOICES: tuple[AgentName, ...] = ("claude", "codex")
DEFAULT_AGENT: AgentName = "claude"
AGENT_AUTH_STATUS_TIMEOUT = 10
CODEX_FINAL_MESSAGE_GRACE_ENV = "HEPH_CODEX_FINAL_MESSAGE_GRACE"
CODEX_FINAL_MESSAGE_GRACE_SECONDS = 5.0
AGENT_AUTH_STATUS_COMMANDS: dict[AgentName, tuple[tuple[str, ...], ...]] = {
    "claude": (("claude", "auth", "status"),),
    "codex": (("codex", "login", "status"),),
}


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
            "(default: auto-detect authenticated backend, preferring claude when authenticated)"
        ),
    )


def is_agent_authenticated(agent: AgentName) -> bool:
    """Return True when the provider CLI is installed and reports logged-in auth."""
    if shutil.which(agent) is None:
        return False

    for cmd in AGENT_AUTH_STATUS_COMMANDS[agent]:
        try:
            result = subprocess.run(
                list(cmd),
                text=True,
                capture_output=True,
                timeout=AGENT_AUTH_STATUS_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return True
    return False


def resolve_agent(agent: str | None) -> AgentName:
    """Resolve an optional provider selection into a concrete backend.

    When the operator omits ``--agent``, choose an installed provider that also
    reports authenticated status. Claude still wins ties when both providers are
    authenticated.

    When the operator explicitly passes ``--agent``, the selected provider must
    still be installed and authenticated — an unauthenticated backend would
    silently produce empty output, causing every automation phase to report
    success while doing no work.
    """
    if agent is not None:
        if agent not in AGENT_CHOICES:
            raise ValueError(f"Unsupported agent: {agent}")
        if not is_agent_authenticated(agent):
            if shutil.which(agent) is None:
                raise RuntimeError(
                    f"Agent '{agent}' is not installed on PATH. "
                    f"Install the '{agent}' CLI and try again, "
                    f"or omit --agent to auto-detect an authenticated backend."
                )
            raise RuntimeError(
                f"Agent '{agent}' is installed but not authenticated. "
                f"Run `{agent} auth status` (or `{agent} login status`) "
                f"and log in before running automation."
            )
        return agent

    installed_agents = tuple(agent_name for agent_name in AGENT_CHOICES if shutil.which(agent_name))
    if not installed_agents:
        raise RuntimeError(
            "No supported agent backend found on PATH. Install `claude` or `codex`, "
            "or pass --agent after installing the selected backend."
        )

    for agent_name in installed_agents:
        if is_agent_authenticated(agent_name):
            return agent_name

    raise RuntimeError(
        "Supported agent backends are installed but none are authenticated. "
        "Run `claude auth status` or `codex login status`, then log in to the "
        "provider you want automation to use."
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
        try:
            stdout_text, stderr_text = _communicate_codex_process(
                cmd,
                cwd=cwd,
                prompt=prompt,
                timeout=timeout,
                env=env,
                output_path=Path(output_file.name),
            )
        except subprocess.TimeoutExpired as e:
            last_message = Path(output_file.name).read_text(encoding="utf-8").strip()
            stdout_text = _coerce_timeout_output(e.stdout)
            stderr_text = _coerce_timeout_output(e.stderr)
            if not last_message:
                raise
            session_id, _ = _parse_codex_json_events(stdout_text)
            return AgentRunResult(
                stdout=last_message,
                stderr=stderr_text or f"Codex wrapper timed out after {timeout}s",
                session_id=session_id,
            )
        last_message = Path(output_file.name).read_text(encoding="utf-8")

    session_id, event_message = _parse_codex_json_events(stdout_text)
    stdout = (last_message or event_message or stdout_text or "").strip()
    return AgentRunResult(stdout=stdout, stderr=stderr_text, session_id=session_id)


def _communicate_codex_process(
    cmd: list[str],
    *,
    cwd: Path,
    prompt: str,
    timeout: int,
    env: dict[str, str],
    output_path: Path,
) -> tuple[str, str]:
    """Run Codex and recover when a completed final message leaves the wrapper alive."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        text=True,
        env=env,
    )
    started_at = time.monotonic()
    final_seen_at: float | None = None
    input_text: str | None = prompt
    grace_seconds = _codex_final_message_grace_seconds()

    while True:
        elapsed = time.monotonic() - started_at
        remaining = timeout - elapsed
        if remaining <= 0:
            stdout_text, stderr_text = _terminate_codex_process(proc)
            last_message = _read_text_file(output_path).strip()
            if last_message:
                return stdout_text, stderr_text or f"Codex wrapper timed out after {timeout}s"
            raise subprocess.TimeoutExpired(cmd, timeout, output=stdout_text, stderr=stderr_text)

        try:
            stdout_text, stderr_text = proc.communicate(
                input=input_text,
                timeout=min(1.0, remaining),
            )
            if proc.returncode:
                raise subprocess.CalledProcessError(
                    proc.returncode,
                    cmd,
                    output=stdout_text,
                    stderr=stderr_text,
                )
            return stdout_text or "", stderr_text or ""
        except subprocess.TimeoutExpired:
            input_text = None
            if _read_text_file(output_path).strip():
                final_seen_at = final_seen_at or time.monotonic()
                if time.monotonic() - final_seen_at >= grace_seconds:
                    stdout_text, stderr_text = _terminate_codex_process(proc)
                    return (
                        stdout_text,
                        stderr_text or "Codex wrapper terminated after final message",
                    )


def _terminate_codex_process(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Terminate a Codex process and collect any remaining stdout/stderr."""
    if proc.poll() is None:
        proc.terminate()
    try:
        stdout_text, stderr_text = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout_text, stderr_text = proc.communicate()
    return stdout_text or "", stderr_text or ""


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _codex_final_message_grace_seconds() -> float:
    raw = os.environ.get(CODEX_FINAL_MESSAGE_GRACE_ENV)
    if raw is None:
        return CODEX_FINAL_MESSAGE_GRACE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return CODEX_FINAL_MESSAGE_GRACE_SECONDS
    return max(0.0, value)


def _coerce_timeout_output(output: str | bytes | None) -> str:
    """Return text from ``TimeoutExpired`` stdout/stderr regardless of mode."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


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
