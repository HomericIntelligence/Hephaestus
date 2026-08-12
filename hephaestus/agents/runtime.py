"""Shared process helpers for agent-driven CLIs."""

from __future__ import annotations

import argparse
import contextlib
import errno
import inspect
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionPolicy,
    ExecutionPolicyError,
    ExecutionRequest,
    SessionLifecycle,
    resolve_policy,
)
from hephaestus.agents.pi_plugins import (
    PiPreflightResult,
    preflight_pi_environment,
)
from hephaestus.agents.pi_session import (
    AgentSessionBinding,
    PiSessionBindingError,
    create_pi_binding,
    validate_pi_binding,
)
from hephaestus.constants import (
    agent_auth_status_timeout,
)
from hephaestus.io.utils import write_secure
from hephaestus.utils.helpers import strip_null_bytes

AgentName = Literal["claude", "codex", "pi"]
ProcessTracker = Callable[[int], contextlib.AbstractContextManager[None]]
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
CODEX_GPT_56_SOL_MODEL = "gpt-5.6-sol"
CODEX_GPT_56_TERRA_MODEL = "gpt-5.6-terra"
CODEX_GPT_56_LUNA_MODEL = "gpt-5.6-luna"
# Preserve the established Claude-tier translation while exposing the GPT-5.6
# Sol/Terra/Luna family as explicit capability-tier aliases below.
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
CLAUDE_READ_ONLY_TOOLS = "Read,Glob,Grep"
PI_PROVIDER_ENV = "HEPH_PI_PROVIDER"
PI_MODEL_ENV = "HEPH_PI_MODEL"
PI_ISOLATION_ADAPTER_ENV = "HEPH_PI_ISOLATION_ADAPTER"
PI_ISOLATION_ADAPTER_ENTRY_POINT_GROUP = "hephaestus.pi_isolation_adapters"
PI_MODEL_CONFIG_RELATIVE_PATH = Path(".pi") / "agent" / "models.json"
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
REQUIRED_ALIAS_ENVS: tuple[str, ...] = (PI_PROVIDER_ENV, PI_MODEL_ENV)
AGENT_AUTH_STATUS_COMMANDS: dict[AgentName, tuple[tuple[str, ...], ...]] = {
    "claude": (("claude", "auth", "status"),),
    "codex": (("codex", "login", "status"),),
    "pi": (("pi", "--version"),),
}

