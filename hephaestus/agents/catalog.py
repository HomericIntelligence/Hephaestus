"""Canonical harness identity and model-effort transport rules.

This module is the one place for harness names and for the transport of a
free-form effort value. It holds no model catalog: a literal model string stays
authoritative as ADR-0044 requires.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum

LOG = logging.getLogger(__name__)

DEFAULT_EFFORT_SENTINEL = "default"
"""The one sentinel that means an operator did not select an effort."""

OPENCODE2_EFFORT_GRAMMAR = "{model}#{effort}"
"""The OpenCode 2 inline grammar for a model and its effort."""


class Harness(StrEnum):
    """Agent runtimes that the pipeline can start."""

    CLAUDE = "claude"
    CODEX = "codex"
    PI = "pi"
    OPENCODE = "opencode"
    OPENCODE2 = "opencode2"


class EffortTransport(StrEnum):
    """Surfaces that carry a free-form effort value to a provider."""

    NONE = "none"
    CODEX_CONFIG = "codex_config"
    OPENCODE_VARIANT = "opencode_variant"
    OPENCODE2_INLINE = "opencode2_inline"
    PI_THINKING = "pi_thinking"


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

    ``core_capabilities`` come from the provider's base CLI.
    ``package_capabilities`` need an explicit, separately verified package.
    ``unavailable_capabilities`` fail closed rather than being inferred from a
    similarly named provider feature.
    """

    direct_runner: bool
    supports_approval: bool
    supports_sandbox: bool
    supports_sessions: bool
    core_capabilities: frozenset[AgentCapability] = frozenset()
    package_capabilities: frozenset[AgentCapability] = frozenset()
    unavailable_capabilities: frozenset[AgentCapability] = frozenset()


@dataclass(frozen=True, slots=True)
class ModelTransport:
    """Rules that move an effort value onto one provider command."""

    grammar: str
    effort_transport: EffortTransport
    requires_model_for_effort: bool = False


@dataclass(frozen=True, slots=True)
class HarnessRecord:
    """One harness's stable identity and transport data."""

    name: Harness
    display_name: str
    commit_coauthor_name: str
    cli_name: str
    auth_probe: tuple[tuple[str, ...], ...]
    auto_detectable: bool
    requires_isolation_adapter: bool
    capabilities: AgentCapabilities
    model_transport: ModelTransport


