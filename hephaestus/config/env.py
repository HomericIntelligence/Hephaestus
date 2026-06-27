"""Unified environment variable manager for ProjectHephaestus.

Centralises every ``HEPH_*`` / ``HEPHAESTUS_*`` environment-variable read
behind a declarative registry so that:

* Type coercion (``int``, ``float``, ``bool``, ``str``) happens once, with
  consistent error-handling (log-and-fallback on malformed values).
* Every env var is self-documenting: name, type, default, and a one-line
  description live in one place.
* Callers never call ``os.environ.get()`` directly for application-level
  config — they call a typed accessor function or use the registry.

Quick-start::

    from hephaestus.config.env import env

    timeout = env.int("HEPH_PLANNER_AGENT_TIMEOUT", default=7200)
    model   = env.str("HEPH_PLANNER_MODEL", default="claude-opus-4-7")
    rate    = env.float("HEPHAESTUS_GH_GLOBAL_RATE", default=10.0)
    guard   = env.bool("HEPHAESTUS_RATE_GUARD", default=True)

    # Register-and-read pattern (self-documenting):
    env.register(
        "HEPH_CI_POLL_MAX_WAIT",
        type=int,
        default=600,
        description="Wall-clock seconds for the CI-driver poll loops.",
    )
    poll_wait = env.int("HEPH_CI_POLL_MAX_WAIT")

Design decisions:

* **No Pydantic / attrs dependency** — pure stdlib so the config layer
  stays importable before third-party packages are installed.
* **Thread-safe** — reads are inherently safe (each call goes through
  ``os.environ.get`` which is atomic for CPython); the registry dict is
  populated at import time and read-only thereafter.
* **Fail-open** — a malformed env var logs a warning and returns the
  default rather than crashing the process, matching the existing
  ``claude_timeouts._read_int_env`` contract.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── Builtin aliases ───────────────────────────────────────────────────
# The accessor methods below are deliberately named ``str``/``int``/
# ``float``/``bool`` for ergonomic calls (``env.int("X")``).  Those names
# shadow the builtins *inside the class body*, so annotations like
# ``default: str | None`` would otherwise resolve to the method object
# rather than the type.  Bind the builtins to private aliases here and use
# them in the signatures.
_Str = str
_Int = int
_Float = float
_Bool = bool

# ── Boolean truthy/falsy sets (shared with config.utils) ──────────────
_TRUTHY: frozenset[str] = frozenset({"true", "yes", "on", "1"})
_FALSY: frozenset[str] = frozenset({"false", "no", "off", "0"})


@dataclass(frozen=True)
class EnvVarSpec:
    """Metadata for a single environment variable."""

    name: str
    type: type = str
    default: Any = None
    description: str = ""


class EnvRegistry:
    """Thread-safe, self-documenting environment variable registry.

    Instantiate once (the module-level ``env`` singleton) and use its
    typed accessor methods everywhere instead of ``os.environ.get()``.
    """

    def __init__(self) -> None:
        """Initialise an empty registry with its own lock."""
        self._specs: dict[str, EnvVarSpec] = {}
        self._lock = threading.Lock()

    # ── Registration ──────────────────────────────────────────────────

    def register(
        self,
        name: str,
        *,
        type: type = str,
        default: Any = None,
        description: str = "",
    ) -> None:
        """Register an env var spec.  Overwrites silently if already registered."""
        with self._lock:
            self._specs[name] = EnvVarSpec(
                name=name, type=type, default=default, description=description
            )

    def spec(self, name: str) -> EnvVarSpec | None:
        """Return the spec for *name*, or ``None`` if not registered."""
        return self._specs.get(name)

    @property
    def all_specs(self) -> dict[str, EnvVarSpec]:
        """Return a snapshot of all registered specs."""
        with self._lock:
            return dict(self._specs)

    # ── Typed accessors ───────────────────────────────────────────────
    #
    # Each accessor first checks ``os.environ`` for a live value.  When the
    # env var is unset, the accessor consults the **registry** for a
    # registered default before falling back to the caller-supplied
    # *default* keyword.  This means
    # ``env.register("HEPH_FOO", type=int, default=42)`` followed by
    # ``env.int("HEPH_FOO")`` returns ``42`` — not ``0`` — when the env
    # var is not set.

    def _registered_default(self, name: str, fallback: Any) -> Any:
        """Return the registered default for *name*, or *fallback* if unregistered."""
        spec = self._specs.get(name)
        if spec is not None and spec.default is not None:
            return spec.default
        return fallback

    def str(self, name: _Str, *, default: _Str | None = None) -> _Str:
        """Read a string env var.

        When the env var is unset, the registered default is used if
        available; otherwise *default* (which defaults to ``""``).
        """
        fallback = self._registered_default(name, "")
        effective = default if default is not None else fallback
        raw = os.environ.get(name)
        if raw is None:
            return effective
        return raw

    def int(self, name: _Str, *, default: _Int | None = None) -> _Int:
        """Read an integer env var, falling back to registered/default on parse error."""
        fallback = self._registered_default(name, 0)
        effective: int = default if default is not None else fallback
        raw = os.environ.get(name)
        if raw is None:
            return effective
        try:
            return int(raw)
        except ValueError:
            logger.warning(
                "Ignoring non-integer %s=%r — using default %d",
                name,
                raw,
                effective,
            )
            return effective

    def float(self, name: _Str, *, default: _Float | None = None) -> _Float:
        """Read a float env var, falling back to registered/default on parse error."""
        fallback = self._registered_default(name, 0.0)
        effective: float = default if default is not None else fallback
        raw = os.environ.get(name)
        if raw is None:
            return effective
        try:
            return float(raw)
        except ValueError:
            logger.warning(
                "Ignoring non-float %s=%r — using default %s",
                name,
                raw,
                effective,
            )
            return effective

    def bool(self, name: _Str, *, default: _Bool | None = None) -> _Bool:
        """Read a boolean env var (truthy: true/yes/on/1, falsy: false/no/off/0).

        Any other value falls back to registered/default with a warning.
        """
        fallback = self._registered_default(name, False)
        effective: bool = default if default is not None else fallback
        raw = os.environ.get(name)
        if raw is None:
            return effective
        lower = raw.strip().lower()
        if lower in _TRUTHY:
            return True
        if lower in _FALSY:
            return False
        logger.warning(
            "Ignoring non-boolean %s=%r (expected true/false/yes/no/on/off/0/1) — using default %s",
            name,
            raw,
            effective,
        )
        return effective

    # ── Bulk helpers ──────────────────────────────────────────────────

    def snapshot(self) -> dict[_Str, _Str]:
        """Return the current value of every registered env var.

        Useful for diagnostics / debug logging.
        """
        with self._lock:
            specs = dict(self._specs)
        out: dict[_Str, _Str] = {}
        for name, spec in specs.items():
            raw = os.environ.get(name)
            out[name] = raw if raw is not None else f"<default: {spec.default!r}>"
        return out

    def as_rst_table(self) -> _Str:
        """Render registered env vars as an RST table (for docs generation)."""
        with self._lock:
            specs = sorted(self._specs.values(), key=lambda s: s.name)
        if not specs:
            return ""
        lines = [
            ".. list-table:: Environment Variables",
            "   :header-rows: 1",
            "",
            "   * - Variable",
            "      - Type",
            "      - Default",
            "      - Description",
        ]
        for s in specs:
            lines.append(
                f"   * - ``{s.name}``"
                f"\n      - {s.type.__name__}"
                f"\n      - ``{s.default!r}``"
                f"\n      - {s.description}"
            )
        return "\n".join(lines)


# ── Module-level singleton ────────────────────────────────────────────
env = EnvRegistry()

# ── Pre-register well-known env vars ─────────────────────────────────
# These are the vars that were already scattered across the codebase.
# Callers can register additional vars at import time.

_REGISTRY: list[dict[str, Any]] = [
    # ── Agent model overrides ──
    {
        "name": "HEPH_PLANNER_MODEL",
        "type": str,
        "default": "claude-opus-4-7",
        "description": "Model for the planner agent.",
    },
    {
        "name": "HEPH_IMPLEMENTER_MODEL",
        "type": str,
        "default": "claude-haiku-4-5",
        "description": "Model for the implementer agent.",
    },
    {
        "name": "HEPH_REVIEWER_MODEL",
        "type": str,
        "default": "claude-sonnet-4-6",
        "description": "Model for plan/PR reviewers.",
    },
    {
        "name": "HEPH_ADVISE_MODEL",
        "type": str,
        "default": "claude-haiku-4-5",
        "description": "Model for the advise agent.",
    },
    {
        "name": "HEPH_LEARN_MODEL",
        "type": str,
        "default": "claude-haiku-4-5",
        "description": "Model for the /learn agent.",
    },
    # ── Agent timeouts ──
    {
        "name": "HEPH_PLANNER_AGENT_TIMEOUT",
        "type": int,
        "default": 7200,
        "description": "Timeout (s) for agent calls in the planner.",
    },
    {
        "name": "HEPH_PLAN_REVIEWER_AGENT_TIMEOUT",
        "type": int,
        "default": 7200,
        "description": "Timeout (s) for the plan reviewer agent.",
    },
    {
        "name": "HEPH_IMPLEMENTER_AGENT_TIMEOUT",
        "type": int,
        "default": 7200,
        "description": "Timeout (s) for the implementer agent.",
    },
    {
        "name": "HEPH_ADVISE_AGENT_TIMEOUT",
        "type": int,
        "default": 7200,
        "description": "Timeout (s) for advise agent calls.",
    },
    {
        "name": "HEPH_PR_REVIEWER_AGENT_TIMEOUT",
        "type": int,
        "default": 7200,
        "description": "Timeout (s) for PR reviewer agent analysis.",
    },
    {
        "name": "HEPH_ADDRESS_REVIEW_AGENT_TIMEOUT",
        "type": int,
        "default": 7200,
        "description": "Timeout (s) for the address-review fix session.",
    },
    {
        "name": "HEPH_CI_DRIVER_AGENT_TIMEOUT",
        "type": int,
        "default": 7200,
        "description": "Timeout (s) for the CI-driver fix session.",
    },
    {
        "name": "HEPH_LEARN_AGENT_TIMEOUT",
        "type": int,
        "default": 7200,
        "description": "Timeout (s) for /learn agent calls.",
    },
    {
        "name": "HEPH_FOLLOW_UP_AGENT_TIMEOUT",
        "type": int,
        "default": 7200,
        "description": "Timeout (s) for the follow-up-issue agent session.",
    },
    # ── CI / loop control ──
    {
        "name": "HEPH_PHASE_TIMEOUT",
        "type": int,
        "default": 14400,
        "description": "Per-phase subprocess timeout (s) in the loop runner.",
    },
    {
        "name": "HEPH_CI_POLL_MAX_WAIT",
        "type": int,
        "default": 600,
        "description": "Wall-clock seconds for the CI-driver poll loops.",
    },
    {
        "name": "HEPH_PR_MERGE_MAX_WAIT",
        "type": int,
        "default": 1800,
        "description": "Max wait (s) for PR merge completion after auto-merge.",
    },
    {
        "name": "HEPHAESTUS_RATE_GUARD",
        "type": bool,
        "default": True,
        "description": "Enable/disable the rate guard (1=on, 0=off).",
    },
    {
        "name": "HEPHAESTUS_RATE_GUARD_THRESHOLD",
        "type": int,
        "default": 200,
        "description": "Rate guard threshold before throttling.",
    },
    # ── GitHub / gh CLI ──
    {
        "name": "HEPH_GH_TIMEOUT",
        "type": int,
        "default": 120,
        "description": "Timeout (s) for individual gh CLI calls.",
    },
    {
        "name": "HEPHAESTUS_GH_GLOBAL_RATE",
        "type": float,
        "default": 10.0,
        "description": "Global gh calls/sec token-bucket rate.",
    },
    {
        "name": "HEPHAESTUS_GH_GLOBAL_BURST",
        "type": float,
        "default": 30.0,
        "description": "Global gh token-bucket burst capacity.",
    },
    {
        "name": "HEPHAESTUS_RATE_DIR",
        "type": str,
        "default": "",
        "description": "Directory for the global rate-limit state file.",
    },
    {
        "name": "GH_RATE_LIMIT_PER_SEC",
        "type": float,
        "default": 5.0,
        "description": "Per-thread gh CLI throttle rate.",
    },
    # ── Logging ──
    {
        "name": "HEPHAESTUS_LOG_FORMAT",
        "type": str,
        "default": "",
        "description": "Set to 'json' for structured JSON log output.",
    },
    # ── Repo / paths ──
    {
        "name": "HEPHAESTUS_REPO_ROOT",
        "type": str,
        "default": "",
        "description": "Override for the repo root directory.",
    },
    {
        "name": "HEPHAESTUS_PLUGIN_SKILLS_DIR",
        "type": str,
        "default": "",
        "description": "Override for the plugin skills directory.",
    },
    {
        "name": "HEPH_TRUNK_GITHASH",
        "type": str,
        "default": "",
        "description": "Trunk githash for session naming (set by loop runner).",
    },
    {
        "name": "HEPH_WORK_REPORT",
        "type": str,
        "default": "",
        "description": "Path to the work-report temp file (set by loop runner).",
    },
    # ── Subprocess timeouts ──
    {
        "name": "HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT",
        "type": int,
        "default": 10,
        "description": "Timeout (s) for local non-network queries.",
    },
    {
        "name": "HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT",
        "type": int,
        "default": 120,
        "description": "Timeout (s) for network-touching operations.",
    },
    # ── Codex ──
    {
        "name": "HEPH_CODEX_FINAL_MESSAGE_GRACE",
        "type": int,
        "default": 30,
        "description": "Grace period (s) for Codex final message.",
    },
]

for _spec in _REGISTRY:
    env.register(**_spec)

# Keep the list accessible for documentation generation.
del _REGISTRY
