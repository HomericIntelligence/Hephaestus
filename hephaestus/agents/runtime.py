"""Shared process helpers for agent-driven CLIs."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import inspect
import json
import logging
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from hephaestus.agents.codex_isolation import (
    CodexIsolationAdapterV1,
    CodexIsolationError,
    CodexIsolationPreparedV1,
    CodexIsolationRequestV1,
    CodexIsolationResultV1,
    _CodexPrepareCleanupError,
    validate_adapter,
    validate_prepared,
    validate_result_evidence,
)
from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionPolicy,
    ExecutionPolicyError,
    ExecutionRequest,
    SessionLifecycle,
    resolve_policy,
)
from hephaestus.agents.model_selection import (
    PI_THINKING_LEVELS,
    AgentModelSelection,
    parse_model_selection,
    validate_claude_model_reference,
    validate_codex_role_model_reference,
)
from hephaestus.agents.pi_plugins import (
    PiPreflightResult,
    package_tree_digest,
    preflight_pi_environment,
    prove_athena_skill_command,
)
from hephaestus.agents.pi_session import (
    AgentSessionBinding,
    PiSessionBindingError,
    create_pi_binding,
    validate_pi_binding,
)
from hephaestus.config.child_environments import (
    build_claude_child_env,
    build_codex_child_env,
    build_pi_child_env,
    read_approved_parent_env,
)
from hephaestus.constants import (
    agent_auth_status_timeout,
)
from hephaestus.io.utils import write_secure
from hephaestus.utils.helpers import strip_null_bytes

LOG = logging.getLogger(__name__)

AgentName = Literal["claude", "codex", "pi", "opencode"]
ProcessTracker = Callable[[int], contextlib.AbstractContextManager[None]]
SubprocessCommandPart = str | bytes | os.PathLike[str] | os.PathLike[bytes]
SubprocessCommand = SubprocessCommandPart | Sequence[SubprocessCommandPart]
AGENT_CHOICES: tuple[AgentName, ...] = ("claude", "codex", "pi", "opencode")
DEFAULT_AGENT: AgentName = "claude"
CODEX_HELP_PROBE_SECONDS = 10
GIT_COMMON_DIR_PROBE_SECONDS = 5
CODEX_TERMINATION_GRACE_SECONDS = 5
CODEX_FINAL_MESSAGE_GRACE_SECONDS = 5.0
CODEX_PARENT_CONTEXT_ENV_VARS = ("CODEX_THREAD_ID",)
CODEX_ATHENA_MARKETPLACE_SOURCE = "https://github.com/HomericIntelligence/Athena.git"
CODEX_ATHENA_MARKETPLACE_REF = "5df1b2f9fd8037fe0655edb36a37e0189eaab8c9"
CODEX_ATHENA_VERSION = "0.5.1"
CODEX_ATHENA_ARTIFACT_SHA256 = "7fbfb710a8da2c36e276276c1ff2d33ee40fe8f2365348c06c40696a9dfe0af1"
CODEX_ATHENA_CACHE_RELATIVE_PATH = Path("plugins") / "cache" / "athena" / "athena"
CODEX_AUTH_MAX_BYTES = 1024 * 1024
CODEX_ATHENA_MAX_BYTES = 32 * 1024 * 1024
CODEX_PRESERVED_STATE_MAX_FILES = 100_000
CODEX_PRESERVED_STATE_MAX_BYTES = 2 * 1024 * 1024 * 1024
PI_ISOLATION_ADAPTER_ENTRY_POINT_GROUP = "hephaestus.pi_isolation_adapters"
PI_MODEL_CONFIG_RELATIVE_PATH = Path(".pi") / "agent" / "models.json"
PI_SETTINGS_CONFIG_RELATIVE_PATH = Path(".pi") / "agent" / "settings.json"
PI_CONFIG_MAX_BYTES = 1024 * 1024
PI_PRIVATE_DENYLIST_FILENAME = ".heph-private-denylist"
PI_PROJECT_DENYLIST_FILENAME = ".heph-project-denylist"
PI_DENYLIST_FILENAMES = (PI_PROJECT_DENYLIST_FILENAME, PI_PRIVATE_DENYLIST_FILENAME)
PI_PRIVATE_REDACTION = "<redacted-pi-private-value>"
PI_SMOKE_LOG_DIR_PREFIX = "pi-smoke-"
PI_RUNTIME_TEMP_ROOT_NAME = "hephaestus-pi-runtime"
_PI_INTERNAL_ADMISSION_TOKEN = object()
PI_READ_ONLY_TOOLS = "read,grep,find,ls"
PI_SMOKE_BASE_ARGS: tuple[str, ...] = (
    "--mode",
    "json",
    "--print",
    "--no-session",
    "--no-approve",
    "--no-context-files",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--offline",
)
PI_AUTOMATION_PREFLIGHT_ERROR = (
    "Pi automation preflight is unavailable. Run "
    "`hephaestus-install-pi-plugins --dry-run --json` to inspect the required setup."
)
AGENT_AUTH_STATUS_COMMANDS: dict[AgentName, tuple[tuple[str, ...], ...]] = {
    "claude": (("claude", "auth", "status"),),
    "codex": (("codex", "login", "status"),),
    "pi": (("pi", "--version"),),
    # Exit 0 only proves the CLI runs. OpenCode serves models from stored
    # credentials OR environment keys, so `providers list` legitimately reports
    # "0 credentials" on fully working setups; credential-count parsing would
    # false-negative those. Deeper authentication is verified by the run itself.
    "opencode": (("opencode", "providers", "list"),),
}


@dataclass(frozen=True)
class PiAliasConfig:
    """Private operator aliases used only by the tool-free Pi smoke sentinel."""

    provider: str
    model: str


def _validate_pi_alias_metadata(metadata: os.stat_result) -> None:
    """Validate one snapshot of the private alias file's security metadata."""
    if stat.S_ISLNK(metadata.st_mode):
        raise OSError("Pi alias config must be a regular file, not a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("Pi alias config must be a regular file")
    if not hasattr(os, "getuid"):
        raise OSError("Pi alias config ownership checks are unavailable on this platform")
    if metadata.st_uid != os.getuid():
        raise OSError("Pi alias config must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise OSError("Pi alias config must have mode 0600")


def load_pi_alias_config(path: Path) -> PiAliasConfig:
    """Load an exact two-key Pi alias TOML file through a race-safe descriptor."""
    try:
        initial = path.lstat()
    except OSError as exc:
        raise OSError("Unable to inspect Pi alias config") from exc
    _validate_pi_alias_metadata(initial)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OSError("Unable to open Pi alias config without following symlinks") from exc
    try:
        opened = os.fstat(descriptor)
        _validate_pi_alias_metadata(opened)
        if (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("Pi alias config changed while being opened")
        with os.fdopen(descriptor, "rb", closefd=False) as config_file:
            try:
                payload: Any = tomllib.load(config_file)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError("Pi alias config is not valid TOML") from exc
    finally:
        os.close(descriptor)

    if not isinstance(payload, dict) or set(payload) != {"provider", "model"}:
        raise ValueError("Pi alias config must contain exactly provider and model")
    provider = payload["provider"]
    model = payload["model"]
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("Pi alias config provider must be a nonblank string")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Pi alias config model must be a nonblank string")
    return PiAliasConfig(provider=provider.strip(), model=model.strip())


def _platform_child_env() -> dict[str, str]:
    """Return the approved platform environment used by direct providers."""
    return read_approved_parent_env()


def _codex_child_env() -> dict[str, str]:
    """Return the named allowlisted environment for Codex."""
    return build_codex_child_env()


@dataclass(frozen=True)
class AgentRunResult:
    """Text output plus optional provider session id."""

    stdout: str
    stderr: str
    session_id: str | None = None
    session_binding: AgentSessionBinding | None = None
    observed_skill_invocations: tuple[str, ...] = ()


class AgentExecutionError(RuntimeError):
    """An agent CLI reported a fatal provider, sandbox, or tool failure."""


class _CodexReasoningEffortRejectedError(AgentExecutionError):
    """Codex rejected the reasoning effort before it emitted output or work."""


class PiAutomationDisabledError(AgentExecutionError):
    """The operator disabled Pi automation before any provider process started."""


class PiIsolationUnavailableError(AgentExecutionError):
    """No verified external OS-isolation adapter is available for Pi."""


class PiIsolationAdapter(Protocol):
    """A host-provided adapter that enforces a Pi execution policy externally.

    Pi's native tool allowlist is model-visible only.  An implementation of
    this protocol must enforce the filesystem mount and network relay named by
    ``policy`` before it starts the provider process.  It must also enter the
    supplied ``process_tracker`` around each live provider child when the host
    supplies one, so queue shutdown can terminate the child's process group.
    The provider must be launched with ``command`` and ``environment`` exactly
    as supplied; inheriting the adapter process environment would reintroduce
    ambient credentials outside the reviewed Pi profile. Broker-owned secrets
    may be injected from the adapter's own credential store, but never copied
    from ambient variables. The adapter returns trusted skill-call events in
    ``AgentRunResult.observed_skill_invocations``.
    """

    def invoke(
        self,
        *,
        policy: ExecutionPolicy,
        command: list[str],
        environment: dict[str, str],
        prompt: str,
        cwd: Path,
        timeout: int,
        model: str,
        session_id: str | None,
        process_tracker: ProcessTracker | None,
    ) -> AgentRunResult:
        """Start Pi with external constraints and host-owned process tracking."""
        raise NotImplementedError


def agent_compaction_resume(
    agent: str,
    *,
    session_agent: str,
    session_id: str | None,
    session_binding: AgentSessionBinding | None,
    execution_request: ExecutionRequest | None,
) -> tuple[str, dict[str, Any]] | None:
    """Prepare a neutral resume id and provider-only compaction arguments."""
    if is_pi(agent):
        if session_binding is None:
            return None
        request = execution_request or ExecutionRequest(
            _pi_role_for_session_agent(session_agent),
            AgentOperation.COMPACT,
            SessionLifecycle.RESUME_REQUIRED,
        )
        return session_binding.session_id, {
            "execution_request": request,
            "resume_binding": session_binding,
        }
    if not session_id:
        return None
    return session_id, {}


def _pi_role_for_session_agent(session_agent: str) -> AgentRole:
    """Map pipeline session names to their policy role inside the adapter."""
    if session_agent == "planner":
        return AgentRole.PLANNER
    if session_agent == "plan-reviewer":
        return AgentRole.PLAN_REVIEWER
    if session_agent == "pr-reviewer":
        return AgentRole.PR_REVIEWER
    return AgentRole.IMPLEMENTER


# Pi does not provide an operating-system sandbox.  No adapter is bundled with
# Hephaestus, so Pi automation is explicitly N/A in a stock installation.
# A host integration must register a reviewed adapter before selecting Pi; the
# runtime never falls back to its model-visible ``--tools`` flags.
_PI_ISOLATION_ADAPTER: PiIsolationAdapter | None = None


def register_pi_isolation_adapter(adapter: PiIsolationAdapter) -> None:
    """Register the host-owned Pi isolation broker for this process.

    The host is responsible for verifying that ``adapter`` enforces every
    filesystem and network grant before it starts Pi.  This explicit seam
    keeps the base package Pi N/A without a deployed broker and lets an
    integration opt in without exposing an unscoped provider runner.
    """
    global _PI_ISOLATION_ADAPTER
    if not _supports_pi_isolation_adapter_invoke_contract(adapter):
        raise PiIsolationUnavailableError(
            "Pi isolation adapter does not implement invoke() with the required keyword contract"
        )
    _PI_ISOLATION_ADAPTER = adapter


def _supports_pi_isolation_adapter_invoke_contract(adapter: object) -> bool:
    """Return whether ``adapter.invoke`` accepts the runtime's keyword call."""
    try:
        invoke = getattr(adapter, "invoke", None)
        if not callable(invoke):
            return False
        inspect.signature(invoke).bind(
            policy=object(),
            command=[],
            environment={},
            prompt="",
            cwd=Path("."),
            timeout=0,
            model="",
            session_id=None,
            process_tracker=None,
        )
    except Exception:
        return False
    return True


def load_pi_isolation_adapter(adapter_name: str | None) -> None:
    """Load one explicitly selected host adapter for a fresh CLI process."""
    adapter_name = (adapter_name or "").strip()
    if not adapter_name:
        return
    try:
        matches = tuple(
            entry_points(
                group=PI_ISOLATION_ADAPTER_ENTRY_POINT_GROUP,
                name=adapter_name,
            )
        )
    except Exception:
        raise PiIsolationUnavailableError(
            f"Pi isolation adapter {adapter_name!r} could not be discovered"
        ) from None
    if len(matches) != 1:
        raise PiIsolationUnavailableError(
            f"Pi isolation adapter {adapter_name!r} is not installed exactly once in "
            f"entry-point group {PI_ISOLATION_ADAPTER_ENTRY_POINT_GROUP!r}"
        )
    try:
        factory = matches[0].load()
        adapter = factory()
    except Exception:
        raise PiIsolationUnavailableError(
            f"Pi isolation adapter {adapter_name!r} could not be initialized"
        ) from None
    if not _supports_pi_isolation_adapter_invoke_contract(adapter):
        raise PiIsolationUnavailableError(
            f"Pi isolation adapter {adapter_name!r} does not implement invoke() with the "
            "required keyword contract"
        )
    register_pi_isolation_adapter(adapter)


def _require_pi_isolation_adapter(adapter_name: str | None = None) -> None:
    """Fail at provider selection when this installation has no Pi broker."""
    if _PI_ISOLATION_ADAPTER is None:
        load_pi_isolation_adapter(adapter_name)
    if _PI_ISOLATION_ADAPTER is None:
        raise PiIsolationUnavailableError(
            "Pi automation is N/A: this installation has no registered host "
            "OS-isolation adapter. Select Claude or Codex; Pi remains limited "
            "to the tool-free operator smoke command."
        )


class AgentCapability(StrEnum):
    """Provider capability names used by the provider-neutral parity contract."""

    FILE_READ = "file-read"
    FILE_WRITE = "file-write"
    SHELL = "shell"
    SEARCH = "search"
    SESSION = "session"
    RESUME = "resume"
    SKILL = "skill"
    TOOL_ALLOWLIST = "tool-allowlist"
    SUBAGENT = "subagent"
    WEB_ACCESS = "web-access"
    INTERACTIVE_APPROVAL = "interactive-approval"
    OS_SANDBOX = "os-sandbox"


@dataclass(frozen=True)
class AgentCapabilities:
    """Backend capabilities used by provider-neutral call sites.

    ``core_capabilities`` are provided by the provider's base CLI.
    ``package_capabilities`` require an explicit, separately verified package.
    ``unavailable_capabilities`` must fail closed rather than being inferred from
    a similarly named provider feature. The Pi entries form the executable
    companion to ADR-0019; later bootstrap and pipeline stages consume this
    distinction instead of creating stage-specific provider forks.
    """

    direct_runner: bool
    supports_approval: bool
    supports_sandbox: bool
    supports_sessions: bool
    core_capabilities: frozenset[AgentCapability] = frozenset()
    package_capabilities: frozenset[AgentCapability] = frozenset()
    unavailable_capabilities: frozenset[AgentCapability] = frozenset()


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
        supports_sandbox=False,
        supports_sessions=True,
        core_capabilities=frozenset(
            {
                AgentCapability.FILE_READ,
                AgentCapability.FILE_WRITE,
                AgentCapability.SHELL,
                AgentCapability.SEARCH,
                AgentCapability.SESSION,
                AgentCapability.RESUME,
                AgentCapability.SKILL,
                AgentCapability.TOOL_ALLOWLIST,
            }
        ),
        package_capabilities=frozenset(
            {
                AgentCapability.SUBAGENT,
                AgentCapability.WEB_ACCESS,
            }
        ),
        unavailable_capabilities=frozenset(
            {
                AgentCapability.INTERACTIVE_APPROVAL,
                AgentCapability.OS_SANDBOX,
            }
        ),
    ),
    "opencode": AgentCapabilities(
        direct_runner=True,
        supports_approval=False,
        supports_sandbox=True,
        supports_sessions=True,
    ),
}


@dataclass(frozen=True)
class CodexModelConfig:
    """Literal Codex model selection with an optional effort."""

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
    parser.add_argument(
        "--disable-pi-automation",
        action="store_true",
        help="Reject Pi automation before preflight or provider execution",
    )
    parser.add_argument(
        "--auth-status-timeout",
        type=_positive_timeout,
        default=agent_auth_status_timeout(),
        metavar="SECONDS",
        help="Positive timeout for provider authentication probes (default: 10)",
    )
    parser.add_argument(
        "--pi-isolation-adapter",
        default=None,
        metavar="ENTRY_POINT",
        help="Explicit registered Pi OS-isolation adapter entry point",
    )
    parser.add_argument(
        "--pi-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Explicit Pi coding-agent configuration directory",
    )
    parser.add_argument(
        "--codex-isolation-adapter",
        type=_codex_adapter_name,
        default=None,
        metavar="NAME",
        help="Exact external Codex implementation-isolation entry point",
    )
    parser.add_argument(
        "--codex-isolation-deployment-lock",
        type=_absolute_cli_path,
        default=None,
        metavar="PATH",
        help="Absolute detached Codex adapter deployment-lock path",
    )
    parser.add_argument(
        "--codex-isolation-deployment-lock-sha256",
        type=_lowercase_sha256,
        default=None,
        metavar="SHA256",
        help="Expected SHA-256 digest for the detached deployment lock",
    )


def _codex_adapter_name(value: str) -> str:
    """Parse one public entry-point name without command syntax."""
    if (
        not value
        or any(character.isspace() for character in value)
        or any(token in value for token in ("/", "\\", ":", ";"))
    ):
        raise argparse.ArgumentTypeError("Codex isolation adapter name is invalid")
    return value


def _absolute_cli_path(value: str) -> Path:
    """Parse one lexical absolute path without file-system access."""
    path = Path(value)
    if not path.is_absolute() or "\x00" in value:
        raise argparse.ArgumentTypeError("Codex deployment-lock path must be absolute")
    return path


def _lowercase_sha256(value: str) -> str:
    """Parse one lowercase SHA-256 value."""
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError("Codex deployment-lock digest must be lowercase SHA-256")
    return value


def _positive_timeout(value: str) -> int:
    """Parse a strictly positive integer timeout at the CLI boundary."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive integer")
    return parsed


def is_agent_authenticated(
    agent: AgentName,
    *,
    auth_status_timeout: int | None = None,
    pi_dir: Path | None = None,
) -> bool:
    """Return True when the provider CLI is installed and reports logged-in auth."""
    if shutil.which(agent) is None:
        return False

    for cmd in AGENT_AUTH_STATUS_COMMANDS[agent]:
        child_env = {
            "claude": build_claude_child_env,
            "codex": build_codex_child_env,
            "pi": lambda: build_pi_child_env(pi_dir=pi_dir),
            # OpenCode serves models from stored credentials or environment
            # keys, so the approved platform environment is sufficient here.
            "opencode": _platform_child_env,
        }[agent]()
        try:
            result = subprocess.run(
                list(cmd),
                text=True,
                capture_output=True,
                timeout=(
                    agent_auth_status_timeout()
                    if auth_status_timeout is None
                    else auth_status_timeout
                ),
                check=False,
                env=child_env,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            if agent == "pi":
                return _pi_models_configured(pi_dir)
            return True
    return False


def _pi_models_configured(pi_dir: Path | None = None) -> bool:
    """Return True when Pi has at least one local model alias configured."""
    config_path = (
        pi_dir.expanduser() / "models.json"
        if pi_dir is not None
        else Path.home() / PI_MODEL_CONFIG_RELATIVE_PATH
    )
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


def _validate_pi_settings_metadata(metadata: os.stat_result) -> None:
    """Validate one snapshot of the trusted global Pi settings file."""
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError
    if metadata.st_size > PI_CONFIG_MAX_BYTES:
        raise OSError
    if not hasattr(os, "getuid") or metadata.st_uid != os.getuid():
        raise OSError
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise OSError


def _read_pi_settings_payload(config_path: Path, *, required: bool) -> Any | None:
    """Read one bounded regular Pi settings file without following a link."""
    failure: AgentExecutionError | None = None
    payload: Any = None
    try:
        initial = config_path.lstat()
        if stat.S_ISLNK(initial.st_mode):
            raise OSError
        _validate_pi_settings_metadata(initial)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(config_path, flags)
        try:
            opened = os.fstat(descriptor)
            _validate_pi_settings_metadata(opened)
            if (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError
            with os.fdopen(descriptor, "rb", closefd=False) as settings_file:
                raw_settings = settings_file.read(PI_CONFIG_MAX_BYTES + 1)
            if len(raw_settings) > PI_CONFIG_MAX_BYTES:
                raise OSError
            payload = json.loads(raw_settings)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        if not required:
            return None
        failure = AgentExecutionError("Pi default model configuration is unavailable or invalid")
    except (OSError, UnicodeError, json.JSONDecodeError):
        failure = AgentExecutionError("Pi default model configuration is unavailable or invalid")
    if failure is not None:
        raise failure
    if not isinstance(payload, dict):
        raise AgentExecutionError("Pi default model configuration must be a JSON object")
    return payload


def _load_pi_default_model_selection(
    pi_dir: Path | None = None,
    *,
    required: bool = True,
    explicit_model: str = "",
) -> AgentModelSelection | None:
    """Load the operator-global Pi default through a bounded regular file."""
    config_path = (
        pi_dir.expanduser() / "settings.json"
        if pi_dir is not None
        else Path.home() / PI_SETTINGS_CONFIG_RELATIVE_PATH
    )
    payload = _read_pi_settings_payload(config_path, required=required)
    if payload is None:
        return None

    provider = payload.get("defaultProvider")
    model = payload.get("defaultModel")
    thinking = payload.get("defaultThinkingLevel", "")
    if not isinstance(thinking, str) or thinking not in PI_THINKING_LEVELS | {""}:
        raise AgentExecutionError(
            "Pi default model configuration has an invalid defaultThinkingLevel"
        )
    if explicit_model:
        return AgentModelSelection(explicit_model, thinking)
    if (
        not isinstance(provider, str)
        or not provider
        or provider.strip() != provider
        or not isinstance(model, str)
        or not model
        or model.strip() != model
    ):
        raise AgentExecutionError(
            "Pi default model configuration requires defaultProvider and defaultModel"
        )
    return AgentModelSelection(f"{provider}/{model}", thinking)


def _resolve_pi_model_selection(
    model: str,
    *,
    pi_dir: Path | None = None,
) -> AgentModelSelection:
    """Return the explicit Pi model and thinking selection."""
    selection = parse_model_selection(model)
    if selection.model and selection.reasoning_effort not in {"", "default"}:
        return selection
    configured = _load_pi_default_model_selection(
        pi_dir, required=not selection.model, explicit_model=selection.model
    )
    if configured is None:
        return AgentModelSelection(selection.model)
    selected_model = selection.model or configured.model
    selected_effort = (
        configured.reasoning_effort
        if selection.reasoning_effort in {"", "default"}
        else selection.reasoning_effort
    )
    return AgentModelSelection(selected_model, selected_effort)


def resolve_pi_model_reference(model: str, *, pi_dir: Path | None = None) -> str:
    """Return the Pi model reference used for a private session fingerprint."""
    return _resolve_pi_model_selection(model, pi_dir=pi_dir).reference


def _require_pi_automation_admission(
    cwd: Path,
    *,
    disable_pi_automation: bool = False,
    pi_dir: Path | None = None,
) -> PiPreflightResult:
    """Return verified Pi admission, honoring the emergency stop before probing."""
    if disable_pi_automation:
        raise PiAutomationDisabledError(
            "Pi automation disabled by CLI policy; no Pi or broker process was started"
        )
    result = preflight_pi_environment(cwd, pi_dir=pi_dir)
    if not result.ready:
        raise AgentExecutionError(f"{PI_AUTOMATION_PREFLIGHT_ERROR} {result.remediation_message()}")
    return result


def _validate_pi_model_references_before_admission(
    model_references: Sequence[str] | None,
    *,
    disable_pi_automation: bool,
    pi_dir: Path | None,
) -> None:
    """Validate pending Pi selections before a process-backed admission check."""
    if disable_pi_automation:
        raise PiAutomationDisabledError(
            "Pi automation disabled by CLI policy; no Pi or broker process was started"
        )
    if model_references is None:
        return
    if not model_references:
        raise ValueError("Pi model references must not be empty")
    for reference in model_references:
        _resolve_pi_model_selection(reference, pi_dir=pi_dir)


def _validate_codex_model_references(model_references: Sequence[str] | None) -> None:
    """Validate Codex role references before provider authentication."""
    if model_references is None:
        return
    for reference in model_references:
        validate_codex_role_model_reference(reference)


def _validate_claude_model_references(model_references: Sequence[str] | None) -> None:
    """Validate Claude references before provider authentication."""
    if model_references is None:
        return
    for reference in model_references:
        validate_claude_model_reference(reference)


def _validate_fixed_provider_model_references(
    agent: str,
    model_references: Sequence[str] | None,
) -> None:
    """Validate references when the selected provider is known."""
    if agent == "claude":
        _validate_claude_model_references(model_references)
    elif agent == "codex":
        _validate_codex_model_references(model_references)


def validate_durable_model_selection(
    provider: str,
    model: str,
    selection_format: object,
) -> None:
    """Validate one durable provider, model, and selection-format identity."""
    if provider not in AGENT_CHOICES:
        raise ValueError("invalid durable provider model selection")
    if not isinstance(model, str) or model != model.strip():
        raise ValueError("invalid durable provider model selection")
    if type(selection_format) is not int or selection_format != 1:
        raise ValueError("invalid durable provider model selection")
    parse_model_selection(model)


def resolve_agent(
    agent: str | None,
    *,
    cwd: Path | None = None,
    disable_pi_automation: bool = False,
    auth_status_timeout: int | None = None,
    pi_isolation_adapter: str | None = None,
    pi_dir: Path | None = None,
    model_references: Sequence[str] | None = None,
) -> AgentName:
    """Resolve an optional provider selection into a concrete backend.

    When Pi is explicit, ``model_references`` binds all pending executions to
    their model selections before admission or authentication starts a child
    process. An empty reference resolves the trusted operator-global default.
    """
    effective_cwd = Path.cwd() if cwd is None else cwd
    if agent is not None:
        if agent not in AGENT_CHOICES:
            raise ValueError(f"Unsupported agent: {agent}")
        _validate_fixed_provider_model_references(agent, model_references)
        if agent == "pi":
            _validate_pi_model_references_before_admission(
                model_references,
                disable_pi_automation=disable_pi_automation,
                pi_dir=pi_dir,
            )
            _require_pi_automation_admission(
                effective_cwd,
                pi_dir=pi_dir,
            )
            _require_pi_isolation_adapter(pi_isolation_adapter)
        authenticated = is_agent_authenticated(
            agent,
            auth_status_timeout=auth_status_timeout,
            pi_dir=pi_dir if agent == "pi" else None,
        )
        if not authenticated:
            if shutil.which(agent) is None:
                raise RuntimeError(
                    f"Agent '{agent}' is not installed on PATH. "
                    f"Install the '{agent}' CLI and try again, "
                    f"or omit --agent to auto-detect an authenticated backend."
                )
            status_hint = (
                "`pi --version` and check ~/.pi/agent/models.json"
                if agent == "pi"
                else "`opencode providers login` (environment keys also work)"
                if agent == "opencode"
                else f"`{agent} auth status` (or `{agent} login status`)"
            )
            raise RuntimeError(
                f"Agent '{agent}' is installed but not authenticated. "
                f"Run {status_hint} before running automation."
            )
        return agent

    installed_agents = tuple(
        agent_name
        for agent_name in AGENT_CHOICES
        if agent_name != "pi" and shutil.which(agent_name)
    )
    if not installed_agents:
        if shutil.which("pi") is not None:
            _require_pi_automation_admission(
                effective_cwd,
                disable_pi_automation=disable_pi_automation,
                pi_dir=pi_dir,
            )
        raise RuntimeError(
            "No supported agent backend found on PATH. Install `claude`, `codex`, `pi`, "
            "or `opencode`, or pass --agent after installing the selected backend."
        )

    for agent_name in installed_agents:
        if is_agent_authenticated(agent_name, auth_status_timeout=auth_status_timeout):
            _validate_fixed_provider_model_references(agent_name, model_references)
            return agent_name

    raise RuntimeError(
        "Supported agent backends are installed but none are authenticated. "
        "Run `claude auth status`, `codex login status`, `pi --version`, or "
        "`opencode providers list`, then log in/configure the provider you want "
        "automation to use."
    )


def is_codex(agent: str) -> bool:
    """Return True when the selected provider is Codex."""
    return agent == "codex"


def requires_codex_implementation_isolation(agent: str) -> bool:
    """Return true when Codex implementation needs its publication scope checks.

    External adapter selection is optional. A selected adapter must satisfy
    the full isolation contract.
    """
    return is_codex(agent)


def is_pi(agent: str) -> bool:
    """Return True when the selected provider is Pi."""
    return agent == "pi"


def is_opencode(agent: str) -> bool:
    """Return True when the selected provider is OpenCode."""
    return agent == "opencode"


def reject_pi_unsupported_surface(agent: str, reason: str) -> None:
    """Fail before a legacy surface can run Pi outside its scoped policy.

    Args:
        agent: Selected provider name.
        reason: Operator-facing N/A remediation that describes the supported
            queue or wrapper to use instead.

    """
    if is_pi(agent):
        raise AgentExecutionError(f"Pi is not supported by this surface: {reason}")


def agent_uses_configured_model_default(agent: str) -> bool:
    """Return whether the provider owns its model default."""
    return agent in AGENT_CHOICES


def normalize_provider_model_reference(agent: str, reference: str) -> str:
    """Validate the tool and preserve the literal model reference."""
    agent_cli_name(agent)
    return parse_model_selection(reference).reference


def uses_direct_agent_runner(agent: str) -> bool:
    """Return True when the provider is invoked through runtime text/session helpers."""
    if agent not in AGENT_CAPABILITIES:
        return False
    return AGENT_CAPABILITIES[agent].direct_runner


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
        "opencode": "OpenCode",
    }
    try:
        return names[agent]
    except KeyError as e:
        raise ValueError(f"Unsupported agent: {agent}") from e


def _resolve_pi_denylist_root(root: Path, *, require_readable: bool) -> Path | None:
    """Resolve a denylist search root, optionally failing closed on an error."""
    try:
        return root.resolve()
    except OSError as exc:
        if require_readable:
            raise OSError("Unable to resolve Pi private denylist root") from exc
        return None


def _read_pi_private_denylist(
    denylist: Path,
    *,
    require_readable: bool,
) -> tuple[str, ...] | None:
    """Read one Pi privacy-policy file, returning ``None`` when it is absent."""
    try:
        denylist.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        if require_readable:
            raise OSError("Unable to inspect Pi private denylist") from exc
        return ()
    if not denylist.is_file():
        if require_readable:
            raise OSError("Pi private denylist is not a regular file")
        return ()
    try:
        lines = denylist.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        if require_readable:
            raise OSError("Unable to read Pi private denylist") from exc
        return ()
    return tuple(token for line in lines if (token := line.strip()) and not token.startswith("#"))


def _pi_private_log_permissions_supported() -> bool:
    """Return whether this platform can verify private smoke-artifact ACLs."""
    return os.name == "posix" and (sys.platform == "darwin" or sys.platform.startswith("linux"))


def _run_pi_private_acl_command(command: list[str]) -> str:
    """Run a platform ACL command without accepting caller-controlled input."""
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=True,
            env=_platform_child_env(),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise OSError("Unable to verify Pi smoke artifact ACLs") from exc
    return result.stdout


_LINUX_POSIX_ACL_FILESYSTEMS = frozenset(
    {
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "overlay",
        "overlayfs",
        "ramfs",
        "tmpfs",
        "virtiofs",
        "xfs",
    }
)


def _decode_linux_mountinfo_path(value: str) -> str:
    """Decode the octal path escapes used by Linux ``mountinfo`` records."""
    decoded: list[str] = []
    index = 0
    while index < len(value):
        candidate = value[index + 1 : index + 4]
        if (
            value[index] == "\\"
            and len(candidate) == 3
            and all("0" <= character <= "7" for character in candidate)
        ):
            decoded.append(chr(int(candidate, 8)))
            index += 4
            continue
        decoded.append(value[index])
        index += 1
    return "".join(decoded)


def _linux_pi_private_filesystem_type(path: Path) -> str:
    """Return the filesystem type containing ``path`` from ``/proc/self/mountinfo``."""
    absolute_path = Path(os.path.abspath(path))
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError("Unable to determine Pi smoke artifact filesystem") from exc

    selected: tuple[int, str] | None = None
    for line in mountinfo.splitlines():
        before_separator, separator, after_separator = line.partition(" - ")
        fields = before_separator.split()
        filesystem_fields = after_separator.split()
        if not separator or len(fields) < 5 or not filesystem_fields:
            continue
        mount_path = Path(_decode_linux_mountinfo_path(fields[4]))
        try:
            absolute_path.relative_to(mount_path)
        except ValueError:
            continue
        candidate = (len(mount_path.parts), filesystem_fields[0])
        if selected is None or candidate[0] > selected[0]:
            selected = candidate
    if selected is None:
        raise OSError("Unable to determine Pi smoke artifact filesystem")
    return selected[1]


def _verify_pi_private_acl(path: Path, *, clear: bool) -> None:
    """Clear or reject ACL grants that would make a smoke artifact non-private."""
    if not _pi_private_log_permissions_supported():
        raise OSError("Pi smoke requires verifiable private artifact permissions")
    if sys.platform == "darwin":
        if clear:
            _run_pi_private_acl_command(["/bin/chmod", "-N", str(path)])
        acl_listing = _run_pi_private_acl_command(["/bin/ls", "-lde", str(path)])
        if len(acl_listing.splitlines()) != 1:
            raise OSError("Pi smoke artifact path has an access ACL")
        return

    filesystem_type = _linux_pi_private_filesystem_type(path)
    if filesystem_type not in _LINUX_POSIX_ACL_FILESYSTEMS:
        raise OSError("Pi smoke requires a local filesystem with verifiable POSIX ACLs")

    absent_errors = {
        errno.ENODATA,
        errno.ENOTSUP,
        getattr(errno, "ENOATTR", errno.ENODATA),
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
    for attribute in ("system.posix_acl_access", "system.posix_acl_default"):
        if clear:
            try:
                os.removexattr(path, attribute, follow_symlinks=False)
            except OSError as exc:
                if exc.errno not in absent_errors:
                    raise OSError("Unable to clear Pi smoke artifact ACLs") from exc
        try:
            os.getxattr(path, attribute, follow_symlinks=False)
        except OSError as exc:
            if exc.errno in absent_errors:
                continue
            raise OSError("Unable to verify Pi smoke artifact ACLs") from exc
        raise OSError("Pi smoke artifact path has an access ACL")


def _absolute_pi_log_path(path: Path) -> Path:
    """Return an absolute, lexical Pi log path without resolving symlinks."""
    return Path(os.path.abspath(path))


def _pi_log_path_components(path: Path) -> tuple[Path, ...]:
    """Return every lexical component from an absolute path's filesystem root."""
    root = Path(path.anchor)
    components = [root]
    current = root
    for part in path.parts[1:]:
        current /= part
        components.append(current)
    return tuple(components)


def _pi_group_has_other_users(group_id: int, current_uid: int) -> bool:
    """Return whether a writable group includes an account other than the caller."""
    try:
        import grp
        import pwd

        group = grp.getgrgid(group_id)
        passwd_entries = pwd.getpwall()
    except (ImportError, KeyError, OSError):
        return True

    current_names = {entry.pw_name for entry in passwd_entries if entry.pw_uid == current_uid}
    group_names = set(group.gr_mem)
    group_names.update(entry.pw_name for entry in passwd_entries if entry.pw_gid == group_id)
    return not current_names or bool(group_names - current_names)


def _pi_uid_is_mapped(uid: int) -> bool:
    """Return whether a Linux namespace can act as ``uid``; fail closed elsewhere."""
    if sys.platform != "linux":
        return True
    try:
        uid_map = Path("/proc/self/uid_map").read_text(encoding="utf-8")
    except OSError:
        return True

    valid_mapping = False
    for line in uid_map.splitlines():
        fields = line.split()
        if len(fields) != 3:
            return True
        try:
            inside_uid, _, length = (int(field) for field in fields)
        except ValueError:
            return True
        valid_mapping = True
        if inside_uid <= uid < inside_uid + length:
            return True
    return not valid_mapping


def _verify_pi_private_log_directory(
    path: Path,
    *,
    require_current_owner: bool,
    require_owner_only: bool,
    clear_acl: bool,
    verify_acl: bool = True,
) -> None:
    """Verify one no-symlink directory in the private artifact path chain."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OSError("Unable to inspect Pi smoke artifact path") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError("Pi smoke artifact path must be a directory, not a symlink")
    current_uid = os.getuid()
    if require_current_owner and metadata.st_uid != current_uid:
        raise OSError("Pi smoke artifact path is not owned by the current user")
    if (
        not require_current_owner
        and metadata.st_uid not in {0, current_uid}
        and _pi_uid_is_mapped(metadata.st_uid)
    ):
        raise OSError("Pi smoke artifact ancestor is not owner-controlled")
    mode = stat.S_IMODE(metadata.st_mode)
    # A sticky directory cannot have another user's entry renamed or removed.
    # Combined with atomic child creation and ownership verification below, it
    # is safe as an ancestor (for example, the system temporary root).
    writable_by_other = bool(mode & stat.S_IWOTH)
    writable_by_group_peer = bool(mode & stat.S_IWGRP) and _pi_group_has_other_users(
        metadata.st_gid, current_uid
    )
    if (writable_by_other or writable_by_group_peer) and not (metadata.st_mode & stat.S_ISVTX):
        raise OSError("Pi smoke artifact path is writable by another user")
    if clear_acl and verify_acl:
        _verify_pi_private_acl(path, clear=True)
    if clear_acl:
        path.chmod(0o700)
        metadata = path.lstat()
    if verify_acl:
        _verify_pi_private_acl(path, clear=False)
    if require_owner_only and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise OSError("Pi smoke artifact path is not user-only")


def _ensure_pi_private_log_root(log_dir: Path) -> Path:
    """Create or verify an owner-controlled root for smoke artifact run dirs."""
    absolute_root = _absolute_pi_log_path(log_dir)
    components = _pi_log_path_components(absolute_root)
    for index, component in enumerate(components):
        is_root = index == len(components) - 1
        try:
            component.lstat()
        except FileNotFoundError:
            try:
                os.mkdir(component, 0o700)
            except OSError as exc:
                raise OSError("Unable to create Pi smoke artifact directory") from exc
            _verify_pi_private_log_directory(
                component,
                require_current_owner=True,
                require_owner_only=True,
                clear_acl=True,
            )
            continue
        _verify_pi_private_log_directory(
            component,
            require_current_owner=is_root,
            require_owner_only=is_root,
            clear_acl=is_root,
            verify_acl=is_root,
        )
    return absolute_root


def prepare_pi_private_log_dir(log_dir: Path) -> Path:
    """Create a unique ACL-verified private directory for one Pi smoke run."""
    root = _ensure_pi_private_log_root(log_dir)
    run_dir = Path(tempfile.mkdtemp(prefix=PI_SMOKE_LOG_DIR_PREFIX, dir=root))
    try:
        _verify_pi_private_log_directory(
            run_dir,
            require_current_owner=True,
            require_owner_only=True,
            clear_acl=True,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            run_dir.rmdir()
        raise
    return run_dir


def _prepare_pi_private_temp_dir() -> Path:
    """Create the isolated owner-only temporary directory used by Pi itself."""
    try:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    except OSError as exc:
        raise OSError("Unable to resolve Pi runtime temporary root") from exc
    return prepare_pi_private_log_dir(temp_root / PI_RUNTIME_TEMP_ROOT_NAME)


def _verify_pi_private_prompt_file(path: Path) -> None:
    """Verify the prompt file remains a private regular file before Pi reads it."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OSError("Unable to inspect Pi smoke prompt file") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("Pi smoke prompt file must be a regular file, not a symlink")
    if metadata.st_uid != os.getuid():
        raise OSError("Pi smoke prompt file is not owned by the current user")
    _verify_pi_private_acl(path, clear=True)
    path.chmod(0o600)
    _verify_pi_private_acl(path, clear=False)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OSError("Unable to inspect Pi smoke prompt file") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("Pi smoke prompt file must be a regular file, not a symlink")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise OSError("Pi smoke prompt file is not user-only")


def pi_private_redaction_tokens(
    cwd: Path,
    model: str = "",
    *,
    provider: str = "",
    additional_roots: Iterable[Path] = (),
    require_readable: bool = False,
) -> tuple[str, ...]:
    """Return local Pi values that must be redacted from publishable diagnostics.

    ``additional_roots`` lets an entry point protect the checkout-level local
    denylist even when it deliberately invokes Pi from another directory.  A
    caller that will publish diagnostics can set ``require_readable`` to fail
    closed instead of running without a configured local privacy policy.
    """
    tokens = [
        value
        for candidate in (
            model,
            provider,
        )
        if (value := candidate.strip())
    ]
    seen_denylists: set[Path] = set()
    for root in (cwd, *additional_roots):
        resolved_root = _resolve_pi_denylist_root(root, require_readable=require_readable)
        if resolved_root is None:
            continue
        for parent in (resolved_root, *resolved_root.parents):
            found_policy = False
            for filename in PI_DENYLIST_FILENAMES:
                denylist = parent / filename
                if denylist in seen_denylists:
                    continue
                seen_denylists.add(denylist)
                denylist_tokens = _read_pi_private_denylist(
                    denylist,
                    require_readable=require_readable,
                )
                if denylist_tokens is None:
                    continue
                tokens.extend(denylist_tokens)
                found_policy = True
            if found_policy:
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


def session_agent_matches(session_agent: object, selected_agent: str) -> bool:
    """Return whether explicit session metadata matches a supported provider."""
    return (
        isinstance(session_agent, str)
        and session_agent in AGENT_CHOICES
        and selected_agent in AGENT_CHOICES
        and session_agent == selected_agent
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
            env=build_codex_child_env(),
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
    """Split the literal model and free-form effort for Codex."""
    selection = parse_model_selection(model)
    effort = selection.reasoning_effort
    return CodexModelConfig(selection.model, "" if effort == "default" else effort)


def _codex_model_args(model: str, *, use_default: bool = False) -> list[str]:
    """Return Codex CLI arguments for the literal model and effort."""
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
            env=build_codex_child_env(),
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
    # Path.is_relative_to is stdlib since 3.9; the project floor is 3.13.
    if common_dir.is_relative_to(cwd_resolved):
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
    execution_request: ExecutionRequest | None = None,
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
    primary_review = (
        execution_request is not None
        and execution_request.role is AgentRole.PR_REVIEWER
        and execution_request.operation is AgentOperation.PR_REVIEW
        and sandbox == "read-only"
        and approval == "never"
    )
    if primary_review:
        # Use a fresh namespace to avoid merging a fixed ambient profile.
        profile_name = "hephaestus-review-" + secrets.token_hex(16)
        cmd.extend(
            [
                "--strict-config",
                "-c",
                f'permissions.{profile_name}={{extends=":read-only",network={{enabled=true}}}}',
                "-c",
                f"default_permissions={json.dumps(profile_name)}",
                "-c",
                'approval_policy="never"',
            ]
        )
        if resume_id is None:
            if cwd is None:
                raise ValueError("cwd is required for new Codex exec sessions")
            cmd.extend(["--cd", str(cwd)])
    elif resume_id is not None:
        # ``codex exec resume`` does not accept the new-session --sandbox or
        # --ask-for-approval flags.  Its generic config overrides are the
        # enforceable equivalent, and must not inherit a permissive user
        # profile when a pipeline review resumes.
        if sandbox is not None:
            cmd.extend(["-c", f"sandbox_mode={json.dumps(sandbox)}"])
        cmd.extend(["-c", f"approval_policy={json.dumps(approval)}"])
    else:
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


_CODEX_NESTED_SANDBOX_MARKER = "sandbox_apply: Operation not permitted"
_CODEX_NESTED_SANDBOX_DIAGNOSTIC = (
    "codex_nested_sandbox_unsupported: Codex could not initialize its child "
    "sandbox (sandbox_apply: Operation not permitted). Run the outer Hephaestus "
    "automation loop outside the enclosing API sandbox; the child sandbox "
    "permissions were not broadened."
)
_CODEX_FAILED_TOOL_STATUSES = frozenset({"failed", "declined"})
_CODEX_APP_SERVER_STREAM_LAG_PREFIX = "in-process app-server event stream lagged; dropped "
_CODEX_APP_SERVER_STREAM_LAG_SUFFIX = " events"
_CODEX_SKILLS_BUDGET_PREFIX = "Skill descriptions were shortened to fit the "
_CODEX_SKILLS_BUDGET_PLAIN_NOTICE = (
    "skills context budget. Codex can still see every skill, but some descriptions are shorter. "
    "Disable unused skills or plugins to leave more room for the rest."
)
_CODEX_SKILLS_BUDGET_MARKER = "% skills context budget."
_CODEX_REASONING_EFFORT_PARAMS = frozenset({"reasoning.effort", "reasoning_effort"})
_CODEX_REASONING_EFFORT_CODES = frozenset(
    {"invalid_enum_value", "unsupported_parameter", "unsupported_value"}
)
_CODEX_REASONING_EFFORT_PARAM_TAGS = (
    "[ReasoningEffortParam]",
    "[reasoning.effort]",
)
_CODEX_REASONING_EFFORT_CODE_TAGS = tuple(f"[{code}]" for code in _CODEX_REASONING_EFFORT_CODES)
_CODEX_PRE_WORK_EVENT_TYPES = frozenset({"session_meta", "thread.started", "turn.started"})
_CODEX_ERROR_SCAN_MAX_VALUES = 4_096


class _CodexErrorScanLimitError(ValueError):
    """A Codex error value exceeds the bounded diagnostic scan."""


def _codex_json_objects(text: str) -> Iterable[dict[str, Any]]:
    """Yield JSON objects from Codex JSONL while ignoring non-object lines."""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event: Any = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            yield event


def _codex_json_object_stream_is_well_formed(text: str) -> bool:
    """Return whether each nonblank JSONL line is one object."""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event: Any = json.loads(line)
        except (ValueError, RecursionError):
            return False
        if not isinstance(event, dict):
            return False
    return True


def _codex_error_message(value: object) -> str | None:
    """Extract a short message from a structured Codex failure payload."""
    stack = [value]
    scanned = 0
    seen_containers: set[int] = set()
    while stack:
        scanned += 1
        if scanned > _CODEX_ERROR_SCAN_MAX_VALUES:
            return None
        current = stack.pop()
        if isinstance(current, str):
            text = current.strip()
            if text:
                return text
            continue
        if not isinstance(current, dict):
            continue
        identity = id(current)
        if identity in seen_containers:
            continue
        seen_containers.add(identity)
        stack.extend(current.get(key) for key in reversed(("message", "error", "detail", "reason")))
    return None


def _codex_nested_error_values(value: object) -> Iterator[dict[str, Any] | str]:
    """Yield error objects and decode JSON objects embedded in messages."""
    decoder = json.JSONDecoder()
    stack = [value]
    scanned = 0
    seen_containers: set[int] = set()
    while stack:
        scanned += 1
        if scanned > _CODEX_ERROR_SCAN_MAX_VALUES:
            raise _CodexErrorScanLimitError
        current = stack.pop()
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            yield current
            stack.extend(reversed(tuple(current.values())))
            continue
        if isinstance(current, list):
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            stack.extend(reversed(current))
            continue
        if not isinstance(current, str):
            continue
        yield current
        decoded_values: list[object] = []
        cursor = 0
        while (start := current.find("{", cursor)) >= 0:
            scanned += 1
            if scanned > _CODEX_ERROR_SCAN_MAX_VALUES:
                raise _CodexErrorScanLimitError
            try:
                nested, length = decoder.raw_decode(current[start:])
            except (ValueError, RecursionError):
                cursor = start + 1
                continue
            decoded_values.append(nested)
            cursor = start + max(length, 1)
        stack.extend(reversed(decoded_values))


def _codex_reasoning_effort_rejection(event: dict[str, Any]) -> str | None:
    """Return a diagnostic for one exact unsupported-effort error shape."""
    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type not in ("error", "turn.failed"):
        return None
    try:
        values = tuple(_codex_nested_error_values(event))
    except _CodexErrorScanLimitError:
        return None
    mappings = tuple(value for value in values if isinstance(value, dict))
    messages = tuple(value for value in values if isinstance(value, str))
    has_status_400 = any(
        mapping.get(key) == 400 or mapping.get(key) == "400"
        for mapping in mappings
        for key in ("status", "status_code", "http_status")
    ) or any(
        "status 400" in message.casefold() or "400 bad request" in message.casefold()
        for message in messages
    )
    structured_rejection = False
    for mapping in mappings:
        parameter = mapping.get("param")
        code = mapping.get("code")
        if (
            mapping.get("type") == "invalid_request_error"
            and isinstance(parameter, str)
            and parameter in _CODEX_REASONING_EFFORT_PARAMS
            and isinstance(code, str)
            and code in _CODEX_REASONING_EFFORT_CODES
        ):
            structured_rejection = True
            break
    if structured_rejection:
        return _codex_structured_failure(event) or "Codex rejected the reasoning effort"
    if not has_status_400:
        return None
    tagged_rejection = any(
        all(tag in message for tag in _CODEX_REASONING_EFFORT_PARAM_TAGS)
        and any(tag in message for tag in _CODEX_REASONING_EFFORT_CODE_TAGS)
        for message in messages
    ) and any(mapping.get("type") == "invalid_request_error" for mapping in mappings)
    if not tagged_rejection:
        return None
    return _codex_structured_failure(event) or "Codex rejected the reasoning effort"


def _codex_reasoning_effort_failure(stdout: str) -> str | None:
    """Find a classified reasoning-effort rejection in Codex stdout JSONL."""
    if not _codex_json_object_stream_is_well_formed(stdout):
        return None
    events = tuple(_codex_json_objects(stdout))
    diagnostic = next(
        (
            result
            for event in events
            if (result := _codex_reasoning_effort_rejection(event)) is not None
        ),
        None,
    )
    if diagnostic is None:
        return None
    if any(
        _codex_reasoning_effort_rejection(event) is None
        and (
            not isinstance(event.get("type"), str)
            or event.get("type") not in _CODEX_PRE_WORK_EVENT_TYPES
        )
        for event in events
    ):
        return None
    return diagnostic


def _is_codex_app_server_stream_lag(message: str) -> bool:
    """Return whether *message* is Codex's nonfatal app-server lag notice."""
    if not (
        message.startswith(_CODEX_APP_SERVER_STREAM_LAG_PREFIX)
        and message.endswith(_CODEX_APP_SERVER_STREAM_LAG_SUFFIX)
    ):
        return False
    dropped_count = message[
        len(_CODEX_APP_SERVER_STREAM_LAG_PREFIX) : -len(_CODEX_APP_SERVER_STREAM_LAG_SUFFIX)
    ]
    return dropped_count.isascii() and dropped_count.isdigit()


def _is_codex_nonfatal_error_item(message: str) -> bool:
    """Return whether Codex encodes a known informational notice as an error item."""
    if _is_codex_app_server_stream_lag(message):
        return True
    if not message.startswith(_CODEX_SKILLS_BUDGET_PREFIX):
        return False
    remainder = message[len(_CODEX_SKILLS_BUDGET_PREFIX) :]
    if remainder == _CODEX_SKILLS_BUDGET_PLAIN_NOTICE:
        return True
    percentage, marker, _guidance = remainder.partition(_CODEX_SKILLS_BUDGET_MARKER)
    percentage_parts = percentage.split(".", maxsplit=1)
    return bool(
        marker and all(part and part.isascii() and part.isdigit() for part in percentage_parts)
    )


def _codex_structured_failure(event: dict[str, Any]) -> str | None:
    """Return a failure description for a fatal Codex JSONL event."""
    event_type = event.get("type")
    if event_type == "error":
        return _codex_error_message(event) or "unrecoverable Codex error"
    if event_type == "turn.failed":
        return _codex_error_message(event.get("error")) or "Codex turn failed"
    if event_type != "item.completed":
        return None

    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    status = item.get("status")
    if item_type == "command_execution":
        output = item.get("aggregated_output")
        if (
            status in _CODEX_FAILED_TOOL_STATUSES
            and isinstance(output, str)
            and _CODEX_NESTED_SANDBOX_MARKER.casefold() in output.casefold()
        ):
            return output.strip()
        return None
    if item_type == "error":
        message = _codex_error_message(item)
        if message is not None and _is_codex_nonfatal_error_item(message):
            return None
        return message or "Codex error item"
    if status in _CODEX_FAILED_TOOL_STATUSES:
        item_label = item_type if isinstance(item_type, str) else "item"
        return _codex_error_message(item) or f"{item_label} status={status}"
    return None


def _codex_failure_diagnostic(stdout: str, stderr: str) -> str | None:
    """Return a bounded fatal Codex diagnostic from structured failure channels."""
    marker = _CODEX_NESTED_SANDBOX_MARKER.casefold()
    if marker in stderr.casefold():
        return _CODEX_NESTED_SANDBOX_DIAGNOSTIC

    for event in _codex_json_objects(stdout):
        failure = _codex_structured_failure(event)
        if failure is None:
            continue
        if marker in failure.casefold():
            return _CODEX_NESTED_SANDBOX_DIAGNOSTIC
        return f"codex_tool_or_provider_failure: {failure[:300]}"
    return None


def _parse_codex_json_events(text: str) -> tuple[str | None, str]:
    """Extract session id and final text from Codex JSONL output."""
    session_id: str | None = None
    messages: list[str] = []
    for event in _codex_json_objects(text):
        if event.get("type") == "session_meta":
            payload = event.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                session_id = payload["id"]
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            session_id = event["thread_id"]
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
    if message.get("role") != "assistant":
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
    execution_request: ExecutionRequest | None = None,
    process_tracker: ProcessTracker | None = None,
    _final_message_grace_seconds: float | None = None,
) -> AgentRunResult:
    """Run a new persisted Codex exec session and capture its UUID."""
    return _run_codex_session_with_effort_fallback(
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        model=model,
        sandbox=sandbox,
        approval=approval,
        execution_request=execution_request,
        process_tracker=process_tracker,
        final_message_grace_seconds=_final_message_grace_seconds,
    )


@dataclass(slots=True)
class _CodexAdapterCall:
    """Hold one host-observed adapter call and its outcome."""

    started: threading.Event
    completed: threading.Event
    outcome: list[tuple[bool, object]]
    started_at: float = 0.0
    returned_at: float = 0.0


@dataclass(frozen=True, slots=True)
class _CodexTerminalReceipt:
    """Record host-observed final destruction after one adapter call."""

    call_returned_at: float
    destroy_started_at: float
    destroy_returned_at: float
    terminal_observed_at: float
    guest_boot_nonce: str


def _start_codex_adapter_call(call: Callable[[], object]) -> _CodexAdapterCall:
    """Start one adapter call on a host-controlled daemon thread."""
    state = _CodexAdapterCall(threading.Event(), threading.Event(), [])

    def run() -> None:
        state.started_at = time.monotonic()
        state.started.set()
        try:
            state.outcome.append((True, call()))
        except BaseException as exc:
            state.outcome.append((False, exc))
        finally:
            state.returned_at = time.monotonic()
            state.completed.set()

    threading.Thread(target=run, daemon=True, name="codex-adapter-call").start()
    return state


def _wait_for_codex_adapter_call(call: _CodexAdapterCall, deadline: float) -> bool:
    """Wait for one adapter call only until an absolute host deadline."""
    if not call.started.wait(max(0.0, deadline - time.monotonic())):
        return False
    return call.completed.wait(max(0.0, deadline - time.monotonic()))


def _codex_control_deadline(request: CodexIsolationRequestV1) -> float:
    """Return the bounded deadline for one adapter cleanup control."""
    cleanup_budget = (
        request.policy.term_grace_seconds
        + request.policy.kill_grace_seconds
        + request.policy.pipe_close_grace_seconds
        + 2 * request.policy.inventory_quiescence_seconds
    )
    return min(
        request.monotonic_deadline + cleanup_budget,
        time.monotonic() + cleanup_budget,
    )


def _destroy_codex_prepared(
    adapter: CodexIsolationAdapterV1,
    request: CodexIsolationRequestV1,
    prepared: CodexIsolationPreparedV1,
    *,
    call_returned_at: float,
) -> _CodexTerminalReceipt:
    """Destroy one guest and return typed host terminal evidence."""
    destroy_started_at = time.monotonic()
    destroy_call = _start_codex_adapter_call(lambda: adapter.destroy(prepared))
    if not _wait_for_codex_adapter_call(destroy_call, _codex_control_deadline(request)):
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    destroyed_ok, result_or_error = destroy_call.outcome[0]
    if not destroyed_ok or result_or_error is not None:
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    receipt = _CodexTerminalReceipt(
        call_returned_at=call_returned_at,
        destroy_started_at=destroy_started_at,
        destroy_returned_at=destroy_call.returned_at,
        terminal_observed_at=time.monotonic(),
        guest_boot_nonce=prepared.guest_boot_nonce,
    )
    if not (
        receipt.call_returned_at
        <= receipt.destroy_started_at
        <= receipt.destroy_returned_at
        <= receipt.terminal_observed_at
    ):
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    return receipt


_CODEX_OPERATION_TOOLS = {
    AgentOperation.IMPLEMENT_INSPECT: ("Glob", "Grep", "Read"),
    AgentOperation.IMPLEMENT: ("Bash", "Edit", "Glob", "Grep", "Read", "Write"),
    AgentOperation.TEST_FIX: ("Bash", "Edit", "Glob", "Grep", "Read", "Write"),
    AgentOperation.ADDRESS_REVIEW: ("Bash", "Edit", "Glob", "Grep", "Read", "Write"),
}


def _codex_session_authority(
    request: CodexIsolationRequestV1,
) -> tuple[str, str | None, str, tuple[str, ...]]:
    """Read the complete operation authority from one bound V1 session field."""
    try:
        value = json.loads(request.session)
    except (TypeError, json.JSONDecodeError):
        raise CodexIsolationError("codex_adapter_request_mismatch") from None
    if type(value) is not dict or set(value) != {
        "allowed_tools",
        "lifecycle",
        "operation",
        "session_id",
    }:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    allowed_tools = value["allowed_tools"]
    lifecycle = value["lifecycle"]
    operation = value["operation"]
    session_id = value["session_id"]
    if lifecycle not in {item.value for item in SessionLifecycle}:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    if session_id is not None and (type(session_id) is not str or not session_id):
        raise CodexIsolationError("codex_adapter_request_mismatch")
    if (
        type(operation) is not str
        or type(allowed_tools) is not list
        or not all(type(item) is str and item for item in allowed_tools)
        or allowed_tools != sorted(set(allowed_tools))
    ):
        raise CodexIsolationError("codex_adapter_request_mismatch")
    return (
        cast(str, lifecycle),
        session_id,
        operation,
        tuple(cast(list[str], allowed_tools)),
    )


def _codex_implementation_config_arguments(command: tuple[str, ...]) -> tuple[str, ...]:
    """Read configuration options without treating option values as switches."""
    index = 4 if command[2:3] == ("resume",) else 2
    configuration: list[str] = []
    while index < len(command):
        option = command[index]
        if option in {"-c", "--config", "--model", "--cd", "--sandbox"}:
            if index + 1 >= len(command):
                raise CodexIsolationError("codex_adapter_request_mismatch")
            if option in {"-c", "--config"}:
                configuration.append(command[index + 1])
            index += 2
        elif option in {"--json", "-"}:
            index += 1
        else:
            raise CodexIsolationError("codex_adapter_request_mismatch")
    return tuple(configuration)


def _validate_codex_session_authority(
    request: CodexIsolationRequestV1,
    execution_request: ExecutionRequest,
) -> str | None:
    """Require exact agreement between host authority and the frozen V1 request."""
    if execution_request.role is not AgentRole.IMPLEMENTER:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    try:
        resolve_policy(execution_request)
    except ExecutionPolicyError:
        raise CodexIsolationError("codex_adapter_request_mismatch") from None
    lifecycle, session_id, operation, allowed_tools = _codex_session_authority(request)
    expected_tools = _CODEX_OPERATION_TOOLS.get(execution_request.operation)
    rebase_tools = ("Edit", "Glob", "Grep", "Read", "Write")
    rebase_grant = (
        execution_request.operation is AgentOperation.IMPLEMENT and allowed_tools == rebase_tools
    )
    if (
        lifecycle != execution_request.lifecycle.value
        or operation != execution_request.operation.value
        or (allowed_tools != expected_tools and not rebase_grant)
    ):
        raise CodexIsolationError("codex_adapter_request_mismatch")
    operation_config = f"hephaestus_automation.operation={json.dumps(operation)}"
    tools_config = "hephaestus_automation.allowed_tools=" + json.dumps(
        list(allowed_tools), separators=(",", ":")
    )
    configuration = _codex_implementation_config_arguments(request.command)
    if configuration.count(operation_config) != 1 or configuration.count(tools_config) != 1:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    if request.command[1:2] != ("exec",):
        raise CodexIsolationError("codex_adapter_request_mismatch")
    resumes = request.command[2:3] == ("resume",)
    if execution_request.lifecycle is SessionLifecycle.RESUME_REQUIRED:
        if session_id is None or not resumes or request.command[3:4] != (session_id,):
            raise CodexIsolationError("codex_adapter_request_mismatch")
    elif session_id is not None or resumes:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    return session_id


def _complete_timed_out_codex_call(
    *,
    adapter: CodexIsolationAdapterV1,
    request: CodexIsolationRequestV1,
    active_call: _CodexAdapterCall,
    prepared: CodexIsolationPreparedV1 | None,
) -> _CodexTerminalReceipt | None:
    """Require a late call to finish and destroy each returned guest."""
    cleanup_deadline = _codex_control_deadline(request)
    if prepared is not None:
        destroy_started_at = time.monotonic()
        destroy_call = _start_codex_adapter_call(lambda: adapter.destroy(prepared))
        if not _wait_for_codex_adapter_call(active_call, cleanup_deadline):
            raise CodexIsolationError("codex_adapter_inventory_uncertain")
        if not _wait_for_codex_adapter_call(destroy_call, cleanup_deadline):
            raise CodexIsolationError("codex_adapter_inventory_uncertain")
        destroyed_ok, destroyed_value = destroy_call.outcome[0]
        if not destroyed_ok or destroyed_value is not None:
            raise CodexIsolationError("codex_adapter_inventory_uncertain")
        receipt = _CodexTerminalReceipt(
            call_returned_at=active_call.returned_at,
            destroy_started_at=destroy_started_at,
            destroy_returned_at=destroy_call.returned_at,
            terminal_observed_at=time.monotonic(),
            guest_boot_nonce=prepared.guest_boot_nonce,
        )
        if (
            receipt.destroy_started_at > receipt.destroy_returned_at
            or receipt.call_returned_at > receipt.terminal_observed_at
            or receipt.destroy_returned_at > receipt.terminal_observed_at
        ):
            raise CodexIsolationError("codex_adapter_inventory_uncertain")
        return receipt
    if not _wait_for_codex_adapter_call(active_call, cleanup_deadline):
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    call_ok, value = active_call.outcome[0]
    if not call_ok:
        _complete_opaque_prepare_cleanup(
            value,
            cleanup_deadline=cleanup_deadline,
        )
        return None
    late_prepared = cast(CodexIsolationPreparedV1, value)
    return _destroy_late_codex_prepare(
        adapter,
        request,
        late_prepared,
        call_returned_at=active_call.returned_at,
    )


def _complete_opaque_prepare_cleanup(
    value: object,
    *,
    cleanup_deadline: float,
) -> None:
    """Run one opaque cleanup action for an unpublishable prepare result."""
    if not isinstance(value, _CodexPrepareCleanupError):
        return None
    cleanup = value.claim_cleanup()
    if cleanup is None:
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    cleanup_call = _start_codex_adapter_call(cleanup)
    if not _wait_for_codex_adapter_call(cleanup_call, cleanup_deadline):
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    cleanup_ok, cleanup_value = cleanup_call.outcome[0]
    if not cleanup_ok or cleanup_value is not None:
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    raise CodexIsolationError("codex_adapter_inventory_uncertain")


def _destroy_late_codex_prepare(
    adapter: CodexIsolationAdapterV1,
    request: CodexIsolationRequestV1,
    late_prepared: CodexIsolationPreparedV1,
    *,
    call_returned_at: float,
) -> _CodexTerminalReceipt:
    """Destroy a late prepared guest before the host accepts its record."""
    validation_failed = False
    try:
        validate_prepared(request, late_prepared)
    except BaseException:
        validation_failed = True
    try:
        receipt = _destroy_codex_prepared(
            adapter,
            request,
            late_prepared,
            call_returned_at=call_returned_at,
        )
    except BaseException:
        raise CodexIsolationError("codex_adapter_inventory_uncertain") from None
    if validation_failed:
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    return receipt


def _validate_codex_result_window(
    result: CodexIsolationResultV1,
    invoke_call: _CodexAdapterCall,
) -> None:
    """Bind all final V1 cleanup evidence to this host invocation."""
    if (
        result.pipe_close_timestamp < invoke_call.started_at
        or result.pipe_close_timestamp > invoke_call.returned_at
        or any(
            item.monotonic_timestamp < result.pipe_close_timestamp
            or item.monotonic_timestamp > invoke_call.returned_at
            for item in result.inventories[-2:]
        )
    ):
        raise CodexIsolationError("codex_adapter_inventory_uncertain")


def _codex_authentication_path(profile: Path, run_nonce: str) -> Path:
    """Return the external transient authentication path for one profile."""
    return profile.parent / ".transient-auth" / run_nonce / "auth.json"


def _codex_profile_store(profile: Path) -> Path:
    """Return the host-only durable store for one ephemeral profile."""
    if (
        profile.name != "profile"
        or profile.parent.parent.name != ".runs"
        or len(profile.parent.name) != 64
    ):
        raise CodexIsolationError("codex_adapter_request_mismatch")
    return profile.parent.parent.parent


def _codex_rollout_store(profile: Path) -> Path:
    """Return the host-only durable rollout store for one profile identity."""
    return _codex_profile_store(profile) / "sessions"


def _validate_codex_rollout(payload: bytes, session_id: str, worktree: Path) -> None:
    """Validate the first metadata record of one bounded Codex rollout."""
    try:
        first = payload.splitlines()[0]
        record = json.loads(first)
        metadata = record["payload"]
        if (
            record.get("type") != "session_meta"
            or type(metadata) is not dict
            or metadata.get("id") != session_id
            or Path(metadata.get("cwd", "")) != worktree
        ):
            raise ValueError
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise CodexIsolationError("codex_adapter_result_invalid") from None


def _open_codex_rollout_directory(path: Path) -> int:
    """Open one owner-only rollout directory without following its final path."""
    descriptor = -1
    try:
        lexical = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o022
            or _codex_file_fingerprint(lexical) != _codex_file_fingerprint(opened)
        ):
            raise OSError("invalid rollout directory")
        return descriptor
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise CodexIsolationError("codex_adapter_result_invalid") from None