HARNESS_RECORDS: dict[Harness, HarnessRecord] = {
    Harness.CLAUDE: HarnessRecord(
        name=Harness.CLAUDE,
        display_name="Claude Code",
        commit_coauthor_name="Claude Code",
        cli_name="claude",
        auth_probe=(("claude", "auth", "status"),),
        auto_detectable=True,
        requires_isolation_adapter=False,
        capabilities=AgentCapabilities(
            direct_runner=False,
            supports_approval=False,
            supports_sandbox=True,
            supports_sessions=True,
        ),
        model_transport=ModelTransport(
            grammar="",
            effort_transport=EffortTransport.NONE,
        ),
    ),
    Harness.CODEX: HarnessRecord(
        name=Harness.CODEX,
        display_name="Codex",
        commit_coauthor_name="Codex",
        cli_name="codex",
        auth_probe=(("codex", "login", "status"),),
        auto_detectable=True,
        requires_isolation_adapter=False,
        capabilities=AgentCapabilities(
            direct_runner=True,
            supports_approval=True,
            supports_sandbox=True,
            supports_sessions=True,
        ),
        model_transport=ModelTransport(
            grammar="",
            effort_transport=EffortTransport.CODEX_CONFIG,
        ),
    ),
    Harness.PI: HarnessRecord(
        name=Harness.PI,
        display_name="Pi",
        commit_coauthor_name="Pi",
        cli_name="pi",
        auth_probe=(("pi", "--version"),),
        auto_detectable=False,
        requires_isolation_adapter=True,
        capabilities=AgentCapabilities(
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
        model_transport=ModelTransport(
            grammar="",
            effort_transport=EffortTransport.PI_THINKING,
        ),
    ),
    Harness.OPENCODE: HarnessRecord(
        name=Harness.OPENCODE,
        display_name="OpenCode",
        # The commit trailers keep their own established value in this field.
        commit_coauthor_name="OpenCode-AI",
        cli_name="opencode",
        # `providers list` can report zero credentials on a working setup. The
        # run verifies authentication, so a credential count is not parsed.
        auth_probe=(("opencode", "providers", "list"),),
        auto_detectable=True,
        requires_isolation_adapter=False,
        capabilities=AgentCapabilities(
            direct_runner=True,
            supports_approval=False,
            supports_sandbox=True,
            supports_sessions=True,
        ),
        model_transport=ModelTransport(
            grammar="",
            effort_transport=EffortTransport.OPENCODE_VARIANT,
        ),
    ),
    Harness.OPENCODE2: HarnessRecord(
        name=Harness.OPENCODE2,
        display_name="OpenCode 2",
        commit_coauthor_name="OpenCode-AI",
        cli_name="opencode",
        # OpenCode 2 removed the V1 `providers list` surface. This probe proves
        # only that the V2 CLI starts, the same contract as the V1 probe.
        auth_probe=(("opencode", "--version"),),
        auto_detectable=True,
        requires_isolation_adapter=False,
        capabilities=AgentCapabilities(
            direct_runner=True,
            supports_approval=False,
            supports_sandbox=True,
            supports_sessions=True,
        ),
        model_transport=ModelTransport(
            grammar=OPENCODE2_EFFORT_GRAMMAR,
            effort_transport=EffortTransport.OPENCODE2_INLINE,
            requires_model_for_effort=True,
        ),
    ),
}

AGENT_CAPABILITIES: dict[str, AgentCapabilities] = {
    record.name.value: record.capabilities for record in HARNESS_RECORDS.values()
}


class UnsupportedAgentError(ValueError):
    """A name is not a harness that this catalog records."""

    code = "unsupported_agent"

    def __init__(self, agent: str) -> None:
        """Keep the rejected name as structured, machine-readable data."""
        self.agent = agent
        super().__init__(f"Unsupported agent: {agent}")

    def __str__(self) -> str:
        """Return the established human text without matching it elsewhere."""
        return f"Unsupported agent: {self.agent}"


class EffortRequiresModelError(ValueError):
    """An inline effort needs an explicit model reference."""

    def __init__(self, effort: str) -> None:
        """Keep the rejected effort for the localized operator message."""
        self.effort = effort
        super().__init__(effort)


@dataclass(frozen=True, slots=True)
class EffortArgs:
    """Command pieces that one effort transport adds."""

    extra_args: tuple[str, ...] = ()
    model_override: str | None = None

    @property
    def emits_effort(self) -> bool:
        """Return whether this transport carries an explicit effort."""
        return bool(self.extra_args) or self.model_override is not None


def harness_record(name: str) -> HarnessRecord:
    """Return one harness record or reject the name."""
    try:
        return HARNESS_RECORDS[Harness(name)]
    except ValueError as exc:
        raise UnsupportedAgentError(name) from exc


def apply_effort(
    transport: ModelTransport,
    model: str,
    effort: str = "",
    *,
    thinking: str = "",
) -> EffortArgs:
    """Return the command pieces that carry one effort value.

    An empty effort and the ``default`` sentinel both select no explicit
    effort. The Pi transport gives ``thinking`` precedence over ``effort``.
    """
    value = (effort or "").strip()
    if value == DEFAULT_EFFORT_SENTINEL:
        value = ""

    mode = transport.effort_transport
    if mode is EffortTransport.NONE:
        return EffortArgs()
    if mode is EffortTransport.CODEX_CONFIG:
        if value:
            return EffortArgs(("-c", f"model_reasoning_effort={json.dumps(value)}"))
        return EffortArgs()
    if mode is EffortTransport.OPENCODE_VARIANT:
        if value:
            return EffortArgs(("--variant", value))
        return EffortArgs()
    if mode is EffortTransport.OPENCODE2_INLINE:
        if not value:
            return EffortArgs()
        if transport.requires_model_for_effort and not model:
            raise EffortRequiresModelError(value)
        return EffortArgs(model_override=transport.grammar.format(model=model, effort=value))
    if mode is EffortTransport.PI_THINKING:
        effective = (thinking or value).strip()
        if effective and effective != DEFAULT_EFFORT_SENTINEL:
            return EffortArgs(("--thinking", effective))
        return EffortArgs()

    # ADR-0063 requires a warning and a fall back to the harness default.
    LOG.warning("Unknown effort transport %r. No effort argument was added.", mode)
    return EffortArgs()


__all__ = [
    "AGENT_CAPABILITIES",
    "DEFAULT_EFFORT_SENTINEL",
    "HARNESS_RECORDS",
    "OPENCODE2_EFFORT_GRAMMAR",
    "AgentCapabilities",
    "AgentCapability",
    "EffortArgs",
    "EffortRequiresModelError",
    "EffortTransport",
    "Harness",
    "HarnessRecord",
    "ModelTransport",
    "UnsupportedAgentError",
    "apply_effort",
    "harness_record",
]
