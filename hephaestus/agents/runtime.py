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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from hephaestus.constants import (
    agent_auth_status_timeout,
)
from hephaestus.utils.helpers import strip_null_bytes

AgentName = Literal["claude", "codex", "pi"]
SubprocessCommandPart = str | bytes | os.PathLike[str] | os.PathLike[bytes]
SubprocessCommand = SubprocessCommandPart | Sequence[SubprocessCommandPart]
AGENT_CHOICES: tuple[AgentName, ...] = ("claude", "codex", "pi")
DEFAULT_AGENT: AgentName = "claude"
CODEX_HELP_PROBE_SECONDS = 10
GIT_COMMON_DIR_PROBE_SECONDS = 5
CODEX_TERMINATION_GRACE_SECONDS = 5
CODEX_FINAL_MESSAGE_GRACE_ENV = "HEPH_CODEX_FINAL_MESSAGE_GRACE"
CODEX_FINAL_MESSAGE_GRACE_SECONDS = 5.0
CODEX_GPT_56_MODEL = "gpt-5.6"
CODEX_GPT_55_MODEL = "gpt-5.5"
CODEX_FABLE_MODEL = CODEX_GPT_55_MODEL
CODEX_FABLE_REASONING_EFFORT = "xhigh"
CODEX_OPUS_MODEL = CODEX_GPT_55_MODEL
CODEX_OPUS_REASONING_EFFORT = "xhigh"
CODEX_SONNET_MODEL = CODEX_GPT_55_MODEL
CODEX_SONNET_REASONING_EFFORT = "medium"
CODEX_HAIKU_MODEL = "gpt-5.4-mini"
CODEX_DEFAULT_MODEL = CODEX_OPUS_MODEL
CODEX_DEFAULT_REASONING_EFFORT = CODEX_OPUS_REASONING_EFFORT
CODEX_PARENT_CONTEXT_ENV_VARS = ("CODEX_THREAD_ID",)
PI_PROVIDER_ENV = "HEPH_PI_PROVIDER"
PI_MODEL_ENV = "HEPH_PI_MODEL"
PI_MODEL_CONFIG_RELATIVE_PATH = Path(".pi") / "agent" / "models.json"
PI_PRIVATE_DENYLIST_FILENAME = ".heph-private-denylist"
PI_PRIVATE_REDACTION = "<redacted-pi-private-value>"
PI_READ_ONLY_TOOLS = "read,grep,find,ls"
REQUIRED_ALIAS_ENVS: tuple[str, ...] = (PI_PROVIDER_ENV, PI_MODEL_ENV)
AGENT_AUTH_STATUS_COMMANDS: dict[AgentName, tuple[tuple[str, ...], ...]] = {
    "claude": (("claude", "auth", "status"),),
    "codex": (("codex", "login", "status"),),
    "pi": (("pi", "--version"),),
}


def missing_pi_alias_env(
    required: tuple[str, ...] = REQUIRED_ALIAS_ENVS,
) -> list[str]:
    """Return required Pi alias env vars that are unset or blank."""
    return [name for name in required if not os.environ.get(name, "").strip()]


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


@dataclass(frozen=True)
class CodexModelConfig:
    """Codex-native model selection derived from a provider-neutral tier."""

    model: str
    reasoning_effort: str = ""


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
                timeout=agent_auth_status_timeout(),
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


def direct_agent_model(agent: str, phase_env_var: str, *, codex_default: str = "") -> str:
    """Return a model override appropriate for a direct-runner provider."""
    if is_pi(agent):
        return os.environ.get(PI_MODEL_ENV, "")
    return os.environ.get(phase_env_var, codex_default)


def agent_cli_name(agent: str) -> str:
    """Return the executable name for a supported agent backend."""
    if agent not in AGENT_CAPABILITIES:
        raise ValueError(f"Unsupported agent: {agent}")
    return agent


def agent_display_name(agent: str) -> str:
    """Return a short human-facing name for a supported agent backend."""
    names = {
        "claude": "Claude Code",
        "codex": "Codex",
        "pi": "Pi",
    }
    try:
        return names[agent]
    except KeyError as e:
        raise ValueError(f"Unsupported agent: {agent}") from e