def _open_codex_rollout_child_directory(
    descriptor: int,
    name: str,
    *,
    create: bool,
) -> int:
    """Open one bound child directory and optionally create it."""
    if not name or name in {".", ".."} or "/" in name or os.sep in name:
        raise CodexIsolationError("codex_adapter_result_invalid")
    try:
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
            except FileExistsError:
                pass
        lexical = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        child = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=descriptor,
        )
        opened = os.fstat(child)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o022
            or _codex_file_fingerprint(lexical) != _codex_file_fingerprint(opened)
        ):
            raise OSError("invalid rollout directory")
        if create:
            os.fchmod(child, 0o700)
        return child
    except OSError:
        if "child" in locals():
            os.close(child)
        raise CodexIsolationError("codex_adapter_result_invalid") from None


def _read_codex_rollout_file(descriptor: int, name: str) -> bytes:
    """Read one bounded rollout through its held parent directory."""
    child = -1
    try:
        lexical = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        child = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=descriptor,
        )
        opened = os.fstat(child)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) & 0o077
            or opened.st_size > 64 * 1024 * 1024
            or _codex_file_fingerprint(lexical) != _codex_file_fingerprint(opened)
        ):
            raise OSError("invalid rollout file")
        payload = bytearray()
        while chunk := os.read(child, min(1024 * 1024, 64 * 1024 * 1024 + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > 64 * 1024 * 1024:
                raise OSError("rollout file is too large")
        final = os.fstat(child)
        path_final = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if _codex_file_fingerprint(final) != _codex_file_fingerprint(
            opened
        ) or _codex_file_fingerprint(path_final) != _codex_file_fingerprint(opened):
            raise OSError("rollout file changed")
        return bytes(payload)
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None
    finally:
        if child >= 0:
            os.close(child)


def _bounded_codex_rollout_entries(
    descriptor: int,
    budget: _CodexStateScanBudget,
) -> list[os.DirEntry[str]]:
    """Read no more rollout entries than the shared state budget permits."""
    remaining = CODEX_PRESERVED_STATE_MAX_FILES - budget.files
    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(descriptor) as iterator:
            for _index in range(remaining + 1):
                try:
                    entries.append(next(iterator))
                except StopIteration:
                    break
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None
    if len(entries) > remaining:
        raise CodexIsolationError("codex_adapter_result_invalid")
    return sorted(entries, key=lambda entry: entry.name)


def _collect_codex_rollout_candidates(
    descriptor: int,
    relative: Path,
    session_id: str,
    budget: _CodexStateScanBudget,
    candidates: list[tuple[Path, bytes]],
) -> None:
    """Collect bounded rollout candidates through one held directory tree."""
    for entry in _bounded_codex_rollout_entries(descriptor, budget):
        budget.files += 1
        child_relative = relative / entry.name
        try:
            status = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            raise CodexIsolationError("codex_adapter_result_invalid") from None
        if stat.S_ISDIR(status.st_mode):
            child = _open_codex_rollout_child_directory(descriptor, entry.name, create=False)
            try:
                _collect_codex_rollout_candidates(
                    child,
                    child_relative,
                    session_id,
                    budget,
                    candidates,
                )
            finally:
                os.close(child)
            continue
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) & 0o077
        ):
            raise CodexIsolationError("codex_adapter_result_invalid")
        if entry.name.endswith(".jsonl") and session_id in entry.name:
            candidates.append((child_relative, _read_codex_rollout_file(descriptor, entry.name)))


