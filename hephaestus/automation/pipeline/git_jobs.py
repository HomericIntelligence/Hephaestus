"""Provider-neutral Git job specifications for pipeline workers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Self

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding, WorkspaceKind
from hephaestus.automation.worktree_snapshot import (
    DIRTY_SNAPSHOT_CHANGED_FILE_MAX as DIRTY_SNAPSHOT_CHANGED_FILE_MAX,
    DIRTY_SNAPSHOT_CONTENT_MAX_BYTES as DIRTY_SNAPSHOT_CONTENT_MAX_BYTES,
    DIRTY_SNAPSHOT_GIT_MAX_BYTES as DIRTY_SNAPSHOT_GIT_MAX_BYTES,
)

from .host_capabilities import CapabilityRequestTarget
from .repository_validation_preparation import RepositoryValidationSourceRequest
from .scope_retraction import is_safe_scope_retraction_path

FIRST_PUBLICATION_CHECK_ARGV = ("bash", "scripts/run_ci_local.sh", "all", "--check-only")


@dataclass(frozen=True, slots=True)
class FirstPublicationRecord:
    """Retain publication facts without granting source or callback authority."""

    operation_id: str
    repository: str
    scheduler_repository: str
    issue_number: int
    branch: str
    destination: str
    workspace: WorkspaceBinding
    tree_sha: str
    scope_base_sha: str
    allowed_paths: tuple[str, ...] | None
    phase: str
    schema_version: int = 1
    expected_remote: str = "absent"

    def __post_init__(self) -> None:
        """Validate the immutable operation without filesystem access."""
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.operation_id) is not str
            or re.fullmatch(r"[0-9a-f]{32}", self.operation_id) is None
            or type(self.repository) is not str
            or len(self.repository) > 256
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None
            or type(self.scheduler_repository) is not str
            or self.scheduler_repository
            not in {self.repository, self.repository.rsplit("/", 1)[-1]}
            or type(self.issue_number) is not int
            or self.issue_number <= 0
            or not _recovery_branch_valid(self.branch)
            or self.destination != f"https://github.com/{self.repository}.git"
            or type(self.phase) is not str
            or self.phase not in {"publication_intent", "complete"}
            or self.expected_remote != "absent"
            or not _recovery_sha_valid(self.tree_sha)
            or not _recovery_sha_valid(self.scope_base_sha)
        ):
            raise ValueError("The first-publication identity is invalid.")
        workspace = self.workspace
        if (
            type(workspace) is not WorkspaceBinding
            or _strict_recovery_workspace(workspace.to_dict()) != workspace
            or workspace.schema_version != 1
            or workspace.kind is not WorkspaceKind.SOURCE
            or workspace.lane is not SourceLane.IMPLEMENTATION
            or workspace.detached
            or workspace.dirty_claim is not None
            or workspace.item_number != self.issue_number
            or workspace.repository not in {self.repository, self.scheduler_repository}
            or not _recovery_sha_valid(workspace.revision)
            or not workspace.cwd.is_absolute()
            or workspace.reusable_root is None
            or not workspace.reusable_root.is_absolute()
            or not workspace.ownership_key
            or workspace.generation is None
            or workspace.generation < 0
        ):
            raise ValueError("The first-publication source binding is invalid.")
        if self.allowed_paths is not None and (
            type(self.allowed_paths) is not tuple
            or not self.allowed_paths
            or len(self.allowed_paths) > 4096
            or any(
                type(path) is not str or len(path) > 4096 or not is_safe_scope_retraction_path(path)
                for path in self.allowed_paths
            )
            or tuple(sorted(set(self.allowed_paths))) != self.allowed_paths
        ):
            raise ValueError("The first-publication scope is invalid.")

    def to_dict(self) -> dict[str, Any]:
        """Return exact JSON data without credentials or executable inputs."""
        result = {entry.name: getattr(self, entry.name) for entry in fields(self)}
        result["workspace"] = self.workspace.to_dict()
        result["allowed_paths"] = (
            list(self.allowed_paths) if self.allowed_paths is not None else None
        )
        return result

    @classmethod
    def from_dict(cls, payload: object) -> Self:
        """Reject unknown fields and scalar coercion in retained state."""
        if type(payload) is not dict or set(payload) != {entry.name for entry in fields(cls)}:
            raise ValueError("The first-publication record schema is invalid.")
        values = dict(payload)
        paths = values["allowed_paths"]
        if paths is not None and type(paths) is not list:
            raise ValueError("The first-publication scope schema is invalid.")
        values["allowed_paths"] = tuple(paths) if paths is not None else None
        values["workspace"] = _strict_recovery_workspace(values["workspace"])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class PendingRebaseRecord:
    """Retain unfinished Git work without granting source or review authority."""

    request: CapabilityRequestTarget
    operation: str
    scheduler_repository: str
    branch: str
    destination: str
    publication_mode: str
    remote_head_sha: str | None
    target_base_sha: str
    policy_name: str | None
    phase: str
    resulting_workspace: WorkspaceBinding | None = None
    resulting_tree_sha: str | None = None
    target_base_branch: str = "main"
    schema_version: int = 1
    restored_workspace: WorkspaceBinding | None = None
    restored_tree_sha: str | None = None

    def __post_init__(self) -> None:
        """Reject incomplete identities without filesystem or Git access."""
        if type(self.request) is not CapabilityRequestTarget:
            raise ValueError("The pending rebase request type is invalid.")
        replace(self.request)
        repository = self.request.repository
        if (
            type(self.schema_version) is not int
            or self.schema_version not in {1, 2}
            or self.request.phase != "rebase"
            or "/" not in repository
            or self.operation not in {"rebase", "continue_rebase"}
            or self.scheduler_repository not in {repository, repository.rsplit("/", 1)[-1]}
            or self.request.workspace.repository not in {repository, self.scheduler_repository}
            or self.destination != f"https://github.com/{repository}.git"
            or self.target_base_branch != "main"
            or self.policy_name not in {None, "hephaestus-adr-v1", "mnemosyne-current-head-v1"}
            or self.phase
            not in {"intent", "pending_validation", "publication_intent", "complete", "aborted"}
        ):
            raise ValueError("The pending rebase identity is invalid.")
        if not _recovery_branch_valid(self.branch) or not _recovery_sha_valid(self.target_base_sha):
            raise ValueError("The pending rebase branch or base is invalid.")
        if self.publication_mode == "existing":
            if not _recovery_sha_valid(self.remote_head_sha):
                raise ValueError("The pending rebase remote lease is invalid.")
        elif self.publication_mode not in {"none", "absent"} or self.remote_head_sha is not None:
            raise ValueError("The pending rebase publication mode is invalid.")
        if self.publication_mode == "none" and self.phase == "publication_intent":
            raise ValueError("The pending rebase does not permit publication.")
        _validate_abort_restoration(self)
        _validate_recovery_result(self)

    def to_dict(self) -> dict[str, Any]:
        """Return a closed JSON representation without executable inputs."""
        request = {entry.name: getattr(self.request, entry.name) for entry in fields(self.request)}
        request.update(
            repository_root=str(self.request.repository_root),
            checkout_path=str(self.request.checkout_path),
            workspace=self.request.workspace.to_dict(),
        )
        result = {entry.name: getattr(self, entry.name) for entry in fields(self)}
        result["request"] = request
        result["resulting_workspace"] = (
            self.resulting_workspace.to_dict() if self.resulting_workspace is not None else None
        )
        if self.schema_version == 1:
            result.pop("restored_workspace")
            result.pop("restored_tree_sha")
        else:
            result["restored_workspace"] = (
                self.restored_workspace.to_dict() if self.restored_workspace is not None else None
            )
        return result

    @classmethod
    def from_dict(cls, payload: object) -> Self:
        """Parse one exact schema and validate all nested source identities."""
        if type(payload) is not dict or type(payload.get("schema_version")) is not int:
            raise ValueError("The pending rebase record schema is invalid.")
        version = payload["schema_version"]
        keys = {entry.name for entry in fields(cls)}
        if version == 1:
            keys -= {"restored_workspace", "restored_tree_sha"}
        if version not in {1, 2} or set(payload) != keys:
            raise ValueError("The pending rebase record schema is invalid.")
        raw_request = payload["request"]
        if type(raw_request) is not dict or set(raw_request) != {
            entry.name for entry in fields(CapabilityRequestTarget)
        }:
            raise ValueError("The pending rebase request schema is invalid.")
        values = dict(payload)
        request = dict(raw_request)
        for key in ("repository_root", "checkout_path"):
            if type(request[key]) is not str:
                raise ValueError("The pending rebase path is invalid.")
            request[key] = Path(request[key])
        try:
            request["workspace"] = _strict_recovery_workspace(request["workspace"])
            values["request"] = CapabilityRequestTarget(**request)
            raw_result = values["resulting_workspace"]
            values["resulting_workspace"] = (
                None if raw_result is None else _strict_recovery_workspace(raw_result)
            )
            if version == 2:
                restored = values["restored_workspace"]
                values["restored_workspace"] = (
                    None if restored is None else _strict_recovery_workspace(restored)
                )
            return cls(**values)
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("The pending rebase nested identity is invalid.") from error


def _strict_recovery_workspace(value: object) -> WorkspaceBinding:
    """Reject scalar coercion at the durable source-identity boundary."""
    if type(value) is not dict:
        raise ValueError("The pending rebase workspace schema is invalid.")
    integer_fields = {"schema_version", "item_number", "generation"}
    text_fields = {
        "kind",
        "cwd",
        "reusable_root",
        "repository",
        "ownership_key",
        "lane",
        "revision",
    }
    if (
        any(type(value.get(key)) is not int for key in integer_fields)
        or any(type(value.get(key)) is not str for key in text_fields)
        or type(value.get("detached")) is not bool
    ):
        raise ValueError("The pending rebase workspace scalar type is invalid.")
    return WorkspaceBinding.from_dict(value)


def _recovery_sha_valid(value: object) -> bool:
    """Accept only a complete lowercase Git object ID."""
    return type(value) is str and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value) is not None


def _recovery_branch_valid(value: object) -> bool:
    """Reject branch text that can alter a ref or command boundary."""
    return (
        type(value) is str
        and 0 < len(value) <= 256
        and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", value) is not None
        and ".." not in value
        and all(
            part and not part.startswith(".") and not part.endswith(".lock")
            for part in value.split("/")
        )
        and not value.endswith((".", "/"))
    )


def _validate_abort_restoration(record: PendingRebaseRecord) -> None:
    """Keep terminal restoration separate from completed-rebase evidence."""
    if record.phase != "aborted":
        if record.restored_workspace is not None or record.restored_tree_sha is not None:
            raise ValueError("Only an aborted rebase can contain restoration evidence.")
        return
    if (
        record.schema_version != 2
        or type(record.restored_workspace) is not WorkspaceBinding
        or _strict_recovery_workspace(record.restored_workspace.to_dict())
        != record.request.workspace
        or not _recovery_sha_valid(record.restored_tree_sha)
    ):
        raise ValueError("The aborted rebase restoration identity is invalid.")


def _validate_recovery_result(record: PendingRebaseRecord) -> None:
    """Keep the worker result separate from its original input and remote lease."""
    result = record.resulting_workspace
    if record.phase in {"intent", "aborted"}:
        if result is not None or record.resulting_tree_sha is not None:
            raise ValueError("An intent or abort cannot claim a completed rebase result.")
        return
    original = record.request.workspace
    if (
        type(result) is not WorkspaceBinding
        or not _recovery_sha_valid(record.resulting_tree_sha)
        or not _recovery_sha_valid(result.revision)
        or type(result.generation) is not int
        or type(original.generation) is not int
        or result.generation < original.generation
        or replace(result, revision=original.revision, generation=original.generation) != original
    ):
        raise ValueError("The pending rebase result ownership is invalid.")


GIT_OPS: frozenset[str] = frozenset(
    {
        "clone",
        "prepare_repository_validation",
        "prepare_intake",
        "sync_checkout",
        "verify_issue_wave_ancestry",
        "create_worktree",
        "claim_dirty_direct_continuation",
        "publish_dirty_direct_continuation",
        "finish_dirty_direct_publication",
        "inspect_implementation_worktree",
        "recover_dirty_worktree",
        "verify_pr_review_checkout",
        "verify_rebase_review",
        "remove_worktree",
        "fetch_main",
        "rebase",
        "validate_rebase_conflict",
        "continue_rebase",
        "discover_pending_rebase",
        "discover_first_publication",
        "commit_push",
        "prepare_remediation_recovery",
        "publish_remediation_recovery",
        "verify_remediation_journal",
        "persist_remediation_pretest_candidate",
        "invalidate_remediation_pretest_candidate",
        "release_branch_reservation",
    }
)

WORKTREE_MATERIALIZED_KEY = "worktree_materialized"

# These limits bound data from an implementation writer before the data enters
# coordinator state or an agent prompt.
IMPLEMENTATION_INSPECTION_METADATA_MAX_BYTES = 64 * 1024
IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES = 64 * 1024
IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES = 256 * 1024
REPOSITORY_CHECKOUT_OPS = frozenset({"clone", "prepare_intake", "sync_checkout"})


@dataclass(frozen=True)
class GitJob:
    """Request one allowlisted Git operation."""

    repo: str
    op: str
    timeout_s: int
    kwargs: dict[str, Any] = field(default_factory=dict)
    descr: str = ""
    # Pipeline scheduling uses a repository-local key.  Authenticated Git
    # transport validates a separate canonical OWNER/REPOSITORY identity.
    expected_repository: str | None = None
    deadline_s: float | None = None
    workspace: WorkspaceBinding | None = None
    repository_lock_wait_timeout_s: float | None = None
    repository_validation_preparation: RepositoryValidationSourceRequest | None = None
    capability_target: CapabilityRequestTarget | None = field(default=None, kw_only=True)
    rebase_recovery_candidate: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Reject an operation outside the closed Git vocabulary."""
        if self.op not in GIT_OPS:
            raise ValueError(f"unknown git op {self.op!r}; expected one of {sorted(GIT_OPS)}")
        validate_git_repository_validation(self)
        if self.rebase_recovery_candidate is not None and (
            type(self.rebase_recovery_candidate) is not str
            or re.fullmatch(r"[0-9a-f]{32}", self.rebase_recovery_candidate) is None
            or self.op not in {"rebase", "continue_rebase"}
            or self.workspace is None
            or self.capability_target is None
        ):
            raise ValueError("The pending rebase candidate is not bound to its operation.")
        if self.capability_target is not None:
            target = self.capability_target
            if type(target) is not CapabilityRequestTarget:
                raise ValueError("The Git capability request type is invalid.")
            replace(target)
            if (
                self.op not in {"rebase", "continue_rebase"}
                or target.phase != "rebase"
                or target.workspace != self.workspace
                or target.repository.casefold() != self.transport_repository.casefold()
                or target.workspace.repository is None
                or target.workspace.repository.casefold()
                not in {target.repository.casefold(), self.repo.casefold()}
            ):
                raise ValueError("The Git capability request does not match its operation.")
        if self.deadline_s is not None and (
            isinstance(self.deadline_s, bool)
            or not isinstance(self.deadline_s, (int, float))
            or not math.isfinite(self.deadline_s)
            or self.deadline_s <= 0
        ):
            raise ValueError("deadline_s must be a finite positive monotonic time")
        lock_wait = self.repository_lock_wait_timeout_s
        if lock_wait is not None and (
            isinstance(lock_wait, bool)
            or not isinstance(lock_wait, (int, float))
            or not math.isfinite(lock_wait)
            or lock_wait <= 0
        ):
            raise ValueError("repository_lock_wait_timeout_s must be a finite positive value")
        if lock_wait is not None and self.deadline_s is not None:
            raise ValueError("repository_lock_wait_timeout_s cannot be combined with deadline_s")
        if lock_wait is not None and self.op not in REPOSITORY_CHECKOUT_OPS:
            raise ValueError(
                "repository_lock_wait_timeout_s is valid only for repository checkout jobs"
            )

    @property
    def transport_repository(self) -> str:
        """Return the canonical identity required for authenticated Git transport."""
        return self.expected_repository or self.repo


def validate_git_repository_validation(job: GitJob) -> None:
    """Reject source preparation that conflicts with its closed job fields."""
    request = job.repository_validation_preparation
    if request is None:
        if job.op == "prepare_repository_validation":
            raise ValueError("Source preparation requires a bound request.")
        return
    if type(request) is not RepositoryValidationSourceRequest:
        raise ValueError("The source preparation request type is invalid.")
    replace(request)
    if (
        job.op != "prepare_repository_validation"
        or job.kwargs != {}
        or type(job.repo) is not str
        or job.repo.casefold() not in {request.repository.casefold(), "comet"}
        or job.transport_repository.casefold() != request.repository.casefold()
        or job.workspace != request.workspace
        or type(job.timeout_s) is not int
        or not 0 < job.timeout_s <= 120
        or type(job.deadline_s) not in {int, float}
        or job.deadline_s is None
        or not 0 < job.deadline_s <= request.deadline_s
        or job.repository_lock_wait_timeout_s is not None
    ):
        raise ValueError("The source preparation job does not match its request.")
