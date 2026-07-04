"""Role handlers: the HMAS agentic roles a mesh worker can serve.

Roles are addressed by NAME crossed with domain (ADR-013 §1). The registry
maps ``(domain, role)`` to a handler factory; deeper hierarchy roles register
here as they are instantiated.
"""

from __future__ import annotations

from collections.abc import Callable

from hephaestus.automation.mesh.worker import RoleHandler


def _task_agent() -> RoleHandler:
    from hephaestus.automation.mesh.roles.task_agent import TaskAgentHandler

    return TaskAgentHandler()


def _chief_architect() -> RoleHandler:
    from hephaestus.automation.mesh.roles.chief_architect import ChiefArchitectHandler

    return ChiefArchitectHandler()


def _research() -> RoleHandler:
    from hephaestus.automation.mesh.roles.research import ResearchHandler

    return ResearchHandler()


def _coordination() -> RoleHandler:
    from hephaestus.automation.mesh.roles.coordination import CoordinationHandler

    return CoordinationHandler()


#: (domain, role) → handler factory. Lazy so importing the registry does not
#: pull the full automation stack.
ROLE_HANDLERS: dict[tuple[str, str], Callable[[], RoleHandler]] = {
    ("pipeline", "task-agent"): _task_agent,
    ("pipeline", "chief-architect"): _chief_architect,
    ("research", "chief-architect"): _research,
    # L1/L2 nodes are coordination points (ADR-013 §10): claim, record the
    # assignment, complete — Agamemnon's graph walk does the delegation.
    ("pipeline", "component-lead"): _coordination,
    ("pipeline", "module-lead"): _coordination,
}


def resolve_handler(domain: str, role: str) -> RoleHandler:
    """Return a handler instance for ``(domain, role)``.

    Raises:
        KeyError: When no handler is registered for the pair.

    """
    try:
        factory = ROLE_HANDLERS[(domain, role)]
    except KeyError:
        known = ", ".join(f"{d}.{r}" for d, r in sorted(ROLE_HANDLERS))
        raise KeyError(f"no handler for {domain}.{role} (known: {known})") from None
    return factory()
