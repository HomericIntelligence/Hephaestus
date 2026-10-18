"""Pure contracts and Git plan builders for the protected rebase flow.

This module holds no subprocess and no Git execution.  The worker boundary
owns every Git call; this module supplies the validated argv plan, the bridge
commit message record, the target eligibility proof, and the opaque writer
capability registry that binds a protected mutation to a coordinator-proven
implementation writer allocation.
"""

from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from hephaestus.utils.git import _is_full_commit_sha

BRIDGE_MESSAGE_SUBJECT = "Protected rebase bridge"
BRIDGE_FORMAT = 1
MAX_BRIDGE_CYCLE = 8

TRAILER_FORMAT = "Protected-Rebase-Format"
TRAILER_ISSUE = "Protected-Rebase-Issue"
TRAILER_PR = "Protected-Rebase-PR"
TRAILER_CYCLE = "Protected-Rebase-Cycle"
TRAILER_BASE = "Protected-Rebase-Base"
TRAILER_PREVIOUS_HEAD = "Protected-Rebase-Previous-Head"
TRAILER_LOGICAL_TIP = "Protected-Rebase-Logical-Tip"
TRAILER_SIGNOFF = "Signed-off-by"

# Merge-wait cycle outcomes introduced by the protected rebase flow.
PROTECTED_REBASE_REQUIRED = "protected_rebase_required"
PROTECTED_REBASE_TARGET_UNVERIFIED = "protected_rebase_target_unverified"
PROTECTED_REBASE_FORK_HEAD = "protected_rebase_fork_head"
MERGE_ATTEMPT_CONFLICT = "merge_attempt_conflict"
MERGE_ATTEMPT_AMBIGUOUS = "merge_attempt_ambiguous"

# Worker and implementation-stage terminal reasons.
PROTECTED_REBASE_WORKTREE_MISSING = "protected_rebase_worktree_missing"
PROTECTED_REBASE_CAPABILITY_MISMATCH = "protected_rebase_capability_mismatch"
PROTECTED_REBASE_WRITER_UNPROVEN = "protected_rebase_writer_unproven"
PROTECTED_REBASE_CONFLICT = "protected_rebase_conflict"
PROTECTED_REBASE_CYCLES_EXHAUSTED = "protected_rebase_cycles_exhausted"
PROTECTED_REBASE_COMMIT_INVALID = "protected_rebase_commit_invalid"
PROTECTED_REBASE_BRIDGE_INVALID = "protected_rebase_bridge_invalid"
PROTECTED_REBASE_DRIFT = "protected_rebase_drift"


class ProtectedRebaseError(RuntimeError):
    """Base class for fail-closed protected rebase errors."""


class ProtectedRebaseValidationError(ProtectedRebaseError):
    """Raised when target facts, a bridge record, or a plan input is malformed."""


class ProtectedRebaseCapabilityError(ProtectedRebaseError):
    """Raised when a writer capability is missing or does not match."""


class ProtectedRebaseBridgeError(ProtectedRebaseValidationError):
    """Raised when a prior bridge message is absent or not the prior cycle."""


class WriterAllocationOrigin(StrEnum):
    """How a worktree writer allocation came into existence."""

    NEWLY_CREATED = "newly_created"
    DIRECT_ABSENT_RESERVED = "direct_absent_reserved"
    ADOPTED = "adopted"
    REUSED = "reused"


CAPABILITY_ORIGINS = frozenset(
    {
        WriterAllocationOrigin.NEWLY_CREATED,
        WriterAllocationOrigin.DIRECT_ABSENT_RESERVED,
    }
)

_CAPABILITY_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class WriterCapability:
    """Opaque, process-local capability token for a proven writer allocation."""

    token: str

    def __post_init__(self) -> None:
        """Validate the opaque token format without disclosing its value."""
        if not _CAPABILITY_TOKEN_PATTERN.match(self.token):
            raise ProtectedRebaseValidationError("writer capability token must be 64 hex")