_PI_AGENT_STAGE_REQUESTS: dict[str, tuple[AgentRole, AgentOperation, SessionLifecycle]] = {
    "plan": (AgentRole.PLANNER, AgentOperation.PLAN, SessionLifecycle.START_NEW),
    "plan-review": (AgentRole.PLAN_REVIEWER, AgentOperation.PLAN_REVIEW, SessionLifecycle.ONE_SHOT),
    "implement": (AgentRole.IMPLEMENTER, AgentOperation.IMPLEMENT, SessionLifecycle.START_NEW),
    "pr-review": (AgentRole.PR_REVIEWER, AgentOperation.PR_REVIEW, SessionLifecycle.ONE_SHOT),
    "learn": (AgentRole.LEARNER, AgentOperation.LEARN, SessionLifecycle.START_NEW),
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
    session_binding: AgentSessionBinding | None = None
    observed_skill_invocations: tuple[str, ...] = ()


class AgentExecutionError(RuntimeError):
    """An agent CLI reported a fatal provider, sandbox, or tool failure."""


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


def agent_stage_execution_request(agent: str, stage: str) -> ExecutionRequest | None:
    """Return the provider policy request for a generic direct stage."""
    if not is_pi(agent):
        return None
    try:
        role, operation, lifecycle = _PI_AGENT_STAGE_REQUESTS[stage]
    except KeyError as exc:
        raise ValueError(f"Pi agent-stage operation is unsupported: {stage!r}") from exc
    return ExecutionRequest(role, operation, lifecycle)


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
    if session_agent in {"pr-reviewer", "comment-classifier"}:
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


def _load_configured_pi_isolation_adapter() -> None:
    """Load the explicitly selected host adapter for a fresh CLI process."""
    adapter_name = os.environ.get(PI_ISOLATION_ADAPTER_ENV, "").strip()
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


def _require_pi_isolation_adapter() -> None:
    """Fail at provider selection when this installation has no Pi broker."""
    if _PI_ISOLATION_ADAPTER is None:
        _load_configured_pi_isolation_adapter()
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
    configured_root = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    config_path = (
        Path(configured_root).expanduser() / "models.json"
        if configured_root
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


def _require_pi_automation_admission(cwd: Path) -> PiPreflightResult:
    """Return verified Pi admission, honoring the emergency stop before probing."""
    if os.environ.get("HEPH_DISABLE_PI_AUTOMATION") == "1":
        raise PiAutomationDisabledError(
            "Pi automation disabled by HEPH_DISABLE_PI_AUTOMATION=1; "
            "no Pi or broker process was started"
        )
    result = preflight_pi_environment(cwd, trust_override="--no-approve")
    if not result.ready:
        raise AgentExecutionError(f"{PI_AUTOMATION_PREFLIGHT_ERROR} {result.remediation_message()}")
    return result


def resolve_agent(agent: str | None, *, cwd: Path | None = None) -> AgentName:
    """Resolve an optional provider selection into a concrete backend."""
    effective_cwd = Path.cwd() if cwd is None else cwd
    if agent is not None:
        if agent not in AGENT_CHOICES:
            raise ValueError(f"Unsupported agent: {agent}")
        if agent == "pi":
            _require_pi_automation_admission(effective_cwd)
            _require_pi_isolation_adapter()
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

    installed_agents = tuple(
        agent_name
        for agent_name in AGENT_CHOICES
        if agent_name != "pi" and shutil.which(agent_name)
    )
    if not installed_agents:
        if shutil.which("pi") is not None:
            _require_pi_automation_admission(effective_cwd)
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


def reject_pi_unsupported_surface(agent: str, reason: str) -> None:
    """Fail before a legacy surface can run Pi outside its scoped policy.

    Args:
        agent: Selected provider name.
        reason: Operator-facing N/A remediation that describes the supported
            queue or wrapper to use instead.

    """
    if is_pi(agent):
        raise AgentExecutionError(f"Pi is not supported by this surface: {reason}")


def require_supported_direct_surface(
    agent: str,
    *,
    surface: str,
    pi_supported: bool,
    reason: str,
) -> None:
    """Validate a direct entry point's explicit Pi support disposition."""
    if is_pi(agent) and not pi_supported:
        reject_pi_unsupported_surface(agent, f"{surface}: {reason}")


def agent_supports_model_reasoning_effort(agent: str) -> bool:
    """Return whether an agent accepts Codex-style model reasoning selectors."""
    return is_codex(agent)


def uses_direct_agent_runner(agent: str) -> bool:
    """Return True when the provider is invoked through runtime text/session helpers."""
    if agent not in AGENT_CAPABILITIES:
        return False
    return AGENT_CAPABILITIES[agent].direct_runner


def direct_agent_model(
    agent: str,
    phase_env_var: str | None = None,
    *,
    codex_default: str = "",
) -> str:
    """Return a provider-neutral direct-runner model default.

    Pi obtains its operator-local alias from :data:`PI_MODEL_ENV`; other
    providers use an optional phase-specific environment override or the
    explicit caller default.
    """
    if is_pi(agent):
        return os.environ.get(PI_MODEL_ENV, "")
    if phase_env_var is None:
        return codex_default
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
            os.environ.get(PI_MODEL_ENV, ""),
            os.environ.get(PI_PROVIDER_ENV, ""),
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


def run_claude_text(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    allowed_tools: str = "Read,Write,Edit,Glob,Grep,Bash",
) -> subprocess.CompletedProcess[str]:
    """Run Claude Code with an explicit tool policy for read-only calls."""
    cmd = ["claude", "--print", "--output-format", "text"]
    if model:
        cmd.extend(["--model", model])

    if sandbox == "read-only":
        # --allowedTools only pre-approves tools; --tools fixes the model-visible
        # built-in surface. Bare mode and strict MCP mode without a supplied
        # config prevent ambient configuration from adding executable paths.
        cmd.extend(
            [
                "--bare",
                "--permission-mode",
                "dontAsk",
                "--tools",
                CLAUDE_READ_ONLY_TOOLS,
                "--allowedTools",
                CLAUDE_READ_ONLY_TOOLS,
                "--strict-mcp-config",
            ]
        )
    else:
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
    """Translate legacy and GPT-5.6 tier IDs into Codex model settings."""
    normalized = model.strip()
    if not normalized:
        if use_default:
            return CodexModelConfig(CODEX_DEFAULT_MODEL, CODEX_DEFAULT_REASONING_EFFORT)
        return CodexModelConfig("")
    lower_model = normalized.lower()
    base_model, separator, requested_effort = lower_model.rpartition(":")
    original_base_model = normalized.rpartition(":")[0]
    explicit_effort = (
        requested_effort
        if separator
        and requested_effort
        in {
            "default",
            "low",
            "medium",
            "high",
            "xhigh",
        }
        else ""
    )
    alias_model = base_model if explicit_effort else lower_model
    if alias_model in {"sol", CODEX_GPT_56_SOL_MODEL}:
        config = CodexModelConfig(CODEX_GPT_56_SOL_MODEL, CODEX_FABLE_REASONING_EFFORT)
    elif alias_model in {"terra", CODEX_GPT_56_TERRA_MODEL}:
        config = CodexModelConfig(CODEX_GPT_56_TERRA_MODEL, CODEX_OPUS_REASONING_EFFORT)
    elif alias_model in {"luna", CODEX_GPT_56_LUNA_MODEL}:
        config = CodexModelConfig(CODEX_GPT_56_LUNA_MODEL, CODEX_SONNET_REASONING_EFFORT)
    elif lower_model == "fable" or lower_model.startswith("claude-fable-"):
        config = CodexModelConfig(CODEX_FABLE_MODEL, CODEX_FABLE_REASONING_EFFORT)
    elif lower_model == "opus" or lower_model.startswith("claude-opus-"):
        config = CodexModelConfig(CODEX_OPUS_MODEL, CODEX_OPUS_REASONING_EFFORT)
    elif lower_model == "sonnet" or lower_model.startswith("claude-sonnet-"):
        config = CodexModelConfig(CODEX_SONNET_MODEL, CODEX_SONNET_REASONING_EFFORT)
    elif lower_model == "haiku" or lower_model.startswith("claude-haiku-"):
        config = CodexModelConfig(CODEX_HAIKU_MODEL)
    else:
        config = CodexModelConfig(original_base_model if explicit_effort else normalized)
    if explicit_effort:
        reasoning_effort = "" if explicit_effort == "default" else explicit_effort
        return CodexModelConfig(config.model, reasoning_effort)
    return config


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
    if resume_id is not None:
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


def _codex_json_objects(text: str) -> Iterable[dict[str, Any]]:
    """Yield JSON objects from Codex JSONL while ignoring non-object lines."""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _codex_error_message(value: object) -> str | None:
    """Extract a short message from a structured Codex failure payload."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if not isinstance(value, dict):
        return None
    for key in ("message", "error", "detail", "reason"):
        nested_message = _codex_error_message(value.get(key))
        if nested_message is not None:
            return nested_message
    return None


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


def _has_pi_json_event(text: str) -> bool:
    """Return whether Pi JSON-mode output contains at least one event object."""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str) and event["type"].strip():
            return True
    return False


def run_codex_session(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
    process_tracker: ProcessTracker | None = None,
) -> AgentRunResult:
    """Run a new persisted Codex exec session and capture its UUID."""
    cmd = _codex_base_cmd(cwd=cwd, model=model, sandbox=sandbox, approval=approval)
    return _run_codex_command(
        cmd,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        process_tracker=process_tracker,
    )


def resume_codex_session(
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
    """Resume a persisted Codex exec session and capture its latest output."""
    cmd = _codex_base_cmd(
        model=model,
        sandbox=sandbox,
        approval=approval,
        resume_id=session_id,
    )
    return _run_codex_command(
        cmd,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        process_tracker=process_tracker,
    )


def _run_codex_command(
    cmd: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    process_tracker: ProcessTracker | None = None,
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
                process_tracker=process_tracker,
            )
        except subprocess.CalledProcessError as exc:
            diagnostic = _codex_failure_diagnostic(
                _coerce_timeout_output(exc.stdout),
                _coerce_timeout_output(exc.stderr),
            )
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

    diagnostic = _codex_failure_diagnostic(stdout_text, stderr_text)
    if diagnostic is not None:
        raise AgentExecutionError(diagnostic)
    session_id, event_message = _parse_codex_json_events(stdout_text)
    stdout = (last_message or event_message or stdout_text or "").strip()
    return AgentRunResult(stdout=stdout, stderr=stderr_text, session_id=session_id)


def _pi_base_cmd(*, session_id: str | None = None) -> list[str]:
    """Build the common Pi JSON-mode command."""
    cmd = ["pi", "--mode", "json"]
    if session_id:
        cmd.extend(["--session", session_id])
    return cmd


def _pi_automation_cmd(*, model: str, session_id: str | None = None) -> list[str]:
    """Build a non-interactive Pi command with explicit private selection."""
    provider = os.environ.get(PI_PROVIDER_ENV, "").strip()
    selected_model = model.strip() or os.environ.get(PI_MODEL_ENV, "").strip()
    missing: list[str] = []
    if not provider:
        missing.append(PI_PROVIDER_ENV)
    if not selected_model:
        missing.append(PI_MODEL_ENV)
    if missing:
        raise AgentExecutionError(
            "Pi automation requires operator-local provider selection: " + ", ".join(missing)
        )
    cmd = _pi_base_cmd(session_id=session_id)
    cmd.extend(
        [
            "--print",
            "--offline",
            "--no-approve",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-themes",
            "--provider",
            provider,
            "--model",
            selected_model,
        ]
    )
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


def _pi_env(*, model: str = "", temp_dir: Path | None = None) -> dict[str, str]:
    """Return the minimized, privacy-enforcing environment for Pi subprocesses."""
    # The public smoke sentinel is only input to Hephaestus validation and
    # redaction; it is not a native Pi configuration channel.
    del model
    safe_names = (
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "ComSpec",
        "COMSPEC",
        "PATHEXT",
    )
    env = {name: value for name in safe_names if (value := os.environ.get(name))}
    env.setdefault("PATH", os.defpath)
    if temp_dir is not None:
        for name in ("TMPDIR", "TMP", "TEMP"):
            env[name] = str(temp_dir)
    env["PI_TELEMETRY"] = "0"
    env["PI_SKIP_VERSION_CHECK"] = "1"
    return env


def _pi_automation_env(profile_dir: Path) -> dict[str, str]:
    """Return the explicit child environment for an admitted Pi process."""
    safe_names = (
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "ComSpec",
        "COMSPEC",
        "PATHEXT",
        "TMPDIR",
        "TMP",
        "TEMP",
        "PI_CODING_AGENT_SESSION_DIR",
        "PI_PACKAGE_DIR",
    )
    env = {name: value for name in safe_names if (value := os.environ.get(name))}
    env.setdefault("PATH", os.defpath)
    env["PI_CODING_AGENT_DIR"] = str(profile_dir)
    env["PI_OFFLINE"] = "1"
    env["PI_TELEMETRY"] = "0"
    env["PI_SKIP_VERSION_CHECK"] = "1"
    return env


@contextlib.contextmanager
def _pi_automation_profile(preflight: PiPreflightResult) -> Iterator[Path]:
    """Materialize the exact preflight-proven packages plus private model/auth data."""
    inventory = preflight.inventory
    if inventory is None or not inventory.ready:
        raise AgentExecutionError("Pi automation lacks a verified package inventory")
    source_dir = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
    with tempfile.TemporaryDirectory(prefix="pi-automation-") as temporary:
        profile_dir = Path(temporary)
        profile_dir.chmod(0o700)
        package_roots = [str(root) for _, root in sorted(inventory.roots.items())]
        write_secure(
            profile_dir / "settings.json",
            json.dumps({"packages": package_roots}, sort_keys=True) + "\n",
        )
        for filename in ("models.json", "auth.json"):
            source = source_dir / filename
            if not source.is_file() or source.is_symlink() or source.stat().st_size > 1024 * 1024:
                continue
            destination = profile_dir / filename
            write_secure(destination, source.read_text(encoding="utf-8"))
        yield profile_dir


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
) -> tuple[str, ...]:
    """Combine configured values with session IDs observed before Pi failed."""
    tokens = list(pi_private_redaction_tokens(cwd, model))
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
    _internal_admission_token: object | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Pi with prompt content attached via an ephemeral file, not argv."""
    if _internal_admission_token is not _PI_INTERNAL_ADMISSION_TOKEN:
        _require_pi_automation_admission(cwd)
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
                env=_pi_env(model=model, temp_dir=private_temp_dir),
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raw_stdout = exc.stdout or ""
            raw_stderr = exc.stderr or ""
            tokens = _pi_failure_redaction_tokens(cwd, model, raw_stdout, raw_stderr)
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
            tokens = _pi_failure_redaction_tokens(cwd, model, raw_output, raw_stderr)
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


def run_pi_text(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
) -> subprocess.CompletedProcess[str]:
    """Reject the legacy raw Pi entry point.

    Pi automation is dispatched only through :func:`run_agent_text`, which
    resolves an immutable :class:`ExecutionRequest` before invoking the
    provider.  Retaining this compatibility symbol as an executable runner
    would let callers select an unscoped sandbox.
    """
    del prompt, cwd, timeout, model, sandbox, approval
    raise AgentExecutionError(
        "Unscoped run_pi_text is disabled; use run_agent_text with an ExecutionRequest"
    )


def _invoke_pi_session(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str,
    sandbox: str,
    session_id: str | None = None,
    base_cmd: list[str] | None = None,
    require_json_event: bool = False,
    redact_observed_session_ids: bool = False,
    _internal_admission_token: object | None = None,
) -> AgentRunResult:
    """Execute Pi and preserve a new or resumed opaque session identity."""
    if _internal_admission_token is not _PI_INTERNAL_ADMISSION_TOKEN:
        _require_pi_automation_admission(cwd)
    cmd = list(base_cmd) if base_cmd is not None else _pi_base_cmd(session_id=session_id)
    result = _run_pi_command(
        cmd,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        sandbox=sandbox,
        model=model,
        _internal_admission_token=_PI_INTERNAL_ADMISSION_TOKEN,
    )
    raw_stdout = result.stdout or ""
    observed_session_ids = _pi_json_session_ids(raw_stdout)
    if require_json_event and not _has_pi_json_event(raw_stdout):
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


def run_pi_session(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
) -> AgentRunResult:
    """Reject the legacy raw Pi session entry point.

    New Pi sessions must begin via :func:`run_agent_session` with a resolved
    execution policy.  A raw session cannot bind its tool, filesystem, and
    network privileges to an operation.
    """
    del prompt, cwd, timeout, model, sandbox, approval
    raise AgentExecutionError(
        "Unscoped run_pi_session is disabled; use run_agent_session with an ExecutionRequest"
    )


def run_pi_smoke_session(
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
) -> AgentRunResult:
    """Run the fixed tool-free smoke seam without retaining a Pi session id."""
    result = _invoke_pi_session(
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        model=model,
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


def resume_pi_session(
    session_id: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: int,
    model: str = "",
    sandbox: str = "workspace-write",
    approval: str = "never",
) -> AgentRunResult:
    """Reject the legacy raw Pi resume entry point.

    Resumption must use :func:`resume_agent_session`, which requires a
    validated session binding and a resume-only execution request.
    """
    del session_id, prompt, cwd, timeout, model, sandbox, approval
    raise AgentExecutionError(
        "Unscoped resume_pi_session is disabled; use resume_agent_session with "
        "an ExecutionRequest and AgentSessionBinding"
    )


def _require_pi_request(execution_request: ExecutionRequest | None) -> ExecutionPolicy:
    """Resolve Pi's operation policy before constructing any provider command."""
    if execution_request is None:
        raise ExecutionPolicyError(
            "Pi automation requires an ExecutionRequest; sandbox and allowed-tools "
            "compatibility inputs cannot select or widen a Pi policy"
        )
    return resolve_policy(execution_request)


def _require_admitted_pi_policy(
    cwd: Path, execution_request: ExecutionRequest | None
) -> tuple[ExecutionPolicy, PiPreflightResult]:
    """Require Pi admission before resolving its caller-supplied execution policy."""
    preflight = _require_pi_automation_admission(cwd)
    return _require_pi_request(execution_request), preflight


def _pi_policy_args(policy: ExecutionPolicy) -> list[str]:
    """Translate a reviewed policy to Pi's model-visible capability flags.

    These flags are intentionally only a second layer.  The runtime's external
    isolation adapter remains the authority for filesystem and network access.
    """
    args = ["--tools", ",".join(sorted(policy.builtins))]
    if policy.skills:
        commands = ",".join(f"skill:{skill.split(':', 1)[1]}" for skill in policy.skills)
        args.extend(["--commands", commands])
    return args


def _run_pi_with_policy(
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    model: str,
    policy: ExecutionPolicy,
    preflight: PiPreflightResult,
    session_id: str | None = None,
    process_tracker: ProcessTracker | None = None,
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
    command = _pi_automation_cmd(model=model, session_id=session_id)
    command.extend(_pi_policy_args(policy))
    tokens = pi_private_redaction_tokens(cwd, model)
    try:
        with _pi_automation_profile(preflight) as profile_dir:
            result = adapter.invoke(
                policy=policy,
                command=command,
                environment=_pi_automation_env(profile_dir),
                prompt=prompt,
                cwd=cwd,
                timeout=timeout,
                model=model,
                session_id=session_id,
                process_tracker=process_tracker,
            )
    except subprocess.CalledProcessError as exc:
        raise subprocess.CalledProcessError(
            exc.returncode,
            _redact_pi_command_args(exc.cmd, tokens),
            output=_redact_pi_exception_output(exc.stdout, tokens),
            stderr=_redact_pi_exception_output(exc.stderr, tokens),
        ) from None
    except subprocess.TimeoutExpired as exc:
        raise subprocess.TimeoutExpired(
            _redact_pi_command_args(exc.cmd, tokens),
            exc.timeout,
            output=_redact_pi_exception_output(exc.stdout, tokens),
            stderr=_redact_pi_exception_output(exc.stderr, tokens),
        ) from None
    except Exception as exc:
        detail = redact_pi_private_values(str(exc), tokens)
        raise AgentExecutionError(f"Pi isolation adapter invocation failed: {detail}") from None
    allowed_skills = set(policy.skills)
    observed = tuple(dict.fromkeys(result.observed_skill_invocations))
    if any(skill not in allowed_skills for skill in observed):
        raise AgentExecutionError("Pi isolation adapter reported an ungranted skill invocation")
    return result


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
) -> subprocess.CompletedProcess[str]:
    """Run a direct-runner agent non-interactively and return text output."""
    if is_pi(agent):
        policy, preflight = _require_admitted_pi_policy(cwd, execution_request)
        pi_request = cast(ExecutionRequest, execution_request)
        if pi_request.lifecycle is not SessionLifecycle.ONE_SHOT:
            raise ExecutionPolicyError("Pi text execution requires a ONE_SHOT ExecutionRequest")
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
        if execution_request is None:
            raise AssertionError("unreachable")
        result = _run_pi_with_policy(
            prompt=prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            policy=policy,
            preflight=preflight,
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
) -> AgentRunResult:
    """Run a direct-runner agent session and return output plus session id."""
    if is_pi(agent):
        policy, preflight = _require_admitted_pi_policy(cwd, execution_request)
        pi_request = cast(ExecutionRequest, execution_request)
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
    if is_codex(agent):
        return run_codex_session(
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
            model=model,
            policy=policy,
            preflight=preflight,
            session_id=resume_binding.session_id if resume_binding is not None else None,
            process_tracker=process_tracker,
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
) -> AgentRunResult:
    """Resume a direct-runner agent session."""
    if is_pi(agent):
        policy, preflight = _require_admitted_pi_policy(cwd, execution_request)
        pi_request = cast(ExecutionRequest, execution_request)
        if pi_request.lifecycle is not SessionLifecycle.RESUME_REQUIRED:
            raise ExecutionPolicyError(
                "Pi session resume requires a RESUME_REQUIRED ExecutionRequest"
            )
        if resume_binding is None:
            raise PiSessionBindingError("Pi session resume requires a complete session binding")
        validate_pi_binding(resume_binding, cwd=cwd, role=pi_request.role, model=model)
        if session_id != resume_binding.session_id:
            raise PiSessionBindingError("Pi raw session id does not match its session binding")
    if is_codex(agent):
        return resume_codex_session(
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
            model=model,
            policy=policy,
            preflight=preflight,
            session_id=resume_binding.session_id,
            process_tracker=process_tracker,
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
    timeout: int,
    env: dict[str, str],
    output_path: Path,
    process_tracker: ProcessTracker | None = None,
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
        grace_seconds = _codex_final_message_grace_seconds()

        while True:
            elapsed = time.monotonic() - started_at
            remaining = timeout - elapsed
            if remaining <= 0:
                stdout_text, stderr_text = _terminate_codex_process(proc)
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
                        stdout_text, stderr_text = _terminate_codex_process(proc)
                        return (
                            stdout_text,
                            stderr_text or "Codex wrapper terminated after final message",
                        )


def _terminate_codex_process(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Terminate a Codex process group and collect any remaining stdout/stderr."""
    if proc.poll() is None:
        _signal_codex_process_group(proc, signal.SIGTERM)
    try:
        stdout_text, stderr_text = proc.communicate(timeout=CODEX_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_codex_process_group(proc, signal.SIGKILL)
        stdout_text, stderr_text = proc.communicate()
    return stdout_text or "", stderr_text or ""


def _signal_codex_process_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Signal Codex's dedicated process group, falling back to its wrapper."""
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
