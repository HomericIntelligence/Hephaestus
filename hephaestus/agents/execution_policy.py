"""Immutable, least-privilege execution policies for Pi automation.

The policy is deliberately provider-neutral data owned by the library layer.
Automation callers describe *what* is being done with an
:class:`ExecutionRequest`; the Pi runtime resolves that request before it
starts a broker or an isolated process.  A caller-supplied sandbox or tool
allowlist therefore cannot widen Pi's operating-system boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class AgentRole(StrEnum):
    """The automation role that owns an agent invocation."""

    ADVISOR = "advisor"
    PLANNER = "planner"
    PLAN_REVIEWER = "plan_reviewer"
    IMPLEMENTER = "implementer"
    PR_REVIEWER = "pr_reviewer"
    LEARNER = "learner"


class AgentOperation(StrEnum):
    """An operation with a separately reviewable privilege boundary."""

    ADVISE = "advise"
    PLAN = "plan"
    AMEND = "amend"
    COMPACT = "compact"
    PLAN_REVIEW = "plan_review"
    IMPLEMENT_INSPECT = "implement_inspect"
    IMPLEMENT = "implement"
    TEST_FIX = "test_fix"
    ADDRESS_REVIEW = "address_review"
    GIT_MESSAGE = "git_message"
    AUDIT_REVIEW = "audit_review"
    PR_REVIEW = "pr_review"
    REVIEW_VALIDATE = "review_validate"
    COMMENT_CLASSIFY = "comment_classify"
    LEARN = "learn"


class SessionLifecycle(StrEnum):
    """Whether an operation starts, resumes, or avoids a persisted session."""

    ONE_SHOT = "one_shot"
    START_NEW = "start_new"
    RESUME_REQUIRED = "resume_required"


class FilesystemMode(StrEnum):
    """External-isolation mount layouts available to an execution policy."""

    CHECKOUT_RO = "checkout_ro"
    KNOWLEDGE_RO = "knowledge_ro"
    WORKTREE_RW = "worktree_rw"
    MNEMOSYNE_RW = "mnemosyne_rw"
    SESSION_ONLY = "session_only"


class NetworkMode(StrEnum):
    """The only network paths available inside Pi's namespace."""

    PROVIDER_RELAY = "provider_relay"
    CONSTRAINED_WEB_RELAY = "constrained_web_relay"


class ExecutionPolicyError(ValueError):
    """A requested Pi operation is unsupported or would widen a capability."""


@dataclass(frozen=True)
class ExecutionRequest:
    """An operation request supplied by a pipeline or direct runtime caller."""

    role: AgentRole
    operation: AgentOperation
    lifecycle: SessionLifecycle


@dataclass(frozen=True)
class ExecutionPolicy:
    """The complete, immutable grants for a role and operation.

    ``permitted_lifecycles`` belongs to the policy rather than to individual
    rows so direct one-shot PR review and queue-based new/resumed review use
    the same reviewed capability entry.
    """

    role: AgentRole
    operation: AgentOperation
    permitted_lifecycles: frozenset[SessionLifecycle]
    filesystem: FilesystemMode
    builtins: frozenset[str]
    skills: frozenset[str]
    subagent: bool
    network: NetworkMode