@dataclass(frozen=True)
class ProtectedWriterCapabilityRecord:
    """Immutable binding of a capability to one proven writer allocation.

    The ``item`` field is a strong reference to the bound work item so the
    item cannot be collected and its identity recycled while the capability
    is registered.
    """

    capability: WriterCapability
    repository: str
    issue_number: int
    branch: str
    worktree: str
    origin: WriterAllocationOrigin
    item: Any


_WRITER_CAPABILITIES: dict[str, ProtectedWriterCapabilityRecord] = {}
_WRITER_CAPABILITIES_LOCK = threading.Lock()


def register_writer_capability(
    *,
    repository: str,
    issue_number: int,
    branch: str,
    worktree: str,
    origin: WriterAllocationOrigin,
    item: Any,
) -> ProtectedWriterCapabilityRecord:
    """Mint and register one opaque capability for a proven writer allocation."""
    if origin not in CAPABILITY_ORIGINS:
        raise ProtectedRebaseCapabilityError(
            f"writer allocation origin {origin!r} cannot receive a protected capability"
        )
    if not repository or not branch or not worktree:
        raise ProtectedRebaseValidationError(
            "writer capability requires a repository, branch, and resolved worktree"
        )
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
        raise ProtectedRebaseValidationError("writer capability requires a positive issue number")
    record = ProtectedWriterCapabilityRecord(
        capability=WriterCapability(secrets.token_hex(32)),
        repository=repository,
        issue_number=issue_number,
        branch=branch,
        worktree=worktree,
        origin=origin,
        item=item,
    )
    with _WRITER_CAPABILITIES_LOCK:
        _WRITER_CAPABILITIES[record.capability.token] = record
    return record


def pop_writer_capability(token: str) -> ProtectedWriterCapabilityRecord | None:
    """Remove a registered capability at the terminal sink, returning it."""
    with _WRITER_CAPABILITIES_LOCK:
        return _WRITER_CAPABILITIES.pop(token, None)


def find_writer_capability_token(
    *,
    repository: str,
    issue_number: int,
    branch: str,
    worktree: str,
) -> str | None:
    """Return the registered token for one exact writer identity, or ``None``.

    Coordinator-run code uses this lookup to bind a protected mutation job
    to the capability it already proved.  A missing or mismatched identity
    reports ``None``; no token is ever minted here.
    """
    with _WRITER_CAPABILITIES_LOCK:
        for record in _WRITER_CAPABILITIES.values():
            if (
                record.repository == repository
                and record.issue_number == issue_number
                and record.branch == branch
                and record.worktree == worktree
            ):
                return record.capability.token
    return None


def validate_protected_writer_capability(
    token: str,
    *,
    repository: str,
    issue_number: int,
    branch: str,
    worktree: str,
) -> ProtectedWriterCapabilityRecord:
    """Revalidate a durable registered capability before a protected mutation.

    Every field must match the registered record; a missing or mismatched
    capability fails closed.
    """
    if not isinstance(token, str) or not _CAPABILITY_TOKEN_PATTERN.match(token):
        raise ProtectedRebaseCapabilityError("writer capability token must be 64 hex")
    with _WRITER_CAPABILITIES_LOCK:
        record = _WRITER_CAPABILITIES.get(token)
    if record is None:
        raise ProtectedRebaseCapabilityError("writer capability is not registered")
    if (
        record.repository != repository
        or record.issue_number != issue_number
        or record.branch != branch
        or record.worktree != worktree
    ):
        raise ProtectedRebaseCapabilityError(
            "writer capability does not match the protected target"
        )
    return record