def pi_private_redaction_tokens(cwd: Path, model: str = "") -> tuple[str, ...]:
    """Return local Pi values that must be redacted from publishable diagnostics."""
    tokens: list[str] = []
    resolved_model = (model or os.environ.get(PI_MODEL_ENV, "")).strip()
    if resolved_model:
        tokens.append(resolved_model)

    resolved_cwd = cwd.resolve()
    for parent in (resolved_cwd, *resolved_cwd.parents):
        denylist = parent / PI_PRIVATE_DENYLIST_FILENAME
        if not denylist.is_file():
            continue
        try:
            lines = denylist.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            break
        for line in lines:
            token = line.strip()
            if token and not token.startswith("#"):
                tokens.append(token)
        break

    return tuple(dict.fromkeys(tokens))


def redact_pi_private_values(text: str, tokens: Iterable[str]) -> str:
    """Replace local Pi aliases, endpoints, and model identifiers in text."""
    redacted = text
    for token in sorted((token for token in tokens if token), key=len, reverse=True):
        redacted = redacted.replace(token, PI_PRIVATE_REDACTION)
    return redacted


def _redact_pi_command_args(cmd: SubprocessCommand, tokens: Iterable[str]) -> SubprocessCommand:
    """Redact Pi private values from a subprocess command payload."""
    if isinstance(cmd, str):
        return redact_pi_private_values(cmd, tokens)
    if isinstance(cmd, Sequence) and not isinstance(cmd, bytes):
        return [
            redact_pi_private_values(part, tokens) if isinstance(part, str) else part
            for part in cmd
        ]
    return cmd


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
    # A NUL in the prompt would make subprocess.run raise ``ValueError: embedded
    # null byte`` while marshaling text stdin, before the child runs (#1661). The
    # prompt is assembled from untrusted multi-source text; strip defensively.
    return subprocess.run(
        cmd,
        input=strip_null_bytes(prompt),
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
            timeout=CODEX_HELP_PROBE_SECONDS,
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


def _codex_model_config(model: str, *, use_default: bool = False) -> CodexModelConfig:
    """Translate Claude tier IDs into Codex-native model/reasoning settings."""
    normalized = model.strip()
    if not normalized:
        if use_default:
            return CodexModelConfig(CODEX_DEFAULT_MODEL, CODEX_DEFAULT_REASONING_EFFORT)
        return CodexModelConfig("")
    lower_model = normalized.lower()
    if lower_model == "fable" or lower_model.startswith("claude-fable-"):
        return CodexModelConfig(CODEX_FABLE_MODEL, CODEX_FABLE_REASONING_EFFORT)
    if lower_model == "opus" or lower_model.startswith("claude-opus-"):
        return CodexModelConfig(CODEX_OPUS_MODEL, CODEX_OPUS_REASONING_EFFORT)
    if lower_model == "sonnet" or lower_model.startswith("claude-sonnet-"):
        return CodexModelConfig(CODEX_SONNET_MODEL, CODEX_SONNET_REASONING_EFFORT)
    if lower_model == "haiku" or lower_model.startswith("claude-haiku-"):
        return CodexModelConfig(CODEX_HAIKU_MODEL)
    return CodexModelConfig(normalized)


def _codex_model_args(model: str, *, use_default: bool = False) -> list[str]:
    """Return Codex CLI arguments for the requested model tier."""
    model_config = _codex_model_config(model, use_default=use_default)
    args: list[str] = []
    if model_config.model:
        args.extend(["--model", model_config.model])
    if model_config.reasoning_effort:
        args.extend(
            [
                "-c",
                f"model_reasoning_effort={json.dumps(model_config.reasoning_effort)}",
            ]
        )
    return args


def _is_relative_to(path: Path, parent: Path) -> bool:
    """Return whether ``path`` is inside ``parent`` without requiring Python 3.12 APIs."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _codex_extra_writable_dirs(cwd: Path, sandbox: str | None) -> list[Path]:
    """Return extra writable roots Codex needs for git worktree metadata."""
    if sandbox != "workspace-write":
        return []

    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_COMMON_DIR_PROBE_SECONDS,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []

    raw_common_dir = result.stdout.strip()
    if not raw_common_dir:
        return []

    common_dir = Path(raw_common_dir)
    if not common_dir.is_absolute():
        common_dir = cwd / common_dir
    common_dir = common_dir.resolve(strict=False)
    cwd_resolved = cwd.resolve(strict=False)
    if _is_relative_to(common_dir, cwd_resolved):
        return []
    return [common_dir]


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
    cmd.extend(_codex_model_args(model, use_default=resume_id is None))
    if resume_id is None:
        if cwd is None:
            raise ValueError("cwd is required for new Codex exec sessions")
        cmd.extend(["--cd", str(cwd)])
        if sandbox is not None:
            cmd.extend(["--sandbox", sandbox])
        for writable_dir in _codex_extra_writable_dirs(cwd, sandbox):
            cmd.extend(["--add-dir", str(writable_dir)])
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
        for key in CODEX_PARENT_CONTEXT_ENV_VARS:
            env.pop(key, None)
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


def _pi_base_cmd(*, session_id: str | None = None) -> list[str]:
    """Build a Pi JSON-mode command without alias values in argv."""
    cmd = ["pi", "--mode", "json"]
    if session_id:
        cmd.extend(["--session", session_id])
    return cmd


def _model_from_pi_cmd(cmd: list[str]) -> str:
    """Extract the Pi model value from a command list when present."""
    try:
        model_index = cmd.index("--model")
    except ValueError:
        return ""
    if model_index + 1 >= len(cmd):
        return ""
    return cmd[model_index + 1]


def _pi_sandbox_args(sandbox: str) -> list[str]:
    """Return Pi tool restrictions for the requested sandbox mode."""
    if sandbox == "read-only":
        return ["--tools", PI_READ_ONLY_TOOLS]
    if sandbox in {"workspace-write", "danger-full-access"}:
        return []
    raise ValueError(f"Unsupported Pi sandbox mode: {sandbox}")


def _pi_env(*, model: str = "") -> dict[str, str]:
    """Return a privacy-biased environment for Pi subprocesses."""
    env = os.environ.copy()
    if model:
        env[PI_MODEL_ENV] = model
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
    model: str = "",
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
        try:
            return subprocess.run(
                cmd,
                cwd=cwd,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=_pi_env(model=model),
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            tokens = pi_private_redaction_tokens(cwd, _model_from_pi_cmd(cmd))
            redacted_cmd = _redact_pi_command_args(exc.cmd, tokens)
            raise subprocess.CalledProcessError(
                exc.returncode,
                redacted_cmd,
                output=redact_pi_private_values(exc.stdout or "", tokens),
                stderr=redact_pi_private_values(exc.stderr or "", tokens),
            ) from exc
        except subprocess.TimeoutExpired as exc:
            tokens = pi_private_redaction_tokens(cwd, _model_from_pi_cmd(cmd))
            raise subprocess.TimeoutExpired(
                _redact_pi_command_args(exc.cmd, tokens),
                exc.timeout,
                output=redact_pi_private_values(_coerce_timeout_output(exc.output), tokens),
                stderr=redact_pi_private_values(_coerce_timeout_output(exc.stderr), tokens),
            ) from exc
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
    cmd = _pi_base_cmd()
    result = _run_pi_command(
        cmd,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        sandbox=sandbox,
        model=model,
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
    cmd = _pi_base_cmd(session_id=session_id)
    result = _run_pi_command(
        cmd,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        sandbox="workspace-write",
        model=model,
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
    # Strip NUL bytes: proc.communicate(input=...) marshals text stdin and would
    # raise ``ValueError: embedded null byte`` on a stray NUL, before Codex runs
    # (#1661) — the same crash the Claude path guards against.
    input_text: str | None = strip_null_bytes(prompt)
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
        stdout_text, stderr_text = proc.communicate(timeout=CODEX_TERMINATION_GRACE_SECONDS)
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
    cmd.extend(_codex_model_args(model))
    return cmd


def agent_json_stdout(text: str, session_id: str | None = None) -> str:
    """Wrap direct-agent text output in the JSON shape expected by Claude callers."""
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
