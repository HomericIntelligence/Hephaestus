"""Resolve the tool and model settings for each active CLI role."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable, Sequence
from typing import Any


def resolve_role_agents(
    args: Namespace,
    roles: Sequence[str],
    *,
    resolver: Callable[..., str],
) -> tuple[str, dict[str, str]]:
    """Validate each selected tool with only the models that it will use."""
    resolved: dict[str, str] = {}
    cache: dict[tuple[str | None, tuple[str, ...]], str] = {}
    global_agent = getattr(args, "agent", None)
    detected_agent: str | None = None
    for role in roles:
        selected = getattr(args, f"{role}_agent", None) or global_agent or detected_agent
        references: tuple[str, ...] = (
            getattr(args, f"{role}_model", "") or getattr(args, "model", ""),
        )
        fallback = getattr(args, "fallback_model", "")
        if fallback:
            references += (fallback,)
        key = (selected, references)
        if key not in cache:
            options: dict[str, Any] = {
                "disable_pi_automation": getattr(args, "disable_pi_automation", False),
                "auth_status_timeout": getattr(args, "auth_status_timeout", 10),
                "pi_isolation_adapter": getattr(args, "pi_isolation_adapter", None),
                "pi_dir": getattr(args, "pi_dir", None),
                "model_references": references,
            }
            cache[key] = resolver(selected, **options)
        resolved[role] = cache[key]
        if selected is None:
            detected_agent = resolved[role]
            cache[(detected_agent, references)] = detected_agent
    return global_agent or detected_agent or "", resolved