@dataclass(frozen=True)
class ProtectedRebaseTarget:
    """Immutable GitHub-proven identity of one protected rebase target."""

    repository: str
    pr_number: int
    branch: str
    head_sha: str
    base_sha: str

    def __post_init__(self) -> None:
        """Validate the immutable target identity."""
        if not self.repository or "/" not in self.repository:
            raise ProtectedRebaseValidationError("target repository must be owner/name")
        if (
            isinstance(self.pr_number, bool)
            or not isinstance(self.pr_number, int)
            or (self.pr_number <= 0)
        ):
            raise ProtectedRebaseValidationError("target pr_number must be positive")
        if not self.branch:
            raise ProtectedRebaseValidationError("target branch must be non-empty")
        if not _is_full_commit_sha(self.head_sha):
            raise ProtectedRebaseValidationError("target head_sha must be a full commit SHA")
        if not _is_full_commit_sha(self.base_sha):
            raise ProtectedRebaseValidationError("target base_sha must be a full commit SHA")


def repository_owner_name(repository: str) -> tuple[str, str] | None:
    """Split an owner/name repository identity into its two parts."""
    if not isinstance(repository, str):
        return None
    parts = repository.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def validate_protected_rebase_target(
    readiness: object,
    *,
    repository: str,
    pr_number: int,
    expected_head_sha: str,
    default_branch: str,
) -> ProtectedRebaseTarget | str:
    """Prove a protected rebase target from extended readiness facts.

    Return the target, or the terminal rejection reason.  Surrounding
    admission proofs the open, unarmed, exclusive implementation-GO, and
    no-unresolved-threads facts; this validation owns the readiness-local
    facts.  A missing or malformed field makes the target unverified; a
    foreign head repository makes it a fork head.
    """
    if not isinstance(readiness, dict):
        return PROTECTED_REBASE_TARGET_UNVERIFIED
    if str(readiness.get("mergeStateStatus") or "").upper() != "BEHIND":
        return PROTECTED_REBASE_TARGET_UNVERIFIED
    if str(readiness.get("mergeable") or "").upper() != "MERGEABLE":
        return PROTECTED_REBASE_TARGET_UNVERIFIED
    head = readiness.get("headRefOid")
    if not isinstance(head, str) or head != expected_head_sha:
        return PROTECTED_REBASE_TARGET_UNVERIFIED
    if readiness.get("baseRefName") != default_branch:
        return PROTECTED_REBASE_TARGET_UNVERIFIED
    base = readiness.get("baseRefOid")
    if not _is_full_commit_sha(base):
        return PROTECTED_REBASE_TARGET_UNVERIFIED
    branch = readiness.get("headRefName")
    if not isinstance(branch, str) or not branch or branch == default_branch:
        return PROTECTED_REBASE_TARGET_UNVERIFIED
    identity = repository_owner_name(repository)
    if identity is None:
        return PROTECTED_REBASE_TARGET_UNVERIFIED
    head_repository = readiness.get("headRepository")
    head_owner = readiness.get("headRepositoryOwner")
    if not isinstance(head_repository, dict) or not isinstance(head_owner, dict):
        return PROTECTED_REBASE_TARGET_UNVERIFIED
    owner_name, repo_name = identity
    if head_repository.get("name") != repo_name:
        return PROTECTED_REBASE_FORK_HEAD
    if head_owner.get("login") != owner_name:
        return PROTECTED_REBASE_FORK_HEAD
    return ProtectedRebaseTarget(
        repository=repository,
        pr_number=pr_number,
        branch=branch,
        head_sha=head,
        base_sha=base,
    )