_READ: Final = frozenset({"read", "grep", "find", "ls"})
_READ_SHELL: Final = _READ | {"bash"}
_WRITE: Final = _READ_SHELL | {"write", "edit"}
_POLICIES: Final[dict[tuple[AgentRole, AgentOperation], ExecutionPolicy]] = {
    (AgentRole.ADVISOR, AgentOperation.ADVISE): ExecutionPolicy(
        AgentRole.ADVISOR,
        AgentOperation.ADVISE,
        frozenset({SessionLifecycle.ONE_SHOT}),
        FilesystemMode.KNOWLEDGE_RO,
        _READ_SHELL,
        frozenset({"athena:advise"}),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.PLANNER, AgentOperation.PLAN): ExecutionPolicy(
        AgentRole.PLANNER,
        AgentOperation.PLAN,
        frozenset({SessionLifecycle.START_NEW}),
        FilesystemMode.CHECKOUT_RO,
        _READ,
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.PLANNER, AgentOperation.AMEND): ExecutionPolicy(
        AgentRole.PLANNER,
        AgentOperation.AMEND,
        frozenset({SessionLifecycle.RESUME_REQUIRED}),
        FilesystemMode.CHECKOUT_RO,
        _READ,
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.PLANNER, AgentOperation.COMPACT): ExecutionPolicy(
        AgentRole.PLANNER,
        AgentOperation.COMPACT,
        frozenset({SessionLifecycle.RESUME_REQUIRED}),
        FilesystemMode.SESSION_ONLY,
        frozenset(),
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.PLAN_REVIEWER, AgentOperation.PLAN_REVIEW): ExecutionPolicy(
        AgentRole.PLAN_REVIEWER,
        AgentOperation.PLAN_REVIEW,
        frozenset({SessionLifecycle.ONE_SHOT}),
        FilesystemMode.CHECKOUT_RO,
        _READ,
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.IMPLEMENTER, AgentOperation.IMPLEMENT_INSPECT): ExecutionPolicy(
        AgentRole.IMPLEMENTER,
        AgentOperation.IMPLEMENT_INSPECT,
        frozenset({SessionLifecycle.START_NEW, SessionLifecycle.RESUME_REQUIRED}),
        FilesystemMode.CHECKOUT_RO,
        _READ,
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.IMPLEMENTER, AgentOperation.IMPLEMENT): ExecutionPolicy(
        AgentRole.IMPLEMENTER,
        AgentOperation.IMPLEMENT,
        frozenset({SessionLifecycle.START_NEW, SessionLifecycle.RESUME_REQUIRED}),
        FilesystemMode.WORKTREE_RW,
        _WRITE,
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.IMPLEMENTER, AgentOperation.TEST_FIX): ExecutionPolicy(
        AgentRole.IMPLEMENTER,
        AgentOperation.TEST_FIX,
        frozenset({SessionLifecycle.RESUME_REQUIRED}),
        FilesystemMode.WORKTREE_RW,
        _WRITE,
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.IMPLEMENTER, AgentOperation.ADDRESS_REVIEW): ExecutionPolicy(
        AgentRole.IMPLEMENTER,
        AgentOperation.ADDRESS_REVIEW,
        frozenset({SessionLifecycle.START_NEW, SessionLifecycle.RESUME_REQUIRED}),
        FilesystemMode.WORKTREE_RW,
        _WRITE,
        frozenset(),
        # Pi delegation stays unavailable until the isolation broker can
        # resolve and enforce each child request.  A provider-visible
        # ``subagent`` tool alone cannot apply ``intersect_child_policy``.
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.IMPLEMENTER, AgentOperation.GIT_MESSAGE): ExecutionPolicy(
        AgentRole.IMPLEMENTER,
        AgentOperation.GIT_MESSAGE,
        frozenset({SessionLifecycle.ONE_SHOT}),
        FilesystemMode.CHECKOUT_RO,
        _READ,
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.IMPLEMENTER, AgentOperation.COMPACT): ExecutionPolicy(
        AgentRole.IMPLEMENTER,
        AgentOperation.COMPACT,
        frozenset({SessionLifecycle.RESUME_REQUIRED}),
        FilesystemMode.SESSION_ONLY,
        frozenset(),
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.PR_REVIEWER, AgentOperation.AUDIT_REVIEW): ExecutionPolicy(
        AgentRole.PR_REVIEWER,
        AgentOperation.AUDIT_REVIEW,
        frozenset({SessionLifecycle.ONE_SHOT}),
        FilesystemMode.CHECKOUT_RO,
        _READ,
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.PR_REVIEWER, AgentOperation.PR_REVIEW): ExecutionPolicy(
        AgentRole.PR_REVIEWER,
        AgentOperation.PR_REVIEW,
        frozenset(
            {
                SessionLifecycle.ONE_SHOT,
                SessionLifecycle.START_NEW,
                SessionLifecycle.RESUME_REQUIRED,
            }
        ),
        FilesystemMode.CHECKOUT_RO,
        _READ_SHELL,
        frozenset({"athena:pr-review"}),
        # See ADDRESS_REVIEW: do not advertise unenforceable delegation.
        False,
        NetworkMode.CONSTRAINED_WEB_RELAY,
    ),
    (AgentRole.PR_REVIEWER, AgentOperation.REVIEW_VALIDATE): ExecutionPolicy(
        AgentRole.PR_REVIEWER,
        AgentOperation.REVIEW_VALIDATE,
        frozenset({SessionLifecycle.START_NEW, SessionLifecycle.RESUME_REQUIRED}),
        FilesystemMode.CHECKOUT_RO,
        _READ,
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.PR_REVIEWER, AgentOperation.COMMENT_CLASSIFY): ExecutionPolicy(
        AgentRole.PR_REVIEWER,
        AgentOperation.COMMENT_CLASSIFY,
        frozenset({SessionLifecycle.ONE_SHOT}),
        FilesystemMode.CHECKOUT_RO,
        _READ,
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.PR_REVIEWER, AgentOperation.COMPACT): ExecutionPolicy(
        AgentRole.PR_REVIEWER,
        AgentOperation.COMPACT,
        frozenset({SessionLifecycle.RESUME_REQUIRED}),
        FilesystemMode.SESSION_ONLY,
        frozenset(),
        frozenset(),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
    (AgentRole.LEARNER, AgentOperation.LEARN): ExecutionPolicy(
        AgentRole.LEARNER,
        AgentOperation.LEARN,
        frozenset({SessionLifecycle.START_NEW}),
        FilesystemMode.MNEMOSYNE_RW,
        _WRITE,
        frozenset({"athena:learn"}),
        False,
        NetworkMode.PROVIDER_RELAY,
    ),
}

POLICIES: Final[Mapping[tuple[AgentRole, AgentOperation], ExecutionPolicy]] = MappingProxyType(
    _POLICIES
)


def resolve_policy(request: ExecutionRequest) -> ExecutionPolicy:
    """Resolve a complete Pi policy, rejecting unknown or invalid requests.

    Args:
        request: Provider-neutral role, operation, and lifecycle request.

    Returns:
        The immutable policy selected by the role and operation.

    Raises:
        ExecutionPolicyError: If the request is unknown or its lifecycle is
            not explicitly permitted.

    """
    try:
        policy = POLICIES[(request.role, request.operation)]
    except KeyError as exc:
        raise ExecutionPolicyError(
            f"Pi operation {request.role.value}/{request.operation.value} is unsupported"
        ) from exc
    if request.lifecycle not in policy.permitted_lifecycles:
        raise ExecutionPolicyError(
            "Pi lifecycle "
            f"{request.lifecycle.value!r} is not permitted for "
            f"{request.role.value}/{request.operation.value}"
        )
    return policy


def _filesystem_intersection(parent: FilesystemMode, requested: FilesystemMode) -> FilesystemMode:
    """Return a child filesystem mode only when it is no broader than parent."""
    if parent is FilesystemMode.WORKTREE_RW and requested is FilesystemMode.CHECKOUT_RO:
        return requested
    if parent is requested:
        return parent
    raise ExecutionPolicyError(
        f"Pi child filesystem {requested.value!r} would widen parent {parent.value!r}"
    )


def intersect_child_policy(parent: ExecutionPolicy, requested: ExecutionPolicy) -> ExecutionPolicy:
    """Derive a non-recursive child policy by intersecting every capability.

    The child has no delegation grant, cannot regain a parent-absent skill or
    web relay, and may only reduce a writable worktree to a read-only checkout.
    """
    if not parent.subagent:
        raise ExecutionPolicyError("Pi policy does not permit subagent delegation")
    filesystem = _filesystem_intersection(parent.filesystem, requested.filesystem)
    network = (
        NetworkMode.CONSTRAINED_WEB_RELAY
        if (
            parent.network is NetworkMode.CONSTRAINED_WEB_RELAY
            and requested.network is NetworkMode.CONSTRAINED_WEB_RELAY
        )
        else NetworkMode.PROVIDER_RELAY
    )
    return ExecutionPolicy(
        role=requested.role,
        operation=requested.operation,
        permitted_lifecycles=requested.permitted_lifecycles,
        filesystem=filesystem,
        builtins=parent.builtins & requested.builtins,
        skills=parent.skills & requested.skills,
        subagent=False,
        network=network,
    )
