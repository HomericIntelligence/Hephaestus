"""Shared process helpers for agent-driven CLIs."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

AgentName = Literal["claude", "codex", "pi"]
AGENT_CHOICES: tuple[AgentName, ...] = ("claude", "codex", "pi")
DEFAULT_AGENT: AgentName = "claude"
AGENT_AUTH_STATUS_TIMEOUT = 10
CODEX_FINAL_MESSAGE_GRACE_ENV = "HEPH_CODEX_FINAL_MESSAGE_GRACE"
CODEX_FINAL_MESSAGE_GRACE_SECONDS = 5.0
PI_MODEL_ENV = "HEPH_PI_MODEL"
PI_MODEL_CONFIG_RELATIVE_PATH = Path(".pi") / "agent" / "models.json"
PI_READ_ONLY_TOOLS = "read,grep,find,ls"
AGENT_AUTH_STATUS_COMMANDS: dict[AgentName, tuple[tuple[str, ...], ...]] = {
    "claude": (("claude", "auth", "status"),),
    "codex": (("codex", "login", "status"),),
    "pi": (("pi", "--version"),),
}


@dataclass(frozen=True)
class AgentRunResult:
    """Text output plus optional provider session id."""

    stdout: str
    stderr: str
    session_id: str | None = None


@dataclass(frozen=True)
class AgentCapabilities:
    """Backend capabilities used by provider-neutral call sites."""

    direct_runner: bool
    supports_approval: bool
    supports_sandbox: bool
    supports_sessions: bool


AGENT_CAPABILITIES: dict[AgentName, AgentCapabilities] = {
    "claude": AgentCapabilities(
        direct_runner=False,
        supports_approval=False,
        supports_sandbox=True,
        supports_sessions=True,
    ),
    "codex": AgentCapabilities(
        direct_runner=True,
        supports_approval=True,
        supports_sandbox=True,
        supports_sessions=True,
    ),
    "pi": AgentCapabilities(
        direct_runner=True,
        supports_approval=False,
        supports_sandbox=True,
        supports_sessions=True,
    ),
}


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
            if agent == "pi":
                return _pi_models_configured()
            return True
    return False


def _pi_models_configured() -> bool:
    """Return True when Pi has at least one local model alias configured."""
    config_path = Path.home() / PI_MODEL_CONFIG_RELATIVE_PATH
    try:
        payload: Any = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    if isinstance(payload, dict):
        models = payload.get("models")
        if isinstance(models, (dict, list)):
            return bool(models)
        return bool(payload)
    if isinstance(payload, list):
        return bool(payload)
    return False


def resolve_agent(agent: str | None) -> AgentName:
    """Resolve an optional provider selection into a concrete backend."""
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
            status_hint = (
                "`pi --version` and check ~/.pi/agent/models.json"
                if agent == "pi"
                else f"`{agent} auth status` (or `{agent} login status`)"
            )
            raise RuntimeError(
                f"Agent '{agent}' is installed but not authenticated. "
                f"Run {status_hint} before running automation."
            )
        return agent

    installed_agents = tuple(agent_name for agent_name in AGENT_CHOICES if shutil.which(agent_name))
    if not installed_agents:
        raise RuntimeError(
            "No supported agent backend found on PATH. Install `claude`, `codex`, or `pi`, "
            "or pass --agent after installing the selected backend."
        )

    for agent_name in installed_agents:
        if is_agent_authenticated(agent_name):
            return agent_name

    raise RuntimeError(
        "Supported agent backends are installed but none are authenticated. "
        "Run `claude auth status`, `codex login status`, or `pi --version`, then "
        "log in/configure the provider you want automation to use."
    )


def is_codex(agent: str) -> bool:
    """Return True when the selected provider is Codex."""
    return agent == "codex"


def is_pi(agent: str) -> bool:
    """Return True when the selected provider is Pi."""
    return agent == "pi"


def uses_direct_agent_runner(agent: str) -> bool:
    """Return True when the provider is invoked through runtime text/session helpers."""
    if agent not in AGENT_CAPABILITIES:
        return False
    return AGENT_CAPABILITIES[agent].direct_runner


def direct_agent_model(agent: str, phase_env_var: str) -> str:
    """Return a model override appropriate for a direct-runner provider."""
    if is_pi(agent):
        return os.environ.get(PI_MODEL_ENV, "")
    return os.environ.get(phase_env_var, "")


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


def _pi_message_text(message: Any) -> str:
    """Extract assistant text from a Pi message object."""
    if not isinstance(message, dict):
        return ""
    if message.get("role") not in (None, "assistant"):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(item.get("delta"), str):
                    parts.append(item["delta"])
        return "".join(parts).strip()
    text = message.get("text")
    return text.strip() if isinstance(text, str) else ""


def _parse_pi_json_events(text: str) -> tuple[str | None, str]:
    """Extract Pi session id and final assistant text from JSONL output."""
    session_id: str | None = None
    final_message = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "session" and isinstance(event.get("id"), str):
            session_id = event["id"]
        if event.get("type") in {"message_end", "turn_end"}:
            message_text = _pi_message_text(event.get("message"))
            if message_text:
                final_message = message_text
        if event.get("type") == "agent_end":
            raw_messages = event.get("messages")
            if isinstance(raw_messages, list):
                for message in raw_messages:
                    message_text = _pi_message_text(message)
                    if message_text:
                        final_message = message_text
    return session_id, final_message.strip()


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


def _pi_base_cmd(*, model: str = "", session_id: str | None = None) -> list[str]:
    """Build a Pi JSON-mode command."""
    resolved_model = model or os.environ.get(PI_MODEL_ENV, "")
    cmd = ["pi", "--mode", "json"]
    if session_id:
        cmd.extend(["--session", session_id])
    if resolved_model:
        cmd.extend(["--model", resolved_model])
    return cmd


def _pi_sandbox_args(sandbox: str) -> list[str]:
    """Return Pi tool restrictions for the requested sandbox mode."""
    if sandbox == "read-only":
        return ["--tools", PI_READ_ONLY_TOOLS]
    if sandbox in {"workspace-write", "danger-full-access"}:
        return []
    raise ValueError(f"Unsupported Pi sandbox mode: {sandbox}")


def _pi_env() -> dict[str, str]:
    """Return a privacy-biased environment for Pi subprocesses."""
    env = os.environ.copy()
    env.setdefault("PI_TELEMETRY", "0")
    env.setdefault("PI_SKIP_VERSION_CHECK", "1")
    return env


def _run_pi_command(
    cmd: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    sandbox: str,
) -> subprocess.CompletedProcess[str]:
    """Run Pi with prompt content attached via an ephemeral file, not argv."""
    prompt_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            prefix="pi-prompt-",
            suffix=".md",
            encoding="utf-8",
            delete=False,
        ) as prompt_file:
            prompt_file.write(prompt)
            prompt_path = Path(prompt_file.name)
        prompt_path.chmod(0o600)
        cmd.extend(_pi_sandbox_args(sandbox))
        cmd.append(f"@{prompt_path}")
        return subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=_pi_env(),
            check=True,
        )
    finally:
        if prompt_path is not None:
            with contextlib.suppress(OSError):
                prompt_path.unlink()


def run_pi_text(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
) -> subprocess.CompletedProcess[str]:
    """Run Pi non-interactively and return a text completed process."""
    del approval
    result = run_pi_session(prompt, cwd=cwd, timeout=timeout, model=model, sandbox=sandbox)
    return subprocess.CompletedProcess(
        args=["pi", "--mode", "json"],
        returncode=0,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def run_pi_session(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
) -> AgentRunResult:
    """Run a new Pi JSON-mode session and capture its id."""
    del approval
    cmd = _pi_base_cmd(model=model)
    result = _run_pi_command(
        cmd,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        sandbox=sandbox,
    )
    session_id, event_message = _parse_pi_json_events(result.stdout or "")
    stdout = (event_message or result.stdout or "").strip()
    return AgentRunResult(stdout=stdout, stderr=result.stderr or "", session_id=session_id)


def resume_pi_session(
    session_id: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
) -> AgentRunResult:
    """Resume a Pi JSON-mode session by id."""
    cmd = _pi_base_cmd(model=model, session_id=session_id)
    result = _run_pi_command(
        cmd,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        sandbox="workspace-write",
    )
    parsed_session_id, event_message = _parse_pi_json_events(result.stdout or "")
    stdout = (event_message or result.stdout or "").strip()
    return AgentRunResult(
        stdout=stdout,
        stderr=result.stderr or "",
        session_id=parsed_session_id or session_id,
    )


def run_agent_text(
    agent: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
) -> subprocess.CompletedProcess[str]:
    """Run a direct-runner agent non-interactively and return text output."""
    if is_codex(agent):
        return run_codex_text(
            prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            sandbox=sandbox,
            approval=approval,
        )
    if is_pi(agent):
        return run_pi_text(
            prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            sandbox=sandbox,
            approval=approval,
        )
    raise ValueError(f"Agent '{agent}' does not support direct text execution")


def run_agent_session(
    agent: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
) -> AgentRunResult:
    """Run a direct-runner agent session and return output plus session id."""
    if is_codex(agent):
        return run_codex_session(
            prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            sandbox=sandbox,
            approval=approval,
        )
    if is_pi(agent):
        return run_pi_session(
            prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            sandbox=sandbox,
            approval=approval,
        )
    raise ValueError(f"Agent '{agent}' does not support direct session execution")


def resume_agent_session(
    agent: str,
    session_id: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
) -> AgentRunResult:
    """Resume a direct-runner agent session."""
    if is_codex(agent):
        return resume_codex_session(session_id, prompt, cwd=cwd, timeout=timeout, model=model)
    if is_pi(agent):
        return resume_pi_session(session_id, prompt, cwd=cwd, timeout=timeout, model=model)
    raise ValueError(f"Agent '{agent}' does not support direct session resume")


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
    """Extract model text from either Claude JSON output or raw direct-agent text."""
    try:
        payload: Any = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return stdout or ""
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, str):
            return result
    return stdout or ""