def _find_codex_rollout(root: Path, session_id: str) -> tuple[Path, bytes]:
    """Find one bound owner-only rollout and return its relative path."""
    root_descriptor = _open_codex_rollout_directory(root)
    candidates: list[tuple[Path, bytes]] = []
    try:
        _collect_codex_rollout_candidates(
            root_descriptor,
            Path(),
            session_id,
            _CodexStateScanBudget(),
            candidates,
        )
    finally:
        os.close(root_descriptor)
    if len(candidates) != 1:
        raise CodexIsolationError("codex_adapter_result_invalid")
    return candidates[0]


def _open_codex_rollout_parent(
    root_descriptor: int,
    relative: Path,
) -> int:
    """Create and open a relative rollout parent through held descriptors."""
    descriptor = os.dup(root_descriptor)
    try:
        for part in relative.parts:
            child = _open_codex_rollout_child_directory(descriptor, part, create=True)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_codex_rollout_file(
    root_descriptor: int,
    relative: Path,
    payload: bytes,
    *,
    replace: bool,
) -> None:
    """Write one rollout through a bound destination directory."""
    parent = _open_codex_rollout_parent(root_descriptor, relative.parent)
    temporary = f".{relative.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        if replace:
            try:
                existing = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (
                    not stat.S_ISREG(existing.st_mode)
                    or existing.st_uid != os.geteuid()
                    or existing.st_nlink != 1
                    or stat.S_IMODE(existing.st_mode) & 0o077
                ):
                    raise OSError("invalid rollout destination")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent,
        )
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if replace:
            os.replace(temporary, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
        else:
            os.link(
                temporary,
                relative.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


def _import_codex_rollout(
    profile: Path,
    session_id: str,
    worktree: Path,
) -> None:
    """Import only one validated rollout into one fresh profile."""
    relative, payload = _find_codex_rollout(_codex_rollout_store(profile), session_id)
    _validate_codex_rollout(payload, session_id, worktree)
    sessions = _open_codex_rollout_directory(profile / "sessions")
    try:
        _write_codex_rollout_file(sessions, relative, payload, replace=False)
    finally:
        os.close(sessions)


def _export_codex_rollout(
    profile: Path,
    session_id: str,
    worktree: Path,
) -> None:
    """Publish only one validated rollout after terminal state proof."""
    relative, payload = _find_codex_rollout(profile / "sessions", session_id)
    _validate_codex_rollout(payload, session_id, worktree)
    store = _codex_rollout_store(profile)
    store_descriptor = _open_codex_rollout_directory(store.parent)
    try:
        sessions = _open_codex_rollout_child_directory(
            store_descriptor,
            store.name,
            create=True,
        )
        try:
            _write_codex_rollout_file(sessions, relative, payload, replace=True)
        finally:
            os.close(sessions)
    finally:
        os.close(store_descriptor)


def _codex_active_receipt_path(profile: Path) -> Path:
    """Return the host-only active receipt for one issue-cycle profile."""
    return _codex_profile_store(profile) / ".active.json"


def _codex_active_receipt_payload(
    request: CodexIsolationRequestV1,
    *,
    status: str = "active",
) -> bytes:
    """Return the canonical non-secret active-request identity."""
    return json.dumps(
        {
            "issue": request.issue,
            "private_profile_path": request.private_profile_path,
            "repository": request.repository,
            "run_nonce": request.run_nonce,
            "session_identity_digest": request.session_identity_digest,
            "status": status,
            "worktree_path": request.worktree_path,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _create_codex_active_receipt(
    request: CodexIsolationRequestV1,
) -> tuple[Path, tuple[int, int, int, int, int, int]]:
    """Create one durable host-only receipt before adapter preparation."""
    path = _codex_active_receipt_path(Path(request.private_profile_path))
    payload = _codex_active_receipt_payload(request)
    parent_descriptor = -1
    descriptor = -1
    try:
        parent_status = path.parent.lstat()
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        opened_parent = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_status.st_mode)
            or parent_status.st_uid != os.geteuid()
            or stat.S_IMODE(parent_status.st_mode) != 0o700
            or (parent_status.st_dev, parent_status.st_ino)
            != (opened_parent.st_dev, opened_parent.st_ino)
        ):
            raise OSError("invalid Codex receipt store")
        try:
            os.stat(".quarantine.json", dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise CodexIsolationError("codex_adapter_inventory_uncertain")
        descriptor = os.open(
            ".active.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o400,
            dir_fd=parent_descriptor,
        )
        offset = 0
        while offset < len(payload):
            offset += os.writev(descriptor, [payload[offset:]])
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        identity = _codex_file_fingerprint(os.fstat(descriptor))
        try:
            os.stat(".quarantine.json", dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            os.unlink(".active.json", dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
            raise CodexIsolationError("codex_adapter_inventory_uncertain")
        os.fsync(parent_descriptor)
        return path, identity
    except CodexIsolationError:
        raise
    except OSError:
        raise CodexIsolationError("codex_adapter_inventory_uncertain") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _clear_codex_active_receipt(
    request: CodexIsolationRequestV1,
    path: Path,
    identity: tuple[int, int, int, int, int, int],
) -> None:
    """Clear the active receipt only after terminal state proof."""
    mode, payload = _read_codex_regular_file(path, max_bytes=16 * 1024)
    if (
        mode != 0o400
        or payload != _codex_active_receipt_payload(request)
        or _codex_file_fingerprint(path.lstat()) != identity
    ):
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    try:
        path.unlink()
        _fsync_codex_directory(path.parent)
        if path.exists() or path.is_symlink():
            raise OSError("active receipt remains")
    except OSError:
        raise CodexIsolationError("codex_adapter_inventory_uncertain") from None


def _quarantine_codex_active_receipt(
    request: CodexIsolationRequestV1,
    path: Path,
    identity: tuple[int, int, int, int, int, int],
) -> Path:
    """Replace a terminal run receipt with one cleanup-only quarantine receipt."""
    mode, payload = _read_codex_regular_file(path, max_bytes=16 * 1024)
    if (
        mode != 0o400
        or payload != _codex_active_receipt_payload(request)
        or _codex_file_fingerprint(path.lstat()) != identity
    ):
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    quarantine = path.with_name(".quarantine.json")
    descriptor = -1
    try:
        descriptor = os.open(
            quarantine,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o400,
        )
        quarantine_payload = _codex_active_receipt_payload(
            request,
            status="terminal-state-invalid",
        )
        offset = 0
        while offset < len(quarantine_payload):
            offset += os.write(descriptor, quarantine_payload[offset:])
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        path.unlink()
        _fsync_codex_directory(path.parent)
        if path.exists() or path.is_symlink() or not quarantine.is_file():
            raise OSError("quarantine receipt transition failed")
        return quarantine
    except OSError:
        raise CodexIsolationError("codex_adapter_inventory_uncertain") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _codex_profile_policy_paths(
    profile: Path,
    run_nonce: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the exact read-only and writable profile capability paths."""
    read_only = (
        str(profile / "config.toml"),
        str(profile / CODEX_ATHENA_CACHE_RELATIVE_PATH / CODEX_ATHENA_VERSION),
        str(_codex_authentication_path(profile, run_nonce)),
    )
    read_write = (str(profile),)
    return read_only, read_write


def _validate_codex_profile_policy(request: CodexIsolationRequestV1) -> Path:
    """Require the exact non-overlapping private profile policy."""
    profile = Path(request.private_profile_path)
    expected_read_only, expected_read_write = _codex_profile_policy_paths(
        profile,
        request.run_nonce,
    )
    actual_read_only = set(request.policy.read_only_mounts)
    actual_read_write = set(request.policy.read_write_mounts)
    protected_profile_paths = tuple(Path(path) for path in expected_read_only)
    if (
        not set(expected_read_only).issubset(actual_read_only)
        or not set(expected_read_write).issubset(actual_read_write)
        or set(expected_read_only) & actual_read_write
        or set(expected_read_write) & actual_read_only
    ):
        raise CodexIsolationError("codex_adapter_request_mismatch")
    for writable in map(Path, actual_read_write):
        if any(
            (
                writable == protected
                or writable in protected.parents
                or protected in writable.parents
            )
            and not (writable == profile and protected.is_relative_to(profile))
            for protected in protected_profile_paths
        ):
            raise CodexIsolationError("codex_adapter_request_mismatch")
    profile_write_paths = {path for path in actual_read_write if Path(path).is_relative_to(profile)}
    if profile_write_paths != set(expected_read_write):
        raise CodexIsolationError("codex_adapter_request_mismatch")
    return _codex_authentication_path(profile, request.run_nonce)


def _run_admitted_codex_implementation_session(  # noqa: C901
    *,
    adapter: CodexIsolationAdapterV1,
    request: CodexIsolationRequestV1,
    execution_request: ExecutionRequest,
    executable_descriptor: int,
    auth_source: Path | None = None,
    terminal_reaper: Callable[[], None] | None = None,
) -> AgentRunResult:
    """Run one automation-admitted Codex implementation request."""
    validate_adapter(adapter)
    expected_session_id = _validate_codex_session_authority(request, execution_request)
    expected_auth_path = _validate_codex_profile_policy(request)
    if time.monotonic() >= request.monotonic_deadline:
        raise CodexIsolationError("codex_adapter_timeout")
    _verify_codex_implementation_executable(request, executable_descriptor)
    active_receipt_path, active_receipt_identity = _create_codex_active_receipt(request)
    prepare_call = _start_codex_adapter_call(lambda: adapter.prepare(request))
    if not _wait_for_codex_adapter_call(prepare_call, request.monotonic_deadline):
        _complete_timed_out_codex_call(
            adapter=adapter,
            request=request,
            active_call=prepare_call,
            prepared=None,
        )
        _clear_codex_active_receipt(request, active_receipt_path, active_receipt_identity)
        raise CodexIsolationError("codex_adapter_timeout")
    prepared_ok, prepared_or_error = prepare_call.outcome[0]
    if not prepared_ok:
        if isinstance(prepared_or_error, _CodexPrepareCleanupError):
            _complete_timed_out_codex_call(
                adapter=adapter,
                request=request,
                active_call=prepare_call,
                prepared=None,
            )
            _clear_codex_active_receipt(request, active_receipt_path, active_receipt_identity)
        else:
            _clear_codex_active_receipt(request, active_receipt_path, active_receipt_identity)
        if isinstance(prepared_or_error, CodexIsolationError):
            raise prepared_or_error
        raise CodexIsolationError("codex_adapter_launch_failed") from None
    prepared = cast(CodexIsolationPreparedV1, prepared_or_error)
    destroy_started = False
    terminal_proved = False
    destroy_failure: BaseException | None = None
    auth_path: Path | None = None
    auth_bridge: _CodexAuthenticationBridge | None = None
    authentication = ""
    provider_session_id: str | None = None
    profile: Path | None = None
    protected_snapshot: tuple[
        tuple[str, tuple[int, int, int, int, int, int, int, int], str | None], ...
    ] = ()
    preserved_baseline: _CodexPreservedBaseline | None = None
    pre_auth_cleanup_failure: BaseException | None = None
    try:
        validate_prepared(request, prepared)
        if time.monotonic() >= min(request.monotonic_deadline, prepared.preparation_deadline):
            raise CodexIsolationError("codex_adapter_timeout")
        _verify_codex_implementation_executable(request, executable_descriptor)
        transient_auth_root = Path(request.private_profile_path).parent / ".transient-auth"
        if transient_auth_root.exists() or transient_auth_root.is_symlink():
            _discard_codex_profile(transient_auth_root)
        profile = _populate_codex_implementation_profile(request)
        if expected_session_id is not None:
            _import_codex_rollout(profile, expected_session_id, Path(request.worktree_path))
        _validate_codex_profile_inventory(profile)
        protected_snapshot = _codex_protected_profile_snapshot(profile)
        preserved_baseline = _capture_codex_preserved_state(request)
        source = auth_source or Path(_codex_child_env()["CODEX_HOME"]) / "auth.json"
        auth_path = expected_auth_path
        try:
            authentication = _read_codex_authentication(source)
            auth_bridge = _create_codex_authentication_bridge(auth_path, authentication)
        except CodexIsolationError:
            try:
                transient_root = auth_path.parents[1]
                if transient_root.exists() or transient_root.is_symlink():
                    _discard_codex_profile(transient_root)
            except BaseException as exc:
                pre_auth_cleanup_failure = exc
            raise
        except BaseException:
            raise CodexIsolationError("codex_adapter_initialization_failed") from None
        if time.monotonic() >= min(request.monotonic_deadline, prepared.preparation_deadline):
            raise CodexIsolationError("codex_adapter_timeout")
        _verify_codex_implementation_executable(request, executable_descriptor)
        invoke_call = _start_codex_adapter_call(lambda: adapter.invoke(prepared, str(auth_path)))
        if not _wait_for_codex_adapter_call(invoke_call, request.monotonic_deadline):
            destroy_started = True
            try:
                _complete_timed_out_codex_call(
                    adapter=adapter,
                    request=request,
                    active_call=invoke_call,
                    prepared=prepared,
                )
                terminal_proved = True
            except BaseException as exc:
                destroy_failure = exc
                raise
            raise CodexIsolationError("codex_adapter_timeout")
        invoked_ok, result_or_error = invoke_call.outcome[0]
        if not invoked_ok:
            raise CodexIsolationError("codex_adapter_launch_failed") from None
        result = cast(CodexIsolationResultV1, result_or_error)
        validate_result_evidence(request, prepared, result)
        _validate_codex_result_window(result, invoke_call)
        _validate_codex_authentication_output(result.output, authentication, auth_path)
        _verify_codex_implementation_executable(request, executable_descriptor)
        destroy_started = True
        try:
            _destroy_codex_prepared(
                adapter,
                request,
                prepared,
                call_returned_at=invoke_call.returned_at,
            )
            terminal_proved = True
        except BaseException as exc:
            destroy_failure = exc
            raise
        if _codex_reasoning_effort_failure(result.output) is not None and any(
            value.startswith("model_reasoning_effort=")
            for value in _codex_implementation_config_arguments(request.command)
        ):
            # The finally block must prove cleanup before this retry signal escapes.
            raise _CodexReasoningEffortRejectedError("codex_unsupported_reasoning_effort")
        if result.exit_status != 0:
            raise CodexIsolationError("codex_adapter_result_invalid")
        provider_session_id, _message = _parse_codex_json_events(result.output)
        if provider_session_id is None or (
            expected_session_id is not None and provider_session_id != expected_session_id
        ):
            raise CodexIsolationError("codex_adapter_result_invalid")
        return AgentRunResult(stdout=result.output, stderr="", session_id=provider_session_id)
    finally:
        if not destroy_started:
            destroy_started = True
            try:
                _destroy_codex_prepared(
                    adapter,
                    request,
                    prepared,
                    call_returned_at=time.monotonic(),
                )
                terminal_proved = True
            except BaseException as exc:
                destroy_failure = exc
        if not terminal_proved and terminal_reaper is not None:
            try:
                terminal_reaper()
            except BaseException as exc:
                destroy_failure = exc
        cleanup_failure: BaseException | None = pre_auth_cleanup_failure
        fallback_auth_descriptor = -1
        if auth_path is not None and auth_bridge is not None:
            try:
                fallback_auth_descriptor = os.dup(auth_bridge.auth_descriptor)
            except BaseException:
                fallback_auth_descriptor = -1
            try:
                _remove_codex_authentication(auth_path, auth_bridge)
            except BaseException as exc:
                cleanup_failure = exc
                try:
                    if fallback_auth_descriptor >= 0:
                        os.ftruncate(fallback_auth_descriptor, 0)
                        os.fsync(fallback_auth_descriptor)
                    if terminal_proved:
                        transient_root = auth_path.parents[1]
                        if transient_root.exists() or transient_root.is_symlink():
                            _discard_codex_profile(transient_root)
                except BaseException as fallback_exc:
                    cleanup_failure = fallback_exc
            finally:
                if fallback_auth_descriptor >= 0:
                    os.close(fallback_auth_descriptor)
        state_failure: BaseException | None = None
        if terminal_proved and profile is not None and auth_bridge is not None:
            try:
                _validate_codex_preserved_state(
                    request,
                    protected_snapshot=protected_snapshot,
                    authentication_identity=auth_bridge.auth_identity,
                    authentication=authentication,
                    baseline=preserved_baseline,
                )
            except BaseException as exc:
                state_failure = exc
        elif terminal_proved and profile is not None:
            try:
                _discard_codex_ephemeral_profile(profile)
            except BaseException as exc:
                state_failure = exc
        if (
            terminal_proved
            and cleanup_failure is not None
            and state_failure is None
            and profile is not None
            and (profile.exists() or profile.is_symlink())
        ):
            try:
                _discard_codex_ephemeral_profile(profile)
            except BaseException as exc:
                state_failure = exc
        if (
            state_failure is None
            and terminal_proved
            and cleanup_failure is None
            and destroy_failure is None
            and profile is not None
            and auth_bridge is not None
        ):
            try:
                if provider_session_id is not None:
                    _export_codex_rollout(
                        profile,
                        provider_session_id,
                        Path(request.worktree_path),
                    )
                _discard_codex_ephemeral_profile(profile)
            except BaseException as exc:
                state_failure = exc
        receipt_failure: BaseException | None = None
        if terminal_proved and any(
            failure is not None for failure in (destroy_failure, cleanup_failure, state_failure)
        ):
            try:
                _quarantine_codex_active_receipt(
                    request,
                    active_receipt_path,
                    active_receipt_identity,
                )
                receipt_failure = CodexIsolationError("codex_adapter_inventory_uncertain")
            except BaseException as exc:
                receipt_failure = exc
        elif terminal_proved:
            try:
                _clear_codex_active_receipt(
                    request,
                    active_receipt_path,
                    active_receipt_identity,
                )
            except BaseException as exc:
                receipt_failure = exc
        if receipt_failure is not None:
            raise receipt_failure
        if state_failure is not None:
            raise state_failure
        if cleanup_failure is not None:
            raise cleanup_failure
        if destroy_failure is not None:
            raise destroy_failure


def _verify_codex_implementation_executable(
    request: CodexIsolationRequestV1,
    executable_descriptor: int,
) -> None:
    """Verify the staged executable identity and digest at the host boundary."""
    path = Path(request.executable_path)
    try:
        opened = _codex_file_fingerprint(os.fstat(executable_descriptor))
        digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(executable_descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        path_identity = _codex_file_fingerprint(path.lstat())
    except (CodexIsolationError, OSError, IndexError, TypeError):
        raise CodexIsolationError("codex_adapter_request_mismatch") from None
    if (
        opened != request.executable_file_identity
        or path_identity != request.executable_file_identity
        or digest.hexdigest() != request.executable_digest
    ):
        raise CodexIsolationError("codex_adapter_request_mismatch")


def _populate_codex_implementation_profile(  # noqa: C901
    request: CodexIsolationRequestV1,
) -> Path:
    """Create the private profile after the adapter prepares its guest."""
    profile = Path(request.private_profile_path)
    source_home = Path(_codex_child_env()["CODEX_HOME"])
    source = source_home / CODEX_ATHENA_CACHE_RELATIVE_PATH / CODEX_ATHENA_VERSION
    files = _codex_athena_snapshot(source)
    source_digest = _codex_athena_snapshot_digest(files)
    if source_digest != CODEX_ATHENA_ARTIFACT_SHA256:
        raise CodexIsolationError("codex_adapter_initialization_failed")
    metadata = {relative.as_posix(): payload for relative, _mode, payload in files}
    try:
        install = json.loads(metadata[".codex-marketplace-install.json"])
        package = json.loads(metadata["package.json"])
    except (KeyError, UnicodeError, json.JSONDecodeError):
        raise CodexIsolationError("codex_adapter_initialization_failed") from None
    if (
        type(install) is not dict
        or type(package) is not dict
        or install.get("source") != CODEX_ATHENA_MARKETPLACE_SOURCE
        or install.get("revision") != CODEX_ATHENA_MARKETPLACE_REF
        or package.get("version") != CODEX_ATHENA_VERSION
    ):
        raise CodexIsolationError("codex_adapter_initialization_failed")

    destination = profile / CODEX_ATHENA_CACHE_RELATIVE_PATH / CODEX_ATHENA_VERSION
    expected_copy = tuple(
        (relative, 0o500 if mode & 0o100 else 0o400, payload) for relative, mode, payload in files
    )
    try:
        store = _codex_profile_store(profile)
        runs = store / ".runs"
        if profile.parent.parent != runs:
            raise CodexIsolationError("codex_adapter_initialization_failed")
        profile.parent.mkdir(mode=0o700)
        profile.mkdir(mode=0o700)
        profile.chmod(0o700)
        destination.mkdir(parents=True, mode=0o700)
        for relative, mode, payload in files:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_codex_profile_file(target, payload, mode)
        for directory in sorted(
            (path for path in destination.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.chmod(0o500)
        destination.chmod(0o500)
        if _codex_athena_snapshot(destination) != expected_copy:
            raise CodexIsolationError("codex_adapter_initialization_failed")
        write_secure(
            profile / "config.toml",
            _codex_implementation_config(destination),
        )
        (profile / "config.toml").chmod(0o400)
        for path in (
            profile / "home",
            profile / "tmp",
            profile / "appdata",
            profile / "localappdata",
            profile / "xdg" / "config",
            profile / "xdg" / "cache",
            profile / "xdg" / "data",
            profile / "sessions",
        ):
            path.mkdir(parents=True, mode=0o700)
            path.chmod(0o700)
        (profile / "xdg").chmod(0o700)
        for directory in (
            profile / "plugins" / "cache" / "athena" / "athena",
            profile / "plugins" / "cache" / "athena",
            profile / "plugins" / "cache",
            profile / "plugins",
        ):
            directory.chmod(0o500)
        _fsync_codex_directory(profile)
    except BaseException as exc:
        if profile.exists() or profile.is_symlink() or profile.parent.exists():
            try:
                _discard_codex_ephemeral_profile(profile)
            except BaseException:
                raise CodexIsolationError("codex_adapter_result_invalid") from None
        if isinstance(exc, CodexIsolationError):
            raise
        if isinstance(exc, OSError):
            raise CodexIsolationError("codex_adapter_initialization_failed") from None
        raise
    return profile


def _validate_codex_resume_profile(
    profile: Path,
    athena: Path,
    expected_athena: tuple[tuple[Path, int, bytes], ...],
) -> None:
    """Validate one issue-owned profile before a provider resume."""
    try:
        status = profile.lstat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != 0o500
            or (profile / "auth.json").exists()
        ):
            raise CodexIsolationError("codex_adapter_initialization_failed")
        _validate_codex_profile_inventory(profile)
        if _codex_athena_snapshot(athena) != expected_athena:
            raise CodexIsolationError("codex_adapter_initialization_failed")
        _mode, config = _read_codex_regular_file(profile / "config.toml", max_bytes=65536)
        if config.decode("utf-8") != _codex_implementation_config(athena):
            raise CodexIsolationError("codex_adapter_initialization_failed")
        _read_only, mutable = _codex_profile_policy_paths(profile, "0" * 64)
        for path in map(Path, mutable):
            mutable_status = path.lstat()
            if (
                not stat.S_ISDIR(mutable_status.st_mode)
                or mutable_status.st_uid != os.geteuid()
                or stat.S_IMODE(mutable_status.st_mode) != 0o700
            ):
                raise CodexIsolationError("codex_adapter_initialization_failed")
    except CodexIsolationError:
        raise
    except (OSError, UnicodeError):
        raise CodexIsolationError("codex_adapter_initialization_failed") from None


def _codex_file_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return the fields that bind one regular-file snapshot."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _codex_protected_fingerprint(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    """Bind one protected object to its link and change identity."""
    return (*_codex_file_fingerprint(metadata), metadata.st_nlink, metadata.st_ctime_ns)


def _read_codex_regular_file(path: Path, *, max_bytes: int) -> tuple[int, bytes]:
    """Read one owned regular file and reject path replacement."""
    descriptor = -1
    try:
        initial = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o022
            or opened.st_size > max_bytes
            or _codex_file_fingerprint(initial) != _codex_file_fingerprint(opened)
        ):
            raise CodexIsolationError("codex_adapter_initialization_failed")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(1024 * 1024, max_bytes + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise CodexIsolationError("codex_adapter_initialization_failed")
        final = os.fstat(descriptor)
        path_final = path.lstat()
        if _codex_file_fingerprint(final) != _codex_file_fingerprint(
            opened
        ) or _codex_file_fingerprint(path_final) != _codex_file_fingerprint(opened):
            raise CodexIsolationError("codex_adapter_initialization_failed")
        return stat.S_IMODE(opened.st_mode), b"".join(chunks)
    except CodexIsolationError:
        raise
    except OSError:
        raise CodexIsolationError("codex_adapter_initialization_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _codex_athena_snapshot(root: Path) -> tuple[tuple[Path, int, bytes], ...]:
    """Return a bounded immutable snapshot of the admitted Athena package."""
    try:
        root_status = root.lstat()
    except OSError:
        raise CodexIsolationError("codex_adapter_initialization_failed") from None
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or root_status.st_uid != os.geteuid()
        or stat.S_IMODE(root_status.st_mode) & 0o022
    ):
        raise CodexIsolationError("codex_adapter_initialization_failed")
    files: list[tuple[Path, int, bytes]] = []
    total = 0
    try:
        paths = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        for path in paths:
            relative = path.relative_to(root)
            status = path.lstat()
            if stat.S_ISLNK(status.st_mode):
                raise CodexIsolationError("codex_adapter_initialization_failed")
            if ".git" in relative.parts or "__pycache__" in relative.parts:
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            if stat.S_ISDIR(status.st_mode):
                if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) & 0o022:
                    raise CodexIsolationError("codex_adapter_initialization_failed")
                continue
            mode, payload = _read_codex_regular_file(
                path,
                max_bytes=CODEX_ATHENA_MAX_BYTES - total,
            )
            total += len(payload)
            files.append((relative, mode, payload))
    except CodexIsolationError:
        raise
    except OSError:
        raise CodexIsolationError("codex_adapter_initialization_failed") from None
    return tuple(files)


def _codex_athena_snapshot_digest(files: tuple[tuple[Path, int, bytes], ...]) -> str:
    """Return the digest of one ordered Athena package snapshot."""
    digest = hashlib.sha256()
    for relative, mode, payload in files:
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(str(mode).encode())
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _codex_athena_artifact_digest(root: Path) -> str:
    """Return the digest of one validated Athena package tree."""
    return _codex_athena_snapshot_digest(_codex_athena_snapshot(root))


def _write_codex_profile_file(path: Path, payload: bytes, source_mode: int) -> None:
    """Write one private profile file without path replacement."""
    descriptor = -1
    mode = 0o500 if source_mode & 0o100 else 0o400
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
        )
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except OSError:
        raise CodexIsolationError("codex_adapter_initialization_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_codex_mutable_file(path: Path, payload: bytes) -> None:
    """Write one owner-only mutable file without following a path."""
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _codex_implementation_config(
    athena_path: Path,
) -> str:
    """Return the operation-invariant configuration for one private profile."""
    return "\n".join(
        (
            f"sqlite_home = {json.dumps(str(athena_path.parents[4] / 'xdg' / 'data'))}",
            f"log_dir = {json.dumps(str(athena_path.parents[4] / 'xdg' / 'data' / 'logs'))}",
            'default_permissions = "hephaestus-automation"',
            "",
            "[features]",
            "shell_snapshot = false",
            "",
            "[marketplaces.athena]",
            'source_type = "git"',
            f"source = {json.dumps(CODEX_ATHENA_MARKETPLACE_SOURCE)}",
            f"ref = {json.dumps(CODEX_ATHENA_MARKETPLACE_REF)}",
            "",
            '[plugins."athena@athena"]',
            "enabled = true",
            "",
            "[permissions.hephaestus-automation]",
            'extends = ":workspace"',
            "",
            "[permissions.hephaestus-automation.filesystem]",
            '":minimal" = "read"',
            f'{json.dumps(str(athena_path))} = "read"',
            "",
            "[permissions.hephaestus-automation.network]",
            "enabled = false",
            "",
        )
    )


def _fsync_codex_directory(path: Path) -> None:
    """Make one private-profile directory update durable."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_codex_authentication(source: Path) -> str:
    """Read one trusted authentication file through a held descriptor."""
    try:
        _mode, payload = _read_codex_regular_file(source, max_bytes=CODEX_AUTH_MAX_BYTES)
        document = json.loads(payload)
        if type(document) is not dict:
            raise CodexIsolationError("codex_adapter_initialization_failed")
        return payload.decode("utf-8")
    except CodexIsolationError:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise CodexIsolationError("codex_adapter_initialization_failed") from None


def _validate_codex_authentication_output(
    output: str,
    authentication: str,
    auth_path: Path,
) -> None:
    """Reject output that contains a value from authentication state."""
    encoded = output.encode("utf-8")
    private_paths = (str(auth_path).encode(), str(auth_path.parent).encode())
    patterns = (*_codex_authentication_patterns(authentication), *private_paths)
    if any(value in encoded for value in patterns):
        raise CodexIsolationError("codex_adapter_result_invalid")


@dataclass(frozen=True, slots=True)
class _CodexAuthenticationBridge:
    """Hold each descriptor and identity for one per-run authentication bridge."""

    auth_descriptor: int
    run_descriptor: int
    auth_identity: tuple[int, int, int, int, int, int]
    profiles_descriptor: int
    root_descriptor: int
    run_identity: tuple[int, int, int, int, int, int]
    run_name: str


def _create_codex_authentication_bridge(
    auth_path: Path,
    payload: str,
) -> _CodexAuthenticationBridge:
    """Create authentication with held file and parent descriptors."""
    profiles_descriptor = -1
    root_descriptor = -1
    run_descriptor = -1
    auth_descriptor = -1
    try:
        profiles_descriptor = os.open(
            auth_path.parents[2],
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        with contextlib.suppress(FileExistsError):
            os.mkdir(auth_path.parents[1].name, 0o700, dir_fd=profiles_descriptor)
        root_descriptor = os.open(
            auth_path.parents[1].name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=profiles_descriptor,
        )
        root_status = os.fstat(root_descriptor)
        if root_status.st_uid != os.geteuid() or stat.S_IMODE(root_status.st_mode) != 0o700:
            raise OSError("invalid transient authentication root")
        os.mkdir(auth_path.parent.name, 0o700, dir_fd=root_descriptor)
        run_descriptor = os.open(
            auth_path.parent.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=root_descriptor,
        )
        run_status = os.fstat(run_descriptor)
        run_path_status = os.stat(
            auth_path.parent.name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            run_status.st_uid != os.geteuid()
            or stat.S_IMODE(run_status.st_mode) != 0o700
            or _codex_file_fingerprint(run_status) != _codex_file_fingerprint(run_path_status)
        ):
            raise OSError("invalid transient authentication run directory")
        auth_descriptor = os.open(
            auth_path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=run_descriptor,
        )
        data = payload.encode("utf-8")
        offset = 0
        while offset < len(data):
            offset += os.write(auth_descriptor, data[offset:])
        os.fchmod(auth_descriptor, 0o600)
        os.fsync(auth_descriptor)
        identity = _codex_file_fingerprint(os.fstat(auth_descriptor))
        path_identity = _codex_file_fingerprint(
            os.stat(auth_path.name, dir_fd=run_descriptor, follow_symlinks=False)
        )
        if path_identity != identity:
            raise OSError("authentication path identity changed")
        os.fsync(run_descriptor)
        os.fsync(root_descriptor)
        run_identity = _codex_file_fingerprint(os.fstat(run_descriptor))
        bridge = _CodexAuthenticationBridge(
            auth_descriptor=auth_descriptor,
            run_descriptor=run_descriptor,
            auth_identity=identity,
            profiles_descriptor=profiles_descriptor,
            root_descriptor=root_descriptor,
            run_identity=run_identity,
            run_name=auth_path.parent.name,
        )
        profiles_descriptor = -1
        return bridge
    except BaseException as exc:
        cleanup_ok = _remove_partial_codex_authentication(
            auth_path,
            auth_descriptor,
            run_descriptor,
            root_descriptor,
            profiles_descriptor,
        )
        profiles_descriptor = -1
        if not cleanup_ok:
            raise CodexIsolationError("codex_adapter_result_invalid") from None
        if isinstance(exc, CodexIsolationError):
            raise
        raise CodexIsolationError("codex_adapter_initialization_failed") from None
    finally:
        if profiles_descriptor >= 0:
            os.close(profiles_descriptor)


def _remove_partial_codex_authentication(  # noqa: C901
    auth_path: Path,
    auth_descriptor: int,
    run_descriptor: int,
    root_descriptor: int,
    profiles_descriptor: int,
) -> bool:
    """Remove a partially created bridge and return true only with absence proof."""
    failed = False
    if auth_descriptor >= 0:
        try:
            os.ftruncate(auth_descriptor, 0)
            os.fsync(auth_descriptor)
        except BaseException:
            failed = True
        try:
            os.close(auth_descriptor)
        except BaseException:
            failed = True
    if run_descriptor < 0:
        if root_descriptor >= 0:
            try:
                os.rmdir(auth_path.parent.name, dir_fd=root_descriptor)
                os.fsync(root_descriptor)
            except FileNotFoundError:
                pass
            except BaseException:
                failed = True
            try:
                os.close(root_descriptor)
            except BaseException:
                failed = True
        if profiles_descriptor >= 0:
            try:
                os.close(profiles_descriptor)
            except BaseException:
                failed = True
        return not failed
    try:
        os.unlink(auth_path.name, dir_fd=run_descriptor)
    except FileNotFoundError:
        pass
    except BaseException:
        failed = True
    try:
        os.fsync(run_descriptor)
    except BaseException:
        failed = True
    try:
        os.stat(auth_path.name, dir_fd=run_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except BaseException:
        failed = True
    else:
        failed = True
    try:
        os.close(run_descriptor)
    except BaseException:
        failed = True
    if root_descriptor >= 0:
        try:
            os.rmdir(auth_path.parent.name, dir_fd=root_descriptor)
            os.fsync(root_descriptor)
        except FileNotFoundError:
            pass
        except BaseException:
            failed = True
        try:
            os.close(root_descriptor)
        except BaseException:
            failed = True
    if profiles_descriptor >= 0:
        try:
            os.rmdir(auth_path.parents[1].name, dir_fd=profiles_descriptor)
            os.fsync(profiles_descriptor)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                failed = True
        try:
            os.close(profiles_descriptor)
        except BaseException:
            failed = True
    return not failed


def _clear_codex_authentication_directory(descriptor: int) -> bool:
    """Remove all unexpected per-run authentication entries without following links."""
    failed = False
    try:
        entries = list(os.scandir(descriptor))
    except BaseException:
        return False
    for entry in entries:
        failed = True
        try:
            status = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode):
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
                try:
                    _clear_codex_authentication_directory(child)
                finally:
                    os.close(child)
                os.rmdir(entry.name, dir_fd=descriptor)
            else:
                os.unlink(entry.name, dir_fd=descriptor)
        except BaseException:
            continue
    try:
        os.fsync(descriptor)
    except BaseException:
        failed = True
    return not failed


def _remove_codex_directory_contents(descriptor: int) -> None:
    """Remove a job-owned directory tree through held descriptors."""
    try:
        entries = list(os.scandir(descriptor))
        for entry in entries:
            status = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode) and not stat.S_ISLNK(status.st_mode):
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
                try:
                    _remove_codex_directory_contents(child)
                finally:
                    os.close(child)
                os.rmdir(entry.name, dir_fd=descriptor)
            else:
                os.unlink(entry.name, dir_fd=descriptor)
        os.fsync(descriptor)
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None


def _remove_codex_authentication(  # noqa: C901
    auth_path: Path,
    bridge: _CodexAuthenticationBridge,
) -> None:
    """Remove and prove absence of one complete per-run authentication bridge."""
    failed = False
    try:
        try:
            current = os.stat(
                auth_path.name,
                dir_fd=bridge.run_descriptor,
                follow_symlinks=False,
            )
            if _codex_file_fingerprint(current) != bridge.auth_identity:
                failed = True
            if _codex_file_fingerprint(os.fstat(bridge.run_descriptor)) != bridge.run_identity:
                failed = True
        except BaseException:
            failed = True
        try:
            os.ftruncate(bridge.auth_descriptor, 0)
            os.fsync(bridge.auth_descriptor)
        except BaseException:
            failed = True
        try:
            os.unlink(auth_path.name, dir_fd=bridge.run_descriptor)
            os.fsync(bridge.run_descriptor)
        except BaseException:
            failed = True
        try:
            os.stat(auth_path.name, dir_fd=bridge.run_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except BaseException:
            failed = True
        else:
            failed = True
        if not _clear_codex_authentication_directory(bridge.run_descriptor):
            failed = True
    finally:
        try:
            os.close(bridge.auth_descriptor)
        except BaseException:
            failed = True
        try:
            os.close(bridge.run_descriptor)
        except BaseException:
            failed = True
        try:
            os.rmdir(bridge.run_name, dir_fd=bridge.root_descriptor)
            os.fsync(bridge.root_descriptor)
        except BaseException:
            failed = True
        try:
            os.stat(bridge.run_name, dir_fd=bridge.root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except BaseException:
            failed = True
        else:
            failed = True
        root_is_empty = False
        try:
            root_is_empty = not any(os.scandir(bridge.root_descriptor))
        except BaseException:
            failed = True
        try:
            os.close(bridge.root_descriptor)
        except BaseException:
            failed = True
        if root_is_empty:
            try:
                os.rmdir(".transient-auth", dir_fd=bridge.profiles_descriptor)
                os.fsync(bridge.profiles_descriptor)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                    failed = True
        try:
            os.close(bridge.profiles_descriptor)
        except BaseException:
            failed = True
    if failed:
        raise CodexIsolationError("codex_adapter_result_invalid") from None


def _codex_protected_profile_snapshot(
    profile: Path,
) -> tuple[tuple[str, tuple[int, int, int, int, int, int, int, int], str | None], ...]:
    """Capture immutable configuration and Athena identities and bytes."""
    athena = profile / CODEX_ATHENA_CACHE_RELATIVE_PATH / CODEX_ATHENA_VERSION
    protected = [profile / "config.toml", athena, *athena.rglob("*")]
    snapshot: list[tuple[str, tuple[int, int, int, int, int, int, int, int], str | None]] = []
    try:
        for path in sorted(protected, key=lambda item: str(item.relative_to(profile))):
            status = path.lstat()
            relative = str(path.relative_to(profile))
            if stat.S_ISDIR(status.st_mode):
                if stat.S_IMODE(status.st_mode) != 0o500:
                    raise CodexIsolationError("codex_adapter_result_invalid")
                digest = None
            elif stat.S_ISREG(status.st_mode):
                if stat.S_IMODE(status.st_mode) not in {0o400, 0o500} or status.st_nlink != 1:
                    raise CodexIsolationError("codex_adapter_result_invalid")
                _mode, payload = _read_codex_regular_file(
                    path,
                    max_bytes=CODEX_ATHENA_MAX_BYTES,
                )
                digest = hashlib.sha256(payload).hexdigest()
            else:
                raise CodexIsolationError("codex_adapter_result_invalid")
            snapshot.append((relative, _codex_protected_fingerprint(status), digest))
    except CodexIsolationError:
        raise
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None
    return tuple(snapshot)


def _codex_authentication_patterns(authentication: str) -> tuple[bytes, ...]:
    """Return the full authentication payload and all scalar secret values."""
    patterns = {authentication.encode("utf-8")}
    try:
        document = json.loads(authentication)
    except json.JSONDecodeError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None

    def collect(value: object, *, key: str = "") -> None:
        normalized = key.lower().replace("-", "_")
        credential_key = any(
            marker in normalized
            for marker in ("token", "secret", "password", "credential", "api_key")
        )
        if isinstance(value, str) and len(value.encode("utf-8")) >= 8 and credential_key:
            patterns.add(value.encode("utf-8"))
        elif isinstance(value, dict):
            for nested_key, nested in value.items():
                collect(nested, key=str(nested_key))
        elif isinstance(value, list):
            for nested in value:
                collect(nested, key=key)

    collect(document)
    return tuple(sorted(patterns, key=len, reverse=True))


def _validate_codex_profile_inventory(profile: Path) -> None:
    """Require the exact sealed profile topology after one invocation."""
    expected_children = {
        profile: {
            "appdata",
            "config.toml",
            "home",
            "localappdata",
            "plugins",
            "sessions",
            "tmp",
            "xdg",
        },
        profile / "plugins": {"cache"},
        profile / "plugins" / "cache": {"athena"},
        profile / "plugins" / "cache" / "athena": {"athena"},
        profile / "plugins" / "cache" / "athena" / "athena": {CODEX_ATHENA_VERSION},
        profile / "xdg": {"cache", "config", "data"},
    }
    try:
        for directory, expected in expected_children.items():
            status = directory.lstat()
            expected_mode = 0o700 if directory in {profile, profile / "xdg"} else 0o500
            if (
                not stat.S_ISDIR(status.st_mode)
                or status.st_uid != os.geteuid()
                or stat.S_IMODE(status.st_mode) != expected_mode
            ):
                raise CodexIsolationError("codex_adapter_result_invalid")
            actual = {entry.name for entry in os.scandir(directory)}
            if actual != expected:
                raise CodexIsolationError("codex_adapter_result_invalid")
    except CodexIsolationError:
        raise
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None


@dataclass
class _CodexStateScanBudget:
    """Track the bounded retained-state scan."""

    files: int = 0
    bytes: int = 0
    path_bytes: int = 0


_CodexPreservedIdentity = tuple[str, tuple[int, int, int, int, int, int, int, int], bytes]
_CodexPreservedBaseline = dict[str, dict[str, _CodexPreservedIdentity]]


def _bounded_codex_directory_entries(
    descriptor: int,
    budget: _CodexStateScanBudget,
) -> list[os.DirEntry[str]]:
    """Read no more than the remaining entry budget plus one sentinel."""
    remaining = CODEX_PRESERVED_STATE_MAX_FILES - budget.files
    if remaining < 0:
        raise CodexIsolationError("codex_adapter_result_invalid")
    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(descriptor) as iterator:
            for _index in range(remaining + 1):
                try:
                    entries.append(next(iterator))
                except StopIteration:
                    break
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None
    if len(entries) > remaining:
        raise CodexIsolationError("codex_adapter_result_invalid")
    return sorted(entries, key=lambda item: item.name)


def _codex_xattr_items(list_xattrs: Any, get_xattr: Any) -> tuple[tuple[bytes, bytes], ...]:
    """Collect bounded attribute names and values through selected call seams."""
    size = list_xattrs(None, 0)
    if size < 0:
        error = ctypes.get_errno()
        if error in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            return ()
        raise OSError(error, "cannot list extended attributes")
    if size > 64 * 1024:
        raise CodexIsolationError("codex_adapter_result_invalid")
    if size == 0:
        return ()
    names_buffer = ctypes.create_string_buffer(size)
    received = list_xattrs(names_buffer, size)
    if received != size:
        raise CodexIsolationError("codex_adapter_result_invalid")
    result: list[tuple[bytes, bytes]] = []
    canonical_size = 0
    for name in sorted(filter(None, names_buffer.raw[:size].split(b"\0"))):
        value_size = get_xattr(name, None, 0)
        canonical_size += len(name) + value_size + 2
        if value_size < 0 or canonical_size > 64 * 1024:
            raise CodexIsolationError("codex_adapter_result_invalid")
        value_buffer = ctypes.create_string_buffer(value_size)
        value_received = get_xattr(name, value_buffer, value_size)
        if value_received != value_size:
            raise CodexIsolationError("codex_adapter_result_invalid")
        result.append((name, value_buffer.raw[:value_size]))
    return tuple(result)


def _codex_canonical_xattrs(list_xattrs: Any, get_xattr: Any) -> bytes:
    """Collect bounded canonical attributes through two selected call seams."""
    result = bytearray()
    for name, value in _codex_xattr_items(list_xattrs, get_xattr):
        result.extend(name)
        result.extend(b"\0")
        result.extend(value)
        result.extend(b"\0")
    return bytes(result)


def _remove_codex_descriptor_xattrs(descriptor: int, patterns: tuple[bytes, ...]) -> bool:
    """Remove credential attributes through one held directory descriptor."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        items = _codex_xattr_items(
            lambda buffer, size: library.flistxattr(descriptor, buffer, size, 0),
            lambda name, buffer, size: library.fgetxattr(
                descriptor,
                name,
                buffer,
                size,
                0,
                0,
            ),
        )
    else:
        items = _codex_xattr_items(
            lambda buffer, size: library.flistxattr(descriptor, buffer, size),
            lambda name, buffer, size: library.fgetxattr(descriptor, name, buffer, size),
        )
    removed = False
    for name, value in items:
        if not any(pattern in name or pattern in value for pattern in patterns):
            continue
        result = (
            library.fremovexattr(descriptor, name, 0)
            if sys.platform == "darwin"
            else library.fremovexattr(descriptor, name)
        )
        if result != 0:
            raise CodexIsolationError("codex_adapter_result_invalid")
        removed = True
    if removed:
        os.fsync(descriptor)
        remaining = _codex_descriptor_xattrs(descriptor)
        if any(pattern in remaining for pattern in patterns):
            raise CodexIsolationError("codex_adapter_result_invalid")
    return removed


def _codex_descriptor_xattrs(descriptor: int) -> bytes:
    """Return bounded attributes for one held regular file or directory."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        return _codex_canonical_xattrs(
            lambda buffer, size: library.flistxattr(descriptor, buffer, size, 0),
            lambda name, buffer, size: library.fgetxattr(
                descriptor,
                name,
                buffer,
                size,
                0,
                0,
            ),
        )
    return _codex_canonical_xattrs(
        lambda buffer, size: library.flistxattr(descriptor, buffer, size),
        lambda name, buffer, size: library.fgetxattr(descriptor, name, buffer, size),
    )


def _codex_symlink_xattrs(descriptor: int, name: str) -> bytes:
    """Return bounded no-follow attributes for one child symlink."""
    if sys.platform == "darwin":
        parent = fcntl.fcntl(descriptor, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0]
        path = parent + b"/" + os.fsencode(name)
    else:
        path = os.fsencode(f"/proc/self/fd/{descriptor}/{name}")
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        return _codex_canonical_xattrs(
            lambda buffer, size: library.listxattr(path, buffer, size, 0x0001),
            lambda attr, buffer, size: library.getxattr(
                path,
                attr,
                buffer,
                size,
                0,
                0x0001,
            ),
        )
    return _codex_canonical_xattrs(
        lambda buffer, size: library.llistxattr(path, buffer, size),
        lambda attr, buffer, size: library.lgetxattr(path, attr, buffer, size),
    )


def _codex_preserved_identity(
    descriptor: int,
    entry: os.DirEntry[str],
) -> _CodexPreservedIdentity:
    """Return bounded metadata for one persisted entry without following it."""
    status = entry.stat(follow_symlinks=False)
    if stat.S_ISDIR(status.st_mode):
        kind = "directory"
        flags = os.O_RDONLY | os.O_DIRECTORY
    elif stat.S_ISREG(status.st_mode):
        kind = "regular"
        flags = os.O_RDONLY
    elif stat.S_ISLNK(status.st_mode):
        kind = "symlink"
        attributes = _codex_symlink_xattrs(descriptor, entry.name)
        target = os.fsencode(os.readlink(entry.name, dir_fd=descriptor))
        final = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
        if _codex_protected_fingerprint(final) != _codex_protected_fingerprint(status):
            raise CodexIsolationError("codex_adapter_result_invalid")
        payload = b"target\0" + target + b"\0xattrs\0" + attributes
        return kind, _codex_protected_fingerprint(final), payload
    else:
        kind = "special"
        return kind, _codex_protected_fingerprint(status), b""
    child = os.open(
        entry.name,
        flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        dir_fd=descriptor,
    )
    try:
        target = _codex_descriptor_xattrs(child)
    finally:
        os.close(child)
    return kind, _codex_protected_fingerprint(status), target


def _capture_codex_preserved_directory(
    descriptor: int,
    *,
    relative: Path = Path(),
    budget: _CodexStateScanBudget,
) -> dict[str, _CodexPreservedIdentity]:
    """Capture one bounded metadata inventory without reading file contents."""
    captured: dict[str, _CodexPreservedIdentity] = {}
    try:
        entries = _bounded_codex_directory_entries(descriptor, budget)
        for entry in entries:
            budget.files += 1
            child_relative = relative / entry.name
            relative_bytes = len(os.fsencode(str(child_relative)))
            budget.path_bytes += relative_bytes
            if (
                budget.files > CODEX_PRESERVED_STATE_MAX_FILES
                or relative_bytes > 4096
                or budget.path_bytes > 32 * 1024 * 1024
                or len(child_relative.parts) > 128
            ):
                raise CodexIsolationError("codex_adapter_result_invalid")
            identity = _codex_preserved_identity(descriptor, entry)
            budget.bytes += len(identity[2])
            if budget.bytes > CODEX_PRESERVED_STATE_MAX_BYTES:
                raise CodexIsolationError("codex_adapter_result_invalid")
            captured[str(child_relative)] = identity
            if identity[0] == "directory":
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
                try:
                    if _codex_protected_fingerprint(os.fstat(child)) != identity[1]:
                        raise CodexIsolationError("codex_adapter_result_invalid")
                    captured.update(
                        _capture_codex_preserved_directory(
                            child,
                            relative=child_relative,
                            budget=budget,
                        )
                    )
                finally:
                    os.close(child)
    except CodexIsolationError:
        raise
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None
    return captured


def _capture_codex_preserved_state(request: CodexIsolationRequestV1) -> _CodexPreservedBaseline:
    """Capture all persisted metadata before authentication becomes visible."""
    profile = Path(request.private_profile_path)
    _read_only, mutable = _codex_profile_policy_paths(profile, request.run_nonce)
    roots = [Path(request.worktree_path), *map(Path, mutable)]
    baseline: _CodexPreservedBaseline = {}
    budget = _CodexStateScanBudget()
    for index, root in enumerate(roots):
        initial = root.lstat()
        if (
            not stat.S_ISDIR(initial.st_mode)
            or initial.st_uid != os.geteuid()
            or (index and stat.S_IMODE(initial.st_mode) != 0o700)
        ):
            raise CodexIsolationError("codex_adapter_result_invalid")
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if _codex_protected_fingerprint(opened) != _codex_protected_fingerprint(initial):
                raise CodexIsolationError("codex_adapter_result_invalid")
            root_xattrs = _codex_descriptor_xattrs(descriptor)
            budget.bytes += len(root_xattrs)
            if budget.bytes > CODEX_PRESERVED_STATE_MAX_BYTES:
                raise CodexIsolationError("codex_adapter_result_invalid")
            captured = _capture_codex_preserved_directory(
                descriptor,
                budget=budget,
            )
            captured[""] = (
                "directory",
                _codex_protected_fingerprint(opened),
                root_xattrs,
            )
            baseline[str(root)] = captured
        finally:
            os.close(descriptor)
    return baseline


def _scan_codex_preserved_directory(  # noqa: C901
    descriptor: int,
    *,
    authentication_identity: tuple[int, int],
    patterns: tuple[bytes, ...],
    budget: _CodexStateScanBudget,
    remove_contamination: bool,
    baseline: dict[str, _CodexPreservedIdentity] | None = None,
    relative: Path = Path(),
) -> bool:
    """Scan one held directory without following child links."""
    removed = False
    try:
        entries = _bounded_codex_directory_entries(descriptor, budget)
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None
    for entry in entries:
        budget.files += 1
        if budget.files > CODEX_PRESERVED_STATE_MAX_FILES:
            raise CodexIsolationError("codex_adapter_result_invalid")
        try:
            status = entry.stat(follow_symlinks=False)
            child_relative = relative / entry.name
            relative_bytes = len(os.fsencode(str(child_relative)))
            budget.path_bytes += relative_bytes
            if (
                relative_bytes > 4096
                or budget.path_bytes > 32 * 1024 * 1024
                or len(child_relative.parts) > 128
            ):
                raise CodexIsolationError("codex_adapter_result_invalid")
            current_identity = _codex_preserved_identity(descriptor, entry)
            changed = baseline is None or baseline.get(str(child_relative)) != current_identity
            if changed:
                budget.bytes += len(current_identity[2])
                if budget.bytes > CODEX_PRESERVED_STATE_MAX_BYTES:
                    raise CodexIsolationError("codex_adapter_result_invalid")
            name_contaminated = any(pattern in os.fsencode(entry.name) for pattern in patterns)
            if name_contaminated:
                if not remove_contamination:
                    raise CodexIsolationError("codex_adapter_result_invalid")
                if stat.S_ISDIR(status.st_mode) and not stat.S_ISLNK(status.st_mode):
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=descriptor,
                    )
                    try:
                        _remove_codex_directory_contents(child)
                    finally:
                        os.close(child)
                    os.rmdir(entry.name, dir_fd=descriptor)
                else:
                    os.unlink(entry.name, dir_fd=descriptor)
                os.fsync(descriptor)
                removed = True
                continue
            metadata_contaminated = changed and any(
                pattern in current_identity[2] for pattern in patterns
            )
            if metadata_contaminated and stat.S_ISDIR(status.st_mode):
                if not remove_contamination:
                    raise CodexIsolationError("codex_adapter_result_invalid")
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
                try:
                    _remove_codex_directory_contents(child)
                finally:
                    os.close(child)
                os.rmdir(entry.name, dir_fd=descriptor)
                os.fsync(descriptor)
                removed = True
                continue
            if stat.S_ISDIR(status.st_mode):
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
                try:
                    if _codex_file_fingerprint(os.fstat(child)) != _codex_file_fingerprint(status):
                        raise CodexIsolationError("codex_adapter_result_invalid")
                    removed = (
                        _scan_codex_preserved_directory(
                            child,
                            authentication_identity=authentication_identity,
                            patterns=patterns,
                            budget=budget,
                            remove_contamination=remove_contamination,
                            baseline=baseline,
                            relative=child_relative,
                        )
                        or removed
                    )
                finally:
                    os.close(child)
                continue
            if stat.S_ISLNK(status.st_mode):
                if not remove_contamination:
                    raise CodexIsolationError("codex_adapter_result_invalid")
                if not changed:
                    continue
                os.unlink(entry.name, dir_fd=descriptor)
                os.fsync(descriptor)
                removed = True
                continue
            if not stat.S_ISREG(status.st_mode):
                if not changed and remove_contamination:
                    continue
                raise CodexIsolationError("codex_adapter_result_invalid")
            contaminated = (
                status.st_dev,
                status.st_ino,
            ) == authentication_identity or metadata_contaminated
            if not changed and not contaminated:
                continue
            budget.bytes += status.st_size
            if budget.bytes > CODEX_PRESERVED_STATE_MAX_BYTES:
                raise CodexIsolationError("codex_adapter_result_invalid")
            child = os.open(
                entry.name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=descriptor,
            )
            try:
                opened = os.fstat(child)
                if _codex_file_fingerprint(opened) != _codex_file_fingerprint(status):
                    raise CodexIsolationError("codex_adapter_result_invalid")
                overlap = max((len(pattern) for pattern in patterns), default=1) - 1
                tail = b""
                offset = 0
                while chunk := os.pread(child, 1024 * 1024, offset):
                    candidate = tail + chunk
                    if any(pattern in candidate for pattern in patterns):
                        contaminated = True
                    tail = candidate[-overlap:] if overlap else b""
                    offset += len(chunk)
                if _codex_file_fingerprint(os.fstat(child)) != _codex_file_fingerprint(opened):
                    raise CodexIsolationError("codex_adapter_result_invalid")
            finally:
                os.close(child)
            if contaminated:
                if not remove_contamination:
                    raise CodexIsolationError("codex_adapter_result_invalid")
                os.unlink(entry.name, dir_fd=descriptor)
                os.fsync(descriptor)
                try:
                    os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise CodexIsolationError("codex_adapter_result_invalid")
                removed = True
        except CodexIsolationError:
            raise
        except (OSError, UnicodeError):
            raise CodexIsolationError("codex_adapter_result_invalid") from None
    return removed


def _validate_codex_preserved_state(
    request: CodexIsolationRequestV1,
    *,
    protected_snapshot: tuple[
        tuple[str, tuple[int, int, int, int, int, int, int, int], str | None], ...
    ],
    authentication_identity: tuple[int, int, int, int, int, int],
    authentication: str,
    baseline: _CodexPreservedBaseline | None = None,
) -> None:
    """Prove that protected and retained state contains no authentication."""
    profile = Path(request.private_profile_path)
    try:
        _read_only, mutable = _codex_profile_policy_paths(profile, request.run_nonce)
        worktree = Path(request.worktree_path)
        roots = [(worktree, True), *[(Path(path), False) for path in mutable]]
        patterns = _codex_authentication_patterns(authentication)
        budget = _CodexStateScanBudget()
        contamination_removed = False
        for root, remove_contamination in roots:
            root_status = root.lstat()
            if (
                not stat.S_ISDIR(root_status.st_mode)
                or root_status.st_uid != os.geteuid()
                or (not remove_contamination and stat.S_IMODE(root_status.st_mode) != 0o700)
            ):
                raise CodexIsolationError("codex_adapter_result_invalid")
            descriptor = os.open(
                root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                opened_root = os.fstat(descriptor)
                if _codex_file_fingerprint(opened_root) != _codex_file_fingerprint(root_status):
                    raise CodexIsolationError("codex_adapter_result_invalid")
                root_xattrs = _codex_descriptor_xattrs(descriptor)
                root_identity: _CodexPreservedIdentity = (
                    "directory",
                    _codex_protected_fingerprint(opened_root),
                    root_xattrs,
                )
                root_baseline = None if baseline is None else baseline.get(str(root), {}).get("")
                if root_baseline != root_identity:
                    budget.bytes += len(root_xattrs)
                    if budget.bytes > CODEX_PRESERVED_STATE_MAX_BYTES:
                        raise CodexIsolationError("codex_adapter_result_invalid")
                    if any(pattern in root_xattrs for pattern in patterns):
                        if remove_contamination:
                            contamination_removed = (
                                _remove_codex_descriptor_xattrs(descriptor, patterns)
                                or contamination_removed
                            )
                        else:
                            raise CodexIsolationError("codex_adapter_result_invalid")
                contamination_removed = (
                    _scan_codex_preserved_directory(
                        descriptor,
                        authentication_identity=authentication_identity[:2],
                        patterns=patterns,
                        budget=budget,
                        remove_contamination=remove_contamination,
                        baseline=None if baseline is None else baseline.get(str(root), {}),
                    )
                    or contamination_removed
                )
            finally:
                os.close(descriptor)
        if _codex_protected_profile_snapshot(profile) != protected_snapshot:
            raise CodexIsolationError("codex_adapter_result_invalid")
        if contamination_removed:
            raise CodexIsolationError("codex_adapter_result_invalid")
    except BaseException:
        if profile.exists() or profile.is_symlink():
            _discard_codex_ephemeral_profile(profile)
        raise CodexIsolationError("codex_adapter_result_invalid") from None


def _discard_codex_profile(profile: Path) -> None:
    """Delete an invalid sealed profile without following links."""
    try:
        status = profile.lstat()
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
            raise OSError("invalid profile root")
        for root, directories, _files in os.walk(profile, topdown=True, followlinks=False):
            root_path = Path(root)
            root_status = root_path.lstat()
            if not stat.S_ISDIR(root_status.st_mode) or root_status.st_uid != os.geteuid():
                raise OSError("invalid profile directory")
            root_path.chmod(0o700)
            for name in directories:
                child = root_path / name
                child_status = child.lstat()
                if stat.S_ISLNK(child_status.st_mode):
                    continue
                if not stat.S_ISDIR(child_status.st_mode) or child_status.st_uid != os.geteuid():
                    raise OSError("invalid profile directory")
        shutil.rmtree(profile)
        _fsync_codex_directory(profile.parent)
        if profile.exists() or profile.is_symlink():
            raise OSError("profile remains")
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None


def _discard_codex_ephemeral_profile(profile: Path) -> None:
    """Delete one fresh profile and its per-run directory."""
    if profile.exists() or profile.is_symlink():
        _discard_codex_profile(profile)
    descriptor = -1
    try:
        descriptor = os.open(
            profile.parent.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        with contextlib.suppress(FileNotFoundError):
            os.rmdir(profile.parent.name, dir_fd=descriptor)
        os.fsync(descriptor)
        try:
            os.stat(profile.parent.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise OSError("ephemeral run directory remains")
    except OSError:
        raise CodexIsolationError("codex_adapter_result_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def resume_codex_session(
    session_id: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
    execution_request: ExecutionRequest | None = None,
    process_tracker: ProcessTracker | None = None,
    _final_message_grace_seconds: float | None = None,
) -> AgentRunResult:
    """Resume a persisted Codex exec session and capture its latest output."""
    return _run_codex_session_with_effort_fallback(
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        model=model,
        sandbox=sandbox,
        approval=approval,
        execution_request=execution_request,
        resume_id=session_id,
        process_tracker=process_tracker,
        final_message_grace_seconds=_final_message_grace_seconds,
    )


def _run_codex_session_with_effort_fallback(
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    model: str,
    sandbox: str,
    approval: str,
    execution_request: ExecutionRequest | None = None,
    resume_id: str | None = None,
    process_tracker: ProcessTracker | None = None,
    final_message_grace_seconds: float | None = None,
) -> AgentRunResult:
    """Run Codex and retry one rejected explicit effort with its default."""
    deadline = time.monotonic() + timeout

    def execute(selected_model: str) -> AgentRunResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(["codex", "exec"], timeout)
        cmd = _codex_base_cmd(
            cwd=cwd,
            model=selected_model,
            sandbox=sandbox,
            approval=approval,
            execution_request=execution_request,
            resume_id=resume_id,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return _run_codex_command(
            cmd,
            prompt=prompt,
            cwd=cwd,
            timeout=min(float(timeout), remaining),
            process_tracker=process_tracker,
            final_message_grace_seconds=final_message_grace_seconds,
        )

    selection = parse_model_selection(model)
    effective_config = _codex_model_config(
        selection.reference,
        use_default=resume_id is None,
    )
    try:
        return execute(selection.reference)
    except _CodexReasoningEffortRejectedError:
        if not effective_config.reasoning_effort:
            raise
    LOG.warning("Codex rejected the selected reasoning effort. Retrying with the model default.")
    return execute(AgentModelSelection(effective_config.model, "default"))


def _run_codex_command(
    cmd: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout: float,
    process_tracker: ProcessTracker | None = None,
    final_message_grace_seconds: float | None = None,
) -> AgentRunResult:
    """Execute Codex with JSON events and return final text plus session id."""
    with tempfile.NamedTemporaryFile(prefix="codex-last-", suffix=".txt") as output_file:
        cmd.extend(["--output-last-message", output_file.name, "-"])
        env = _codex_child_env()
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
                process_tracker=process_tracker,
                final_message_grace_seconds=final_message_grace_seconds,
            )
        except subprocess.CalledProcessError as exc:
            stdout = _coerce_timeout_output(exc.stdout)
            stderr = _coerce_timeout_output(exc.stderr)
            final_message = _read_text_file(Path(output_file.name)).strip()
            effort_diagnostic = None if final_message else _codex_reasoning_effort_failure(stdout)
            if effort_diagnostic is not None:
                raise _CodexReasoningEffortRejectedError(
                    f"codex_unsupported_reasoning_effort: {effort_diagnostic[:300]}"
                ) from exc
            diagnostic = _codex_failure_diagnostic(stdout, stderr)
            if diagnostic is not None:
                raise AgentExecutionError(diagnostic) from exc
            raise
        except subprocess.TimeoutExpired as e:
            last_message = Path(output_file.name).read_text(encoding="utf-8").strip()
            stdout_text = _coerce_timeout_output(e.stdout)
            stderr_text = _coerce_timeout_output(e.stderr)
            diagnostic = _codex_failure_diagnostic(stdout_text, stderr_text)
            if diagnostic is not None:
                raise AgentExecutionError(diagnostic) from e
            if not last_message:
                raise
            session_id, _ = _parse_codex_json_events(stdout_text)
            return AgentRunResult(
                stdout=last_message,
                stderr=stderr_text or f"Codex wrapper timed out after {timeout}s",
                session_id=session_id,
            )
        last_message = Path(output_file.name).read_text(encoding="utf-8")

    effort_diagnostic = (
        None if last_message.strip() else _codex_reasoning_effort_failure(stdout_text)
    )
    if effort_diagnostic is not None:
        raise _CodexReasoningEffortRejectedError(
            f"codex_unsupported_reasoning_effort: {effort_diagnostic[:300]}"
        )
    diagnostic = _codex_failure_diagnostic(stdout_text, stderr_text)
    if diagnostic is not None:
        raise AgentExecutionError(diagnostic)
    session_id, event_message = _parse_codex_json_events(stdout_text)
    stdout = (last_message or event_message or stdout_text or "").strip()
    return AgentRunResult(stdout=stdout, stderr=stderr_text, session_id=session_id)


def _parse_opencode_json_events(text: str) -> tuple[str | None, str]:
    """Extract OpenCode session id and final assistant text from JSONL output."""
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
        raw_session_id = event.get("sessionID")
        if isinstance(raw_session_id, str) and raw_session_id:
            session_id = raw_session_id
        part = event.get("part")
        if (
            event.get("type") == "text"
            and isinstance(part, dict)
            and isinstance(part.get("text"), str)
        ):
            final_message = part["text"]
    return session_id, final_message.strip()


def _has_jsonl_event(text: str) -> bool:
    """Return whether JSONL output contains at least one event object."""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str) and event["type"]:
            return True
    return False


def _opencode_failure_diagnostic(*texts: str | None) -> str | None:
    """Return a bounded diagnostic when an OpenCode stream carries a fatal error.

    OpenCode v1.18.21 emits structured ``{"type":"error", "error":{"name":...,
    "data":{"message":..., "ref":...}}}`` JSON events (observed for provider
    and model failures) with a non-zero exit, and plain-text errors such as
    ``Error: Session not found`` on stderr for resume failures. Recognizing the
    structured shape converts CLI crashes into actionable automation errors
    instead of opaque exit codes; the message is bounded like the Codex path.
    """
    for text in texts:
        if not text:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                event: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "error":
                continue
            error = event.get("error")
            if not isinstance(error, dict):
                continue
            name = error.get("name")
            if not isinstance(name, str) or not name:
                continue
            data = error.get("data")
            message = ""
            if isinstance(data, dict):
                raw_message = data.get("message")
                if isinstance(raw_message, str):
                    message = f": {raw_message[:300]}"
            ref = error.get("ref")
            suffix = f" [{ref}]" if isinstance(ref, str) and ref else ""
            return f"opencode_fatal_error_event: {name}{message}{suffix}"
    return None


def _run_opencode_command(
    cmd: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    process_tracker: ProcessTracker | None = None,
) -> AgentRunResult:
    """Execute OpenCode with JSON events and return final text plus session id.

    When the CLI emits a well-formed event stream that carries no assistant
    text, the result is empty rather than the raw event stream, so downstream
    verdict parsers never see JSONL as prose. A structured ``type:"error"``
    event raises :class:`AgentExecutionError` with a bounded diagnostic — both
    on a non-zero exit and defensively on an exit-0 stream that reports an
    error without one.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        text=True,
        start_new_session=True,
        env=_platform_child_env(),
    )
    tracker = process_tracker(proc.pid) if process_tracker is not None else contextlib.nullcontext()
    try:
        with tracker:
            # Strip NUL bytes: proc.communicate(input=...) marshals text stdin
            # and would raise ``ValueError: embedded null byte`` on a stray NUL
            # before OpenCode runs (#1661) — the same guard the Claude/Codex
            # paths apply.
            stdout_text, stderr_text = proc.communicate(
                input=strip_null_bytes(prompt),
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        raise
    diagnostic = _opencode_failure_diagnostic(stdout_text, stderr_text)
    if proc.returncode != 0 or diagnostic is not None:
        if diagnostic is not None:
            raise AgentExecutionError(diagnostic)
        raise subprocess.CalledProcessError(
            proc.returncode,
            cmd,
            output=stdout_text,
            stderr=stderr_text,
        )
    session_id, event_message = _parse_opencode_json_events(stdout_text or "")
    if event_message:
        stdout: str = event_message.strip()
    elif _has_jsonl_event(stdout_text or ""):
        stdout = ""
    else:
        stdout = (stdout_text or "").strip()
    return AgentRunResult(stdout=stdout, stderr=stderr_text or "", session_id=session_id)


OPENCODE_PLAN_AGENT = "plan"


def _opencode_base_cmd(
    *,
    cwd: Path,
    session_id: str | None = None,
    model: str = "",
    sandbox: str = "workspace-write",
) -> list[str]:
    """Build an OpenCode run command that reads the prompt from stdin.

    ``--dir`` pins the session's project to the invocation directory. Without
    it OpenCode may anchor the project to an unrelated repository root (its
    registry keys on first-seen git roots), which makes every path inside a
    pipeline worktree *external* — each edit or out-of-tree ``workdir`` then
    needs an interactive permission ask that headless runs auto-deny, so the
    model silently no-ops while claiming success (#2806 validation).
    """
    selection = parse_model_selection(model)
    cmd = ["opencode", "run", "--dir", str(cwd), "--format", "json"]
    if selection.model:
        cmd.extend(["--model", selection.model])
    if selection.reasoning_effort and selection.reasoning_effort != "default":
        cmd.extend(["--variant", selection.reasoning_effort])
    if session_id:
        cmd.extend(["--session", session_id])
    cmd.extend(_opencode_sandbox_args(sandbox))
    return cmd


def _opencode_sandbox_args(sandbox: str) -> list[str]:
    """Return the OpenCode enforcement args for a requested sandbox mode.

    Verified against v1.18.21 built-ins: ``--agent plan`` denies ``edit`` on
    every project path (the model confirmed refusal and no file was written),
    giving real read-only enforcement. ``--pure`` disables skill invocation so
    read-only review jobs keep their structured output contract. The default
    build agent is full-access within the workspace. ``danger-full-access`` has
    no distinct CLI surface and stays fail-closed (#773 precedent).
    """
    if sandbox == "read-only":
        return ["--pure", "--agent", OPENCODE_PLAN_AGENT]
    if sandbox == "workspace-write":
        return []
    raise AgentExecutionError(
        f"OpenCode cannot enforce sandbox mode {sandbox!r}; select claude, "
        "codex, or pi for stages that require this isolation level"
    )


def run_opencode_session(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
    process_tracker: ProcessTracker | None = None,
) -> AgentRunResult:
    """Run a new OpenCode JSON-event session and capture its id.

    Model selections pass through in ``provider/model[:effort]`` form. When empty,
    OpenCode applies its own configured default. The CLI exposes no approval
    flag, so that compatibility input is accepted but unused. ``read-only``
    is enforced via the built-in ``plan`` agent (verified edit-deny) and
    ``--pure`` prevents skill invocation from changing the review output mode.
    """
    del approval
    cmd = _opencode_base_cmd(cwd=cwd, model=model, sandbox=sandbox)
    return _run_opencode_command(
        cmd,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        process_tracker=process_tracker,
    )


def resume_opencode_session(
    session_id: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
    process_tracker: ProcessTracker | None = None,
) -> AgentRunResult:
    """Resume an OpenCode session by id via ``--session``.

    Verified against opencode v1.18.21: resuming an existing session with an
    explicitly different ``--model`` is accepted — the run completes on the
    requested model instead of being locked or rejected — so the pass-through
    is safe for automation flows that switch phase models between calls.
    """
    del approval
    cmd = _opencode_base_cmd(cwd=cwd, session_id=session_id, model=model, sandbox=sandbox)
    return _run_opencode_command(
        cmd,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        process_tracker=process_tracker,
    )


def run_opencode_text(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
) -> subprocess.CompletedProcess[str]:
    """Run OpenCode non-interactively and return a text completed process."""
    result = run_opencode_session(
        prompt,
        cwd=cwd,
        timeout=timeout,
        model=model,
        sandbox=sandbox,
        approval=approval,
    )
    return subprocess.CompletedProcess(
        args=["opencode", "run", "--format", "json"],
        returncode=0,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _pi_base_cmd(
    executable: Path,
    *,
    lifecycle: SessionLifecycle,
    session_id: str | None = None,
) -> list[str]:
    """Build the common Pi JSON-mode command."""
    cmd = [str(executable), "--mode", "json"]
    if lifecycle is SessionLifecycle.ONE_SHOT:
        cmd.append("--no-session")
    elif session_id:
        cmd.extend(["--session", session_id])
    return cmd


def _pi_automation_cmd(
    executable: Path,
    *,
    model: str,
    thinking: str = "",
    lifecycle: SessionLifecycle,
    session_id: str | None = None,
) -> list[str]:
    """Build a non-interactive Pi command with explicit private selection."""
    selection = parse_model_selection(model)
    if not selection.model:
        raise AgentExecutionError("Pi automation requires an explicit model selection")
    cmd = _pi_base_cmd(executable, lifecycle=lifecycle, session_id=session_id)
    cmd.extend(
        [
            "--print",
            "--offline",
            "--no-approve",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-themes",
            "--model",
            selection.model,
        ]
    )
    effective_thinking = thinking or selection.reasoning_effort
    if effective_thinking and effective_thinking != "default":
        cmd.extend(["--thinking", effective_thinking])
    return cmd


def _pi_smoke_base_cmd() -> list[str]:
    """Build the non-interactive, no-discovery Pi operator-smoke command."""
    return ["pi", *PI_SMOKE_BASE_ARGS]


def _pi_sandbox_args(sandbox: str) -> list[str]:
    """Return Pi tool restrictions for the requested sandbox mode."""
    if sandbox == "no-tools":
        return ["--no-tools"]
    if sandbox == "read-only":
        return ["--tools", PI_READ_ONLY_TOOLS]
    if sandbox in {"workspace-write", "danger-full-access"}:
        return []
    raise ValueError(f"Unsupported Pi sandbox mode: {sandbox}")


def _pi_env(
    *, model: str = "", temp_dir: Path | None = None, pi_dir: Path | None = None
) -> dict[str, str]:
    """Return the minimized, privacy-enforcing environment for Pi subprocesses."""
    # The public smoke sentinel is only input to Hephaestus validation and
    # redaction; it is not a native Pi configuration channel.
    del model
    return build_pi_child_env(temp_dir=temp_dir, pi_dir=pi_dir)


def _pi_automation_env(profile_dir: Path) -> dict[str, str]:
    """Return the explicit child environment for an admitted Pi process."""
    env = build_pi_child_env(temp_dir=profile_dir, pi_dir=profile_dir)
    env["PI_OFFLINE"] = "1"
    env["PI_TELEMETRY"] = "0"
    env["PI_SKIP_VERSION_CHECK"] = "1"
    return env


@contextlib.contextmanager
def _pi_automation_profile(
    preflight: PiPreflightResult,
    *,
    pi_dir: Path | None = None,
) -> Iterator[tuple[Path, dict[str, Path]]]:
    """Materialize the exact preflight-proven packages plus private model/auth data."""
    inventory = preflight.inventory
    if inventory is None or not inventory.ready:
        raise AgentExecutionError("Pi automation lacks a verified package inventory")
    source_dir = pi_dir.expanduser() if pi_dir is not None else Path.home() / ".pi" / "agent"
    with tempfile.TemporaryDirectory(prefix="pi-automation-") as temporary:
        profile_dir = Path(temporary)
        profile_dir.chmod(0o700)
        snapshot_roots: dict[str, Path] = {}
        for key, source in sorted(inventory.roots.items()):
            expected = inventory.content_sha256.get(key)
            if not expected or package_tree_digest(source) != expected:
                raise AgentExecutionError(f"Pi package {key!r} content changed after preflight")
            destination = profile_dir / "packages" / key
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git"))
            if package_tree_digest(destination) != expected:
                raise AgentExecutionError(f"Pi package {key!r} snapshot integrity failed")
            snapshot_roots[key] = destination
        write_secure(
            profile_dir / "settings.json",
            json.dumps(
                {"packages": [str(root) for _, root in sorted(snapshot_roots.items())]},
                sort_keys=True,
            )
            + "\n",
        )
        for filename in ("models.json", "auth.json"):
            source = source_dir / filename
            if not source.is_file() or source.is_symlink() or source.stat().st_size > 1024 * 1024:
                continue
            destination = profile_dir / filename
            write_secure(destination, source.read_text(encoding="utf-8"))
        yield profile_dir, snapshot_roots


def _pi_json_session_ids(text: str) -> tuple[str, ...]:
    """Return every opaque Pi session ID present in JSONL diagnostic output."""
    session_ids: list[str] = []
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
            session_ids.append(event["id"])
    return tuple(dict.fromkeys(session_ids))


def _pi_failure_redaction_tokens(
    cwd: Path,
    model: str,
    *diagnostics: str,
    provider: str = "",
) -> tuple[str, ...]:
    """Combine configured values with session IDs observed before Pi failed."""
    tokens = list(pi_private_redaction_tokens(cwd, model))
    if provider.strip():
        tokens.append(provider.strip())
    for diagnostic in diagnostics:
        tokens.extend(_pi_json_session_ids(diagnostic))
    return tuple(dict.fromkeys(tokens))


def _run_pi_command(
    cmd: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    sandbox: str,
    model: str = "",
    provider: str = "",
    pi_dir: Path | None = None,
    _internal_admission_token: object | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Pi with prompt content attached via an ephemeral file, not argv."""
    if _internal_admission_token is not _PI_INTERNAL_ADMISSION_TOKEN:
        _require_pi_automation_admission(cwd, pi_dir=pi_dir)
    prompt_path: Path | None = None
    private_temp_dir: Path | None = None
    try:
        private_temp_dir = _prepare_pi_private_temp_dir()
        with tempfile.NamedTemporaryFile(
            "w",
            prefix="pi-prompt-",
            suffix=".md",
            encoding="utf-8",
            delete=False,
            dir=private_temp_dir,
        ) as prompt_file:
            prompt_path = Path(prompt_file.name)
            prompt_file.write(prompt)
        _verify_pi_private_prompt_file(prompt_path)
        cmd.extend(_pi_sandbox_args(sandbox))
        cmd.append(f"@{prompt_path}")
        try:
            return subprocess.run(
                cmd,
                cwd=cwd,
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                env=_pi_env(model=model, temp_dir=private_temp_dir, pi_dir=pi_dir),
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raw_stdout = exc.stdout or ""
            raw_stderr = exc.stderr or ""
            tokens = _pi_failure_redaction_tokens(
                cwd, model, raw_stdout, raw_stderr, provider=provider
            )
            redacted_cmd = _redact_pi_command_args(exc.cmd, tokens)
            redacted_output = redact_pi_private_values(raw_stdout, tokens)
            redacted_stderr = redact_pi_private_values(raw_stderr, tokens)
            redacted_exception = subprocess.CalledProcessError(
                exc.returncode,
                redacted_cmd,
                output=redacted_output,
                stderr=redacted_stderr,
            )
            # ``raise ... from None`` hides the context when rendered, but the
            # original exception remains introspectable.  Sanitize it too.
            exc.cmd = redacted_cmd
            exc.output = redacted_output
            exc.stderr = redacted_stderr
            exc.args = redacted_exception.args
            raise redacted_exception from None
        except subprocess.TimeoutExpired as exc:
            raw_output = _coerce_timeout_output(exc.output)
            raw_stderr = _coerce_timeout_output(exc.stderr)
            tokens = _pi_failure_redaction_tokens(
                cwd, model, raw_output, raw_stderr, provider=provider
            )
            redacted_cmd = _redact_pi_command_args(exc.cmd, tokens)
            redacted_output = redact_pi_private_values(raw_output, tokens)
            redacted_stderr = redact_pi_private_values(raw_stderr, tokens)
            redacted_timeout = subprocess.TimeoutExpired(
                redacted_cmd,
                exc.timeout,
                output=redacted_output,
                stderr=redacted_stderr,
            )
            exc.cmd = redacted_cmd
            exc.output = redacted_output
            exc.stderr = redacted_stderr.encode()
            exc.args = redacted_timeout.args
            raise redacted_timeout from None
    finally:
        if prompt_path is not None:
            with contextlib.suppress(OSError):
                prompt_path.unlink()
        if private_temp_dir is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(private_temp_dir)


def _invoke_pi_session(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str,
    sandbox: str,
    provider: str = "",
    pi_dir: Path | None = None,
    session_id: str | None = None,
    base_cmd: list[str] | None = None,
    require_json_event: bool = False,
    redact_observed_session_ids: bool = False,
    _internal_admission_token: object | None = None,
) -> AgentRunResult:
    """Execute Pi and preserve a new or resumed opaque session identity."""
    if _internal_admission_token is not _PI_INTERNAL_ADMISSION_TOKEN:
        _require_pi_automation_admission(cwd, pi_dir=pi_dir)
    cmd = list(base_cmd) if base_cmd is not None else _pi_smoke_base_cmd()
    result = _run_pi_command(
        cmd,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        sandbox=sandbox,
        model=model,
        provider=provider,
        pi_dir=pi_dir,
        _internal_admission_token=_PI_INTERNAL_ADMISSION_TOKEN,
    )
    raw_stdout = result.stdout or ""
    observed_session_ids = _pi_json_session_ids(raw_stdout)
    if require_json_event and not _has_jsonl_event(raw_stdout):
        raise RuntimeError("Pi smoke did not emit a JSON event")
    parsed_session_id, event_message = _parse_pi_json_events(raw_stdout)
    if require_json_event and not event_message:
        raise RuntimeError("Pi smoke did not emit a terminal assistant JSON event")
    stdout = (event_message or raw_stdout).strip()
    stderr = result.stderr or ""
    if redact_observed_session_ids:
        stdout = redact_pi_private_values(stdout, observed_session_ids)
        stderr = redact_pi_private_values(stderr, observed_session_ids)
    return AgentRunResult(
        stdout=stdout,
        stderr=stderr,
        session_id=parsed_session_id or session_id,
    )


def run_pi_smoke_session(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    provider: str = "",
    pi_dir: Path | None = None,
) -> AgentRunResult:
    """Run the fixed tool-free smoke seam without retaining a Pi session id."""
    result = _invoke_pi_session(
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        model=model,
        provider=provider,
        pi_dir=pi_dir,
        sandbox="no-tools",
        base_cmd=_pi_smoke_base_cmd(),
        require_json_event=True,
        redact_observed_session_ids=True,
        _internal_admission_token=_PI_INTERNAL_ADMISSION_TOKEN,
    )
    session_tokens = (result.session_id,) if result.session_id else ()
    return AgentRunResult(
        stdout=redact_pi_private_values(result.stdout, session_tokens),
        stderr=redact_pi_private_values(result.stderr, session_tokens),
        session_id=None,
    )


def _require_pi_request(execution_request: ExecutionRequest | None) -> ExecutionPolicy:
    """Resolve Pi's operation policy before constructing any provider command."""
    if execution_request is None:
        raise ExecutionPolicyError(
            "Pi automation requires an ExecutionRequest; sandbox and allowed-tools "
            "compatibility inputs cannot select or widen a Pi policy"
        )
    return resolve_policy(execution_request)


def validate_agent_execution_support(
    agent: str,
    execution_request: ExecutionRequest | None,
) -> None:
    """Fail when a provider cannot enforce the requested operation boundary."""
    if (
        execution_request is not None
        and execution_request.operation is AgentOperation.REMEDIATION_REPLY
        and agent not in {"claude", "pi"}
    ):
        raise AgentExecutionError(
            f"{agent} has no enforceable no-tool execution mode for remediation reply"
        )


def _require_pi_execution_policy(
    execution_request: ExecutionRequest | None,
    *,
    disable_pi_automation: bool = False,
) -> ExecutionPolicy:
    """Validate Pi policy and the emergency stop before model resolution."""
    if disable_pi_automation:
        raise PiAutomationDisabledError(
            "Pi automation disabled by CLI policy; no Pi or broker process was started"
        )
    return _require_pi_request(execution_request)


def _pi_policy_args(
    policy: ExecutionPolicy,
    preflight: PiPreflightResult,
    package_roots: dict[str, Path],
) -> list[str]:
    """Translate a reviewed policy to Pi's model-visible capability flags.

    These flags are intentionally only a second layer.  The runtime's external
    isolation adapter remains the authority for filesystem and network access.
    """
    args = ["--tools", ",".join(sorted(policy.builtins)), "--no-skills"]
    if policy.skills:
        for skill in sorted(policy.skills):
            command = f"skill:{skill.split(':', 1)[1]}"
            prove_athena_skill_command(command, preflight)
            skill_path = package_roots["athena"] / "skills" / command.removeprefix("skill:")
            try:
                resolved = skill_path.resolve(strict=True)
                package_root = package_roots["athena"].resolve(strict=True)
            except OSError as exc:
                raise AgentExecutionError(f"Pi skill {command!r} is unavailable") from exc
            if not resolved.is_relative_to(package_root) or not (resolved / "SKILL.md").is_file():
                raise AgentExecutionError(f"Pi skill {command!r} escaped its proven package")
            args.extend(["--skill", str(resolved)])
    return args


def _run_pi_with_policy(
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    model: str,
    thinking: str = "",
    policy: ExecutionPolicy,
    preflight: PiPreflightResult,
    lifecycle: SessionLifecycle,
    session_id: str | None = None,
    process_tracker: ProcessTracker | None = None,
    pi_dir: Path | None = None,
) -> AgentRunResult:
    """Run Pi through the verified OS-isolation adapter for ``policy``.

    No fallback invokes Pi directly: its native ``--tools`` flags cannot
    enforce the policy's filesystem mount or network-relay boundary.
    """
    adapter = _PI_ISOLATION_ADAPTER
    if adapter is None:
        raise PiIsolationUnavailableError(
            "Pi OS-isolation adapter is unavailable for "
            f"filesystem={policy.filesystem.value} network={policy.network.value}; "
            "no Pi provider process was started"
        )
    if preflight.executable is None:
        raise AgentExecutionError("Pi automation lacks a preflight-proven executable")
    try:
        executable = preflight.executable.resolve(strict=True)
        metadata = executable.stat()
    except OSError as exc:
        raise AgentExecutionError("Pi preflight-proven executable is unavailable") from exc
    if executable != preflight.executable:
        raise AgentExecutionError("Pi preflight-proven executable identity drifted")
    fingerprint = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    if fingerprint != preflight.executable_fingerprint:
        raise AgentExecutionError("Pi preflight-proven executable identity drifted")
    selection = (
        model
        if isinstance(model, AgentModelSelection)
        else AgentModelSelection(model, thinking)
        if thinking
        else parse_model_selection(model)
    )
    model_components = selection.model.split("/", 1)
    tokens = tuple(
        dict.fromkeys(
            (
                *pi_private_redaction_tokens(cwd, model),
                selection.model,
                selection.reference,
                *model_components,
            )
        )
    )
    failure: BaseException | None = None
    result: AgentRunResult | None = None
    try:
        with _pi_automation_profile(preflight, pi_dir=pi_dir) as (profile_dir, package_roots):
            command = _pi_automation_cmd(
                executable,
                model=selection,
                lifecycle=lifecycle,
                session_id=session_id,
            )
            command.extend(_pi_policy_args(policy, preflight, package_roots))
            result = adapter.invoke(
                policy=policy,
                command=command,
                environment=_pi_automation_env(profile_dir),
                prompt=prompt,
                cwd=cwd,
                timeout=timeout,
                model=selection.reference,
                session_id=session_id,
                process_tracker=process_tracker,
            )
    except subprocess.CalledProcessError as exc:
        failure = subprocess.CalledProcessError(
            exc.returncode,
            _redact_pi_command_args(exc.cmd, tokens),
            output=_redact_pi_exception_output(exc.stdout, tokens),
            stderr=_redact_pi_exception_output(exc.stderr, tokens),
        )
    except subprocess.TimeoutExpired as exc:
        failure = subprocess.TimeoutExpired(
            _redact_pi_command_args(exc.cmd, tokens),
            exc.timeout,
            output=_redact_pi_exception_output(exc.stdout, tokens),
            stderr=_redact_pi_exception_output(exc.stderr, tokens),
        )
    except Exception as exc:
        detail = redact_pi_private_values(str(exc), tokens)
        failure = AgentExecutionError(f"Pi isolation adapter invocation failed: {detail}")
    if failure is not None:
        raise failure
    if result is None:
        raise AssertionError("Pi isolation adapter returned no result")
    allowed_skills = set(policy.skills)
    observed = tuple(dict.fromkeys(result.observed_skill_invocations))
    if any(skill not in allowed_skills for skill in observed):
        raise AgentExecutionError("Pi isolation adapter reported an ungranted skill invocation")
    return AgentRunResult(
        stdout=redact_pi_private_values(result.stdout, tokens),
        stderr=redact_pi_private_values(result.stderr, tokens),
        session_id=result.session_id,
        session_binding=result.session_binding,
        observed_skill_invocations=observed,
    )


def _redact_pi_exception_output(
    value: str | bytes | None, tokens: Iterable[str]
) -> str | bytes | None:
    """Redact private Pi values while preserving subprocess output types."""
    if isinstance(value, bytes):
        redacted = value
        for token in tokens:
            if token:
                redacted = redacted.replace(token.encode(), PI_PRIVATE_REDACTION.encode())
        return redacted
    if isinstance(value, str):
        return redact_pi_private_values(value, tokens)
    return None


def run_agent_text(
    agent: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
    execution_request: ExecutionRequest | None = None,
    disable_pi_automation: bool = False,
    pi_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a direct-runner agent non-interactively and return text output."""
    pi_thinking = ""
    pi_command_model = model
    if is_pi(agent):
        policy = _require_pi_execution_policy(
            execution_request,
            disable_pi_automation=disable_pi_automation,
        )
        pi_request = cast(ExecutionRequest, execution_request)
        if pi_request.lifecycle is not SessionLifecycle.ONE_SHOT:
            raise ExecutionPolicyError("Pi text execution requires a ONE_SHOT ExecutionRequest")
        pi_selection = _resolve_pi_model_selection(model, pi_dir=pi_dir)
        model = pi_selection.reference
        pi_command_model = pi_selection
        pi_thinking = pi_selection.reasoning_effort
        preflight = _require_pi_automation_admission(cwd, pi_dir=pi_dir)
    if is_codex(agent):
        return run_codex_text(
            prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            sandbox=sandbox,
            approval=approval,
        )
    if is_opencode(agent):
        return run_opencode_text(
            prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            sandbox=sandbox,
            approval=approval,
        )
    if is_pi(agent):
        if execution_request is None:
            raise AssertionError("unreachable")
        result = _run_pi_with_policy(
            prompt=prompt,
            cwd=cwd,
            timeout=timeout,
            model=pi_command_model,
            thinking=pi_thinking,
            policy=policy,
            preflight=preflight,
            lifecycle=execution_request.lifecycle,
            pi_dir=pi_dir,
        )
        return subprocess.CompletedProcess(
            args=["pi", "--mode", "json"], returncode=0, stdout=result.stdout, stderr=result.stderr
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
    process_tracker: ProcessTracker | None = None,
    execution_request: ExecutionRequest | None = None,
    resume_binding: AgentSessionBinding | None = None,
    disable_pi_automation: bool = False,
    pi_dir: Path | None = None,
) -> AgentRunResult:
    """Run a direct-runner agent session and return output plus session id."""
    pi_thinking = ""
    pi_command_model = model
    if is_pi(agent):
        policy = _require_pi_execution_policy(
            execution_request,
            disable_pi_automation=disable_pi_automation,
        )
        pi_request = cast(ExecutionRequest, execution_request)
        pi_selection = _resolve_pi_model_selection(model, pi_dir=pi_dir)
        model = pi_selection.reference
        pi_command_model = pi_selection
        pi_thinking = pi_selection.reasoning_effort
        if pi_request.lifecycle is SessionLifecycle.RESUME_REQUIRED:
            if resume_binding is None:
                raise PiSessionBindingError(
                    "Pi resume-required execution is missing a session binding"
                )
            validate_pi_binding(resume_binding, cwd=cwd, role=pi_request.role, model=model)
        elif resume_binding is not None:
            raise PiSessionBindingError(
                "Pi start-new or one-shot execution must not receive a session binding"
            )
        preflight = _require_pi_automation_admission(cwd, pi_dir=pi_dir)
    if is_codex(agent):
        return run_codex_session(
            prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            sandbox=sandbox,
            approval=approval,
            execution_request=execution_request,
            process_tracker=process_tracker,
        )
    if is_opencode(agent):
        return run_opencode_session(
            prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            sandbox=sandbox,
            approval=approval,
            process_tracker=process_tracker,
        )
    if is_pi(agent):
        if execution_request is None:
            raise AssertionError("unreachable")
        result = _run_pi_with_policy(
            prompt=prompt,
            cwd=cwd,
            timeout=timeout,
            model=pi_command_model,
            thinking=pi_thinking,
            policy=policy,
            preflight=preflight,
            lifecycle=execution_request.lifecycle,
            session_id=resume_binding.session_id if resume_binding is not None else None,
            process_tracker=process_tracker,
            pi_dir=pi_dir,
        )
        if execution_request.lifecycle is SessionLifecycle.ONE_SHOT:
            return AgentRunResult(
                stdout=result.stdout,
                stderr=result.stderr,
                observed_skill_invocations=result.observed_skill_invocations,
            )
        if not result.session_id:
            raise PiSessionBindingError("Pi did not emit a session id for a resumable operation")
        return AgentRunResult(
            stdout=result.stdout,
            stderr=result.stderr,
            session_id=result.session_id,
            observed_skill_invocations=result.observed_skill_invocations,
            session_binding=create_pi_binding(
                session_id=result.session_id,
                cwd=cwd,
                role=execution_request.role,
                model=model,
            ),
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
    sandbox: str = "workspace-write",
    approval: str = "never",
    process_tracker: ProcessTracker | None = None,
    execution_request: ExecutionRequest | None = None,
    resume_binding: AgentSessionBinding | None = None,
    disable_pi_automation: bool = False,
    pi_dir: Path | None = None,
) -> AgentRunResult:
    """Resume a direct-runner agent session."""
    pi_thinking = ""
    pi_command_model = model
    if is_pi(agent):
        policy = _require_pi_execution_policy(
            execution_request,
            disable_pi_automation=disable_pi_automation,
        )
        pi_request = cast(ExecutionRequest, execution_request)
        if pi_request.lifecycle is not SessionLifecycle.RESUME_REQUIRED:
            raise ExecutionPolicyError(
                "Pi session resume requires a RESUME_REQUIRED ExecutionRequest"
            )
        pi_selection = _resolve_pi_model_selection(model, pi_dir=pi_dir)
        model = pi_selection.reference
        pi_command_model = pi_selection
        pi_thinking = pi_selection.reasoning_effort
        if resume_binding is None:
            raise PiSessionBindingError("Pi session resume requires a complete session binding")
        validate_pi_binding(resume_binding, cwd=cwd, role=pi_request.role, model=model)
        if session_id != resume_binding.session_id:
            raise PiSessionBindingError("Pi raw session id does not match its session binding")
        preflight = _require_pi_automation_admission(cwd, pi_dir=pi_dir)
    if is_codex(agent):
        return resume_codex_session(
            session_id,
            prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            sandbox=sandbox,
            approval=approval,
            execution_request=execution_request,
            process_tracker=process_tracker,
        )
    if is_opencode(agent):
        return resume_opencode_session(
            session_id,
            prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            sandbox=sandbox,
            approval=approval,
            process_tracker=process_tracker,
        )
    if is_pi(agent):
        if execution_request is None or resume_binding is None:
            raise AssertionError("unreachable")
        result = _run_pi_with_policy(
            prompt=prompt,
            cwd=cwd,
            timeout=timeout,
            model=pi_command_model,
            thinking=pi_thinking,
            policy=policy,
            preflight=preflight,
            lifecycle=execution_request.lifecycle,
            session_id=resume_binding.session_id,
            process_tracker=process_tracker,
            pi_dir=pi_dir,
        )
        return AgentRunResult(
            stdout=result.stdout,
            stderr=result.stderr,
            session_id=result.session_id or resume_binding.session_id,
            observed_skill_invocations=result.observed_skill_invocations,
            session_binding=create_pi_binding(
                session_id=result.session_id or resume_binding.session_id,
                cwd=cwd,
                role=execution_request.role,
                model=model,
                state_reference=resume_binding.state_reference,
            ),
        )
    raise ValueError(f"Agent '{agent}' does not support direct session resume")


def _communicate_codex_process(
    cmd: list[str],
    *,
    cwd: Path,
    prompt: str,
    timeout: float,
    env: dict[str, str],
    output_path: Path,
    process_tracker: ProcessTracker | None = None,
    final_message_grace_seconds: float | None = None,
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
        start_new_session=True,
    )
    tracker = process_tracker(proc.pid) if process_tracker is not None else contextlib.nullcontext()
    with tracker:
        started_at = time.monotonic()
        final_seen_at: float | None = None
        # Strip NUL bytes: proc.communicate(input=...) marshals text stdin and would
        # raise ``ValueError: embedded null byte`` on a stray NUL, before Codex runs
        # (#1661) — the same crash the Claude path guards against.
        input_text: str | None = strip_null_bytes(prompt)
        grace_seconds = (
            _codex_final_message_grace_seconds()
            if final_message_grace_seconds is None
            else final_message_grace_seconds
        )

        while True:
            elapsed = time.monotonic() - started_at
            remaining = timeout - elapsed
            if remaining <= 0:
                stdout_text, stderr_text = _terminate_process_group(proc)
                last_message = _read_text_file(output_path).strip()
                if last_message:
                    return stdout_text, stderr_text or f"Codex wrapper timed out after {timeout}s"
                raise subprocess.TimeoutExpired(
                    cmd, timeout, output=stdout_text, stderr=stderr_text
                )

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
                        stdout_text, stderr_text = _terminate_process_group(proc)
                        return (
                            stdout_text,
                            stderr_text or "Codex wrapper terminated after final message",
                        )


def _terminate_process_group(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Terminate a direct-agent process group and collect any remaining output."""
    if proc.poll() is None:
        _signal_process_group(proc, signal.SIGTERM)
    try:
        stdout_text, stderr_text = proc.communicate(timeout=CODEX_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc, signal.SIGKILL)
        stdout_text, stderr_text = proc.communicate()
    return stdout_text or "", stderr_text or ""


def _signal_process_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Signal a direct-agent's dedicated process group, falling back to the wrapper."""
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            os.killpg(os.getpgid(pid), sig)
            return
        except (ProcessLookupError, OSError):
            pass
    if sig == signal.SIGKILL:
        proc.kill()
    else:
        proc.terminate()


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _codex_final_message_grace_seconds() -> float:
    """Return the fixed internal grace used by production Codex calls."""
    return CODEX_FINAL_MESSAGE_GRACE_SECONDS


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