@dataclass(frozen=True)
class BridgeMetadata:
    """Immutable format-1 record carried by a signed bridge commit message."""

    issue_number: int
    pr_number: int
    cycle: int
    base_sha: str
    previous_head_sha: str
    logical_tip_sha: str
    signoff: str

    def __post_init__(self) -> None:
        """Validate the immutable bridge record."""
        for name, value in (
            ("issue_number", self.issue_number),
            ("pr_number", self.pr_number),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ProtectedRebaseValidationError(f"bridge {name} must be positive")
        if (
            isinstance(self.cycle, bool)
            or not isinstance(self.cycle, int)
            or not 1 <= self.cycle <= MAX_BRIDGE_CYCLE
        ):
            raise ProtectedRebaseValidationError("bridge cycle must be in 1..8")
        for sha_name, sha_value in (
            ("base_sha", self.base_sha),
            ("previous_head_sha", self.previous_head_sha),
            ("logical_tip_sha", self.logical_tip_sha),
        ):
            if not _is_full_commit_sha(sha_value):
                raise ProtectedRebaseValidationError(f"bridge {sha_name} must be a full commit SHA")
        if not self.signoff:
            raise ProtectedRebaseValidationError("bridge signoff must be non-empty")


def render_bridge_message(metadata: BridgeMetadata) -> str:
    """Render the deterministic format-1 bridge commit message."""
    return (
        f"{BRIDGE_MESSAGE_SUBJECT}\n"
        f"\n"
        f"{TRAILER_FORMAT}: {BRIDGE_FORMAT}\n"
        f"{TRAILER_ISSUE}: {metadata.issue_number}\n"
        f"{TRAILER_PR}: {metadata.pr_number}\n"
        f"{TRAILER_CYCLE}: {metadata.cycle}\n"
        f"{TRAILER_BASE}: {metadata.base_sha}\n"
        f"{TRAILER_PREVIOUS_HEAD}: {metadata.previous_head_sha}\n"
        f"{TRAILER_LOGICAL_TIP}: {metadata.logical_tip_sha}\n"
        f"{metadata.signoff}\n"
    )


def parse_bridge_metadata(message: str) -> BridgeMetadata:
    """Parse and validate a bridge commit message into its format-1 record."""
    if not isinstance(message, str) or not message:
        raise ProtectedRebaseValidationError("bridge message must be non-empty text")
    trailers: dict[str, str] = {}
    signoffs: list[str] = []
    for line in message.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == TRAILER_SIGNOFF:
            signoffs.append(line.strip())
        elif key in {
            TRAILER_FORMAT,
            TRAILER_ISSUE,
            TRAILER_PR,
            TRAILER_CYCLE,
            TRAILER_BASE,
            TRAILER_PREVIOUS_HEAD,
            TRAILER_LOGICAL_TIP,
        }:
            if key in trailers:
                raise ProtectedRebaseValidationError(f"duplicate bridge trailer {key}")
            trailers[key] = value
    if trailers.get(TRAILER_FORMAT) != str(BRIDGE_FORMAT):
        raise ProtectedRebaseValidationError("bridge format trailer is missing or unsupported")
    if len(signoffs) != 1:
        raise ProtectedRebaseValidationError("bridge message requires exactly one sign-off")
    try:
        issue_number = int(trailers[TRAILER_ISSUE])
        pr_number = int(trailers[TRAILER_PR])
        cycle = int(trailers[TRAILER_CYCLE])
    except (KeyError, ValueError) as exc:
        raise ProtectedRebaseValidationError(
            "bridge issue, PR, and cycle trailers must be integers"
        ) from exc
    return BridgeMetadata(
        issue_number=issue_number,
        pr_number=pr_number,
        cycle=cycle,
        base_sha=trailers.get(TRAILER_BASE, ""),
        previous_head_sha=trailers.get(TRAILER_PREVIOUS_HEAD, ""),
        logical_tip_sha=trailers.get(TRAILER_LOGICAL_TIP, ""),
        signoff=signoffs[0],
    )


def logical_head_for_cycle(
    cycle: int,
    *,
    previous_head_sha: str,
    prior_bridge_message: str | None,
) -> str:
    """Return the uniform logical head for one rebase cycle.

    Cycle 1 rebases from the previous published head.  Cycles 2 to 8 unwrap
    and validate the prior bridge's signed metadata and rebase from its
    recorded logical tip.
    """
    if isinstance(cycle, bool) or not isinstance(cycle, int) or not 1 <= cycle <= MAX_BRIDGE_CYCLE:
        raise ProtectedRebaseValidationError("rebase cycle must be in 1..8")
    if not _is_full_commit_sha(previous_head_sha):
        raise ProtectedRebaseValidationError("previous head must be a full commit SHA")
    if cycle == 1:
        return previous_head_sha
    if not isinstance(prior_bridge_message, str) or not prior_bridge_message:
        raise ProtectedRebaseBridgeError("prior bridge message is unavailable")
    metadata = parse_bridge_metadata(prior_bridge_message)
    if metadata.cycle != cycle - 1:
        raise ProtectedRebaseBridgeError("prior bridge message is not the prior cycle")
    return metadata.logical_tip_sha


# Runtime-derived plan slots.  Each marker occupies one whole argv token, so
# substitution is exact and cannot alter a neighboring flag.
MERGE_BASE_SLOT = "@merge-base@"
NEW_TIP_SLOT = "@new-tip@"
BRIDGE_SLOT = "@bridge@"
MESSAGE_FILE_SLOT = "@message-file@"


def protected_rebase_git_plan(
    *,
    logical_head_sha: str,
    new_base_sha: str,
    previous_head_sha: str,
    branch: str,
    default_branch: str,
) -> tuple[tuple[str, ...], ...]:
    """Build the ordered ordinary-Git argv plan for a protected rebase cycle.

    The plan rebases the logical range onto the exact current base with
    signed commits, requires the replayed range to contain no merge commit,
    builds the signed fast-forward bridge commit, re-reads the two remote
    refs to catch drift, proves both ancestries, and pushes only the bridge
    SHA through an ordinary push.  Runtime-derived values occupy marked
    slots; the worker substitutes them after each read-only observation.
    """
    for name, value in (
        ("logical_head_sha", logical_head_sha),
        ("new_base_sha", new_base_sha),
        ("previous_head_sha", previous_head_sha),
    ):
        if not _is_full_commit_sha(value):
            raise ProtectedRebaseValidationError(f"{name} must be a full commit SHA")
    if not branch or branch == default_branch:
        raise ProtectedRebaseValidationError("plan branch must differ from the default branch")
    if not default_branch:
        raise ProtectedRebaseValidationError("plan default branch must be non-empty")
    replay_range = f"{MERGE_BASE_SLOT}..HEAD"
    return (
        ("git", "merge-base", logical_head_sha, new_base_sha),
        (
            "git",
            "rebase",
            "--gpg-sign",
            "--onto",
            new_base_sha,
            MERGE_BASE_SLOT,
            logical_head_sha,
        ),
        ("git", "rev-list", "--merges", replay_range),
        ("git", "rev-list", replay_range),
        ("git", "rev-parse", "HEAD"),
        (
            "git",
            "commit-tree",
            f"{NEW_TIP_SLOT}^{{tree}}",
            "-p",
            NEW_TIP_SLOT,
            "-p",
            previous_head_sha,
            "-S",
            "-F",
            MESSAGE_FILE_SLOT,
        ),
        (
            "git",
            "ls-remote",
            "origin",
            f"refs/heads/{default_branch}",
            f"refs/heads/{branch}",
        ),
        ("git", "merge-base", "--is-ancestor", previous_head_sha, BRIDGE_SLOT),
        ("git", "merge-base", "--is-ancestor", new_base_sha, BRIDGE_SLOT),
        ("git", "push", "origin", f"{BRIDGE_SLOT}:refs/heads/{branch}"),
    )


def substitute_plan_slots(argv: tuple[str, ...], slots: dict[str, str]) -> tuple[str, ...]:
    """Substitute runtime values into one plan step.

    A marker can carry a Git suffix such as ``^{tree}``, so substitution
    replaces every marker occurrence inside each token.  Markers are unique
    bracketed names that cannot collide with ordinary flags.
    """
    resolved: list[str] = []
    for token in argv:
        for marker, value in slots.items():
            token = token.replace(marker, value)
        resolved.append(token)
    return tuple(resolved)
