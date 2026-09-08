"""Durable host receipts for prepared remediation commits."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Self

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.git_jobs import (
    DIRTY_SNAPSHOT_CHANGED_FILE_MAX,
    IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES,
    IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
)
from hephaestus.automation.remediation_recovery import (
    RemediationRecoveryReceipt,
    RemediationReplyResult,
    RemediationReviewInput,
    encode_remediation_review_input,
)
from hephaestus.automation.source_worktree import SourceWorkspaceError, SourceWorkspaceReceipt

_STORE_DIR = "remediation-prepublication"
_STORE_FORMAT = 1
_INTENT_FORMAT = 0
_STORE_MAX_BYTES = 1024 * 1024
_BATCH_RE = re.compile(r"[0-9a-f]{32}")
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class RemediationPreparationIntent:
    """Bind all evidence that exists before Git creates the recovery child."""

    repository: str
    issue_number: int
    pr_number: int
    repo_root: str
    worktree_path: str
    branch: str
    expected_remote_sha: str
    candidate_tree_sha: str
    add_paths: tuple[str, ...]
    update_paths: tuple[str, ...]
    committed_diff_sha256: str
    committed_diff: str
    failure_diagnostic: str
    thread_snapshot_json: str
    content_snapshot: tuple[tuple[str, str], ...]
    batch_nonce: str

    def __post_init__(self) -> None:
        """Validate the immutable pre-commit authority."""
        string_fields = (
            self.repository,
            self.repo_root,
            self.worktree_path,
            self.branch,
            self.expected_remote_sha,
            self.candidate_tree_sha,
            self.committed_diff_sha256,
            self.committed_diff,
            self.failure_diagnostic,
            self.thread_snapshot_json,
            self.batch_nonce,
        )
        if (
            not all(isinstance(value, str) for value in string_fields)
            or isinstance(self.issue_number, bool)
            or not isinstance(self.issue_number, int)
            or isinstance(self.pr_number, bool)
            or not isinstance(self.pr_number, int)
            or not isinstance(self.add_paths, tuple)
            or not isinstance(self.update_paths, tuple)
            or not all(isinstance(path, str) for path in (*self.add_paths, *self.update_paths))
            or not isinstance(self.content_snapshot, tuple)
            or not all(
                isinstance(item, tuple)
                and len(item) == 2
                and all(isinstance(member, str) for member in item)
                for item in self.content_snapshot
            )
        ):
            raise ValueError("remediation prepublication intent schema is invalid")
        try:
            committed_diff_bytes = self.committed_diff.encode("utf-8")
            self.thread_snapshot_json.encode("utf-8")
        except UnicodeError as error:
            raise ValueError("remediation prepublication intent schema is invalid") from error
        if (
            self.repository != self.repository.casefold()
            or self.repository.count("/") != 1
            or not all(self.repository.split("/"))
            or self.issue_number <= 0
            or self.pr_number <= 0
            or not self.branch
            or _FULL_SHA_RE.fullmatch(self.expected_remote_sha) is None
            or _FULL_SHA_RE.fullmatch(self.candidate_tree_sha) is None
            or _SHA256_RE.fullmatch(self.committed_diff_sha256) is None
            or hashlib.sha256(committed_diff_bytes).hexdigest() != self.committed_diff_sha256
            or _BATCH_RE.fullmatch(self.batch_nonce) is None
        ):
            raise ValueError("remediation prepublication intent identity is invalid")
        repo_root = Path(self.repo_root)
        worktree = Path(self.worktree_path)
        if (
            not repo_root.is_absolute()
            or not worktree.is_absolute()
            or repo_root not in worktree.parents
            or not self.thread_snapshot_json
        ):
            raise ValueError("remediation prepublication intent path is invalid")
        changed_paths = (*self.add_paths, *self.update_paths)
        if not changed_paths or len(set(changed_paths)) != len(changed_paths):
            raise ValueError("remediation prepublication intent paths are invalid")
        content = dict(self.content_snapshot)
        if (
            len(content) != len(self.content_snapshot)
            or set(content) != {"index_sha256", "worktree_sha256", "untracked_sha256"}
            or any(_SHA256_RE.fullmatch(value) is None for value in content.values())
        ):
            raise ValueError("remediation prepublication content snapshot is invalid")
        RemediationReviewInput.canonical_thread_snapshot(json.loads(self.thread_snapshot_json))

    def as_dict(self) -> dict[str, object]:
        """Return the canonical JSON-compatible authority payload."""
        return {
            "repository": self.repository,
            "issue_number": self.issue_number,
            "pr_number": self.pr_number,
            "repo_root": self.repo_root,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "expected_remote_sha": self.expected_remote_sha,
            "candidate_tree_sha": self.candidate_tree_sha,
            "add_paths": list(self.add_paths),
            "update_paths": list(self.update_paths),
            "committed_diff_sha256": self.committed_diff_sha256,
            "committed_diff": self.committed_diff,
            "failure_diagnostic": self.failure_diagnostic,
            "thread_snapshot_json": self.thread_snapshot_json,
            "content_snapshot": [list(item) for item in self.content_snapshot],
            "batch_nonce": self.batch_nonce,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        """Restore one intent only from its exact closed schema."""
        if not isinstance(value, dict) or set(value) != {
            "repository",
            "issue_number",
            "pr_number",
            "repo_root",
            "worktree_path",
            "branch",
            "expected_remote_sha",
            "candidate_tree_sha",
            "add_paths",
            "update_paths",
            "committed_diff_sha256",
            "committed_diff",
            "failure_diagnostic",
            "thread_snapshot_json",
            "content_snapshot",
            "batch_nonce",
        }:
            raise ValueError("remediation prepublication intent schema is invalid")
        add_paths = value["add_paths"]
        update_paths = value["update_paths"]
        snapshot = value["content_snapshot"]
        if (
            not isinstance(add_paths, list)
            or not isinstance(update_paths, list)
            or not isinstance(snapshot, list)
            or not all(isinstance(path, str) for path in (*add_paths, *update_paths))
            or not all(
                isinstance(item, list)
                and len(item) == 2
                and all(isinstance(member, str) for member in item)
                for item in snapshot
            )
        ):
            raise ValueError("remediation prepublication intent schema is invalid")
        try:
            return cls(
                repository=value["repository"],
                issue_number=value["issue_number"],
                pr_number=value["pr_number"],
                repo_root=value["repo_root"],
                worktree_path=value["worktree_path"],
                branch=value["branch"],
                expected_remote_sha=value["expected_remote_sha"],
                candidate_tree_sha=value["candidate_tree_sha"],
                add_paths=tuple(add_paths),
                update_paths=tuple(update_paths),
                committed_diff_sha256=value["committed_diff_sha256"],
                committed_diff=value["committed_diff"],
                failure_diagnostic=value["failure_diagnostic"],
                thread_snapshot_json=value["thread_snapshot_json"],
                content_snapshot=tuple(tuple(item) for item in snapshot),
                batch_nonce=value["batch_nonce"],
            )
        except (AttributeError, TypeError, UnicodeError, ValueError) as error:
            raise ValueError("remediation prepublication intent schema is invalid") from error

    def receipt(self, recovery_commit_sha: str) -> RemediationRecoveryReceipt:
        """Build the exact complete receipt for the durable private HEAD."""
        review_input = RemediationReviewInput(
            format_version=3,
            repository=self.repository,
            issue_number=self.issue_number,
            pr_number=self.pr_number,
            repo_root=self.repo_root,
            worktree_path=self.worktree_path,
            branch=self.branch,
            reviewed_parent_sha=self.expected_remote_sha,
            candidate_tree_sha=self.candidate_tree_sha,
            recovery_commit_sha=recovery_commit_sha,
            changed_paths=(*self.add_paths, *self.update_paths),
            committed_diff_sha256=self.committed_diff_sha256,
            committed_diff=self.committed_diff,
            failure_diagnostic=self.failure_diagnostic,
            thread_snapshot_sha256=hashlib.sha256(
                self.thread_snapshot_json.encode("utf-8")
            ).hexdigest(),
            thread_snapshot_json=self.thread_snapshot_json,
        )
        encoding, data = encode_remediation_review_input(review_input.canonical_bytes)
        return RemediationRecoveryReceipt(
            review_input_bytes=review_input.canonical_bytes.decode("utf-8"),
            review_input_sha256=review_input.review_input_sha256,
            journal_input_encoding=encoding,
            journal_input_data=data,
            expected_remote_sha=self.expected_remote_sha,
            content_snapshot=self.content_snapshot,
            add_paths=self.add_paths,
            update_paths=self.update_paths,
        )


def _canonical_json(value: object) -> str:
    """Return deterministic JSON for one local authority record."""
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _record(
    receipt: RemediationRecoveryReceipt,
    batch_nonce: str,
) -> list[object]:
    """Return the bounded record that binds one prepared child."""
    review_input = receipt.review_input
    if _BATCH_RE.fullmatch(batch_nonce) is None:
        raise ValueError("remediation prepublication batch identity is invalid")
    body: list[object] = [
        _STORE_FORMAT,
        review_input.repository,
        review_input.issue_number,
        review_input.pr_number,
        review_input.repo_root,
        review_input.worktree_path,
        review_input.branch,
        review_input.reviewed_parent_sha,
        review_input.recovery_commit_sha,
        batch_nonce,
        receipt.as_dict(),
    ]
    encoded = _canonical_json(body).encode("utf-8")
    if len(encoded) > _STORE_MAX_BYTES:
        raise ValueError("remediation prepublication receipt exceeds its byte limit")
    return [*body, hashlib.sha256(encoded).hexdigest()]


def _intent_record(
    intent: RemediationPreparationIntent,
) -> list[object]:
    """Return authority that exists before the private commit is created."""
    body: list[object] = [_INTENT_FORMAT, "prepare-intent", intent.as_dict()]
    encoded = _canonical_json(body).encode("utf-8")
    if len(encoded) > _STORE_MAX_BYTES:
        raise ValueError("remediation prepublication intent exceeds its byte limit")
    return [*body, hashlib.sha256(encoded).hexdigest()]


def _parse_intent(raw: object) -> RemediationPreparationIntent:
    """Validate one durable pre-commit intent record."""
    if not isinstance(raw, list) or len(raw) != 4 or raw[:2] != [0, "prepare-intent"]:
        raise ValueError("remediation prepublication intent schema is invalid")
    body = raw[:-1]
    if raw[-1] != hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest():
        raise ValueError("remediation prepublication intent digest is invalid")
    return RemediationPreparationIntent.from_dict(raw[2])


def _parse(raw: object) -> tuple[RemediationRecoveryReceipt, str]:
    """Validate one complete local authority record."""
    if not isinstance(raw, list) or len(raw) != 12 or raw[0] != _STORE_FORMAT:
        raise ValueError("remediation prepublication receipt schema is invalid")
    body = raw[:-1]
    digest = raw[-1]
    if (
        not isinstance(digest, str)
        or digest != hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    ):
        raise ValueError("remediation prepublication receipt digest is invalid")
    receipt = RemediationRecoveryReceipt.from_dict(body[10])
    batch_nonce = body[9]
    if not isinstance(batch_nonce, str) or _record(receipt, batch_nonce) != raw:
        raise ValueError("remediation prepublication receipt identity is invalid")
    review_input = receipt.review_input
    expected = [
        review_input.repository,
        review_input.issue_number,
        review_input.pr_number,
        review_input.repo_root,
        review_input.worktree_path,
        review_input.branch,
        review_input.reviewed_parent_sha,
        review_input.recovery_commit_sha,
    ]
    if body[1:9] != expected:
        raise ValueError("remediation prepublication receipt binding is invalid")
    return receipt, batch_nonce


def _directory(repo_root: Path, *, create: bool) -> tuple[Path, int]:
    """Open the host state directory without following path components."""
    root = repo_root.resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(root, flags)
    try:
        for component in (*Path(DEFAULT_STATE_DIR).parts, _STORE_DIR):
            try:
                child_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                child_fd = os.open(component, flags, dir_fd=current_fd)
            if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                os.close(child_fd)
                raise ValueError("remediation prepublication store is not a directory")
            os.close(current_fd)
            current_fd = child_fd
        return root / DEFAULT_STATE_DIR / _STORE_DIR, current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _filename(pr_number: int) -> str:
    """Return the fixed record name for one pull request."""
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError("remediation prepublication PR identity is invalid")
    return f"pr-{pr_number}.json"


def prepublication_private_git_dir(*, repo_root: Path, pr_number: int, create: bool) -> Path:
    """Return one no-follow host-owned private Git directory for a PR."""
    directory, directory_fd = _directory(repo_root, create=create)
    name = f"pr-{pr_number}.git"
    _filename(pr_number)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            private_fd = os.open(name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            if not create:
                raise
            with suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=directory_fd)
            private_fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            if not stat.S_ISDIR(os.fstat(private_fd).st_mode):
                raise ValueError("remediation private Git authority is not a directory")
        finally:
            os.close(private_fd)
        return directory / name
    finally:
        os.close(directory_fd)


def read_prepublication_private_head(*, repo_root: Path, pr_number: int) -> str:
    """Read the bounded detached HEAD from one durable private Git directory."""
    git_dir = prepublication_private_git_dir(repo_root=repo_root, pr_number=pr_number, create=False)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(git_dir, flags)
    try:
        head_fd = os.open("HEAD", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        try:
            info = os.fstat(head_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 128:
                raise ValueError("remediation private Git HEAD is invalid")
            raw = os.read(head_fd, 129).decode("ascii")
        finally:
            os.close(head_fd)
    finally:
        os.close(directory_fd)
    head = raw.strip()
    if _FULL_SHA_RE.fullmatch(head) is None:
        raise ValueError("remediation private Git HEAD is invalid")
    return head


def _read_record(directory_fd: int, name: str) -> object:
    """Read one no-follow regular record with a byte limit."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _STORE_MAX_BYTES:
            raise ValueError("remediation prepublication receipt is not a bounded regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)


def save_prepublication_receipt(
    *,
    repo_root: Path,
    receipt: RemediationRecoveryReceipt,
    batch_nonce: str,
) -> None:
    """Persist one exact prepared child before its first publication attempt."""
    record = _record(receipt, batch_nonce)
    encoded = _canonical_json(record)
    directory, directory_fd = _directory(repo_root, create=True)
    del directory
    name = _filename(receipt.review_input.pr_number)
    try:
        try:
            current = _read_record(directory_fd, name)
        except FileNotFoundError:
            current = None
        if current is not None:
            if current == record:
                _parse(current)
                return
            try:
                intent = _parse_intent(current)
            except ValueError:
                raise ValueError(
                    "a different remediation prepublication receipt already exists"
                ) from None
            review_input = receipt.review_input
            expected_intent = RemediationPreparationIntent(
                repository=review_input.repository,
                issue_number=review_input.issue_number,
                pr_number=review_input.pr_number,
                repo_root=review_input.repo_root,
                worktree_path=review_input.worktree_path,
                branch=review_input.branch,
                expected_remote_sha=review_input.reviewed_parent_sha,
                candidate_tree_sha=review_input.candidate_tree_sha,
                add_paths=receipt.add_paths,
                update_paths=receipt.update_paths,
                committed_diff_sha256=review_input.committed_diff_sha256,
                committed_diff=review_input.committed_diff,
                failure_diagnostic=review_input.failure_diagnostic,
                thread_snapshot_json=review_input.thread_snapshot_json,
                content_snapshot=receipt.content_snapshot,
                batch_nonce=batch_nonce,
            )
            if intent != expected_intent:
                raise ValueError("a different remediation prepublication receipt already exists")
            temporary = f"{name}.next"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                if _read_record(directory_fd, temporary) != record:
                    raise ValueError(
                        "a different remediation prepublication receipt replacement exists"
                    ) from None
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            concurrent = _read_record(directory_fd, name)
            if concurrent != record:
                raise ValueError(
                    "a different remediation prepublication receipt already exists"
                ) from None
            _parse(concurrent)
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(directory_fd)


def save_prepublication_intent(
    *,
    repo_root: Path,
    repository: str,
    issue_number: int,
    pr_number: int,
    worktree_path: Path,
    branch: str,
    expected_remote_sha: str,
    candidate_tree_sha: str,
    add_paths: tuple[str, ...],
    update_paths: tuple[str, ...],
    committed_diff_sha256: str,
    committed_diff: str,
    failure_diagnostic: str,
    thread_snapshot_json: str,
    content_snapshot: tuple[tuple[str, str], ...],
    batch_nonce: str,
) -> None:
    """Persist fail-closed ownership before the private commit operation."""
    record = _intent_record(
        RemediationPreparationIntent(
            repository=repository,
            issue_number=issue_number,
            pr_number=pr_number,
            repo_root=str(repo_root.resolve(strict=True)),
            worktree_path=str(worktree_path.resolve(strict=True)),
            branch=branch,
            expected_remote_sha=expected_remote_sha,
            candidate_tree_sha=candidate_tree_sha,
            add_paths=add_paths,
            update_paths=update_paths,
            committed_diff_sha256=committed_diff_sha256,
            committed_diff=committed_diff,
            failure_diagnostic=failure_diagnostic,
            thread_snapshot_json=thread_snapshot_json,
            content_snapshot=content_snapshot,
            batch_nonce=batch_nonce,
        )
    )
    encoded = _canonical_json(record)
    _directory_path, directory_fd = _directory(repo_root, create=True)
    name = _filename(pr_number)
    try:
        try:
            current = _read_record(directory_fd, name)
        except FileNotFoundError:
            current = None
        if current is not None:
            if current == record:
                _parse_intent(current)
                return
            try:
                receipt, existing_batch = _parse(current)
            except ValueError:
                raise ValueError(
                    "a different remediation prepublication authority exists"
                ) from None
            if existing_batch == batch_nonce and receipt.review_input.pr_number == pr_number:
                return
            raise ValueError("a different remediation prepublication authority exists")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def load_prepublication_receipt(
    *,
    repo_root: Path,
    repository: str,
    issue_number: int,
    pr_number: int,
    branch: str,
    expected_remote_sha: str,
    thread_snapshot_json: str,
) -> tuple[RemediationRecoveryReceipt, str, bool] | None:
    """Load one receipt only when every live recovery identity is exact."""
    try:
        _directory_path, directory_fd = _directory(repo_root, create=False)
    except FileNotFoundError:
        return None
    try:
        try:
            raw = _read_record(directory_fd, _filename(pr_number))
        except FileNotFoundError:
            return None
        if isinstance(raw, list) and raw[:2] == [0, "prepare-intent"]:
            _parse_intent(raw)
            raise ValueError("remediation prepublication prepare intent has no receipt")
        receipt, batch_nonce = _parse(raw)
    finally:
        os.close(directory_fd)
    review_input = receipt.review_input
    if (
        review_input.repository != repository.casefold()
        or review_input.issue_number != issue_number
        or review_input.pr_number != pr_number
        or review_input.repo_root != str(repo_root.resolve(strict=True))
        or review_input.branch != branch
        or expected_remote_sha
        not in {review_input.reviewed_parent_sha, review_input.recovery_commit_sha}
        or review_input.thread_snapshot_json != thread_snapshot_json
    ):
        raise ValueError("remediation prepublication live identity changed")
    return receipt, batch_nonce, expected_remote_sha == review_input.recovery_commit_sha


def load_prepublication_intent(
    *,
    repo_root: Path,
    repository: str,
    issue_number: int,
    pr_number: int,
    branch: str,
    expected_remote_sha: str,
    thread_snapshot_json: str,
) -> RemediationPreparationIntent | None:
    """Load one exact incomplete authority for private-HEAD recovery."""
    try:
        _directory_path, directory_fd = _directory(repo_root, create=False)
    except FileNotFoundError:
        return None
    try:
        try:
            raw = _read_record(directory_fd, _filename(pr_number))
        except FileNotFoundError:
            return None
        if not (isinstance(raw, list) and raw[:2] == [0, "prepare-intent"]):
            return None
        intent = _parse_intent(raw)
    finally:
        os.close(directory_fd)
    if (
        intent.repository != repository.casefold()
        or intent.issue_number != issue_number
        or intent.pr_number != pr_number
        or intent.repo_root != str(repo_root.resolve(strict=True))
        or intent.branch != branch
        or intent.expected_remote_sha != expected_remote_sha
        or intent.thread_snapshot_json != thread_snapshot_json
    ):
        raise ValueError("remediation prepublication live identity changed")
    return intent


def remove_prepublication_receipt(
    *, repo_root: Path, pr_number: int, expected_review_input_sha256: str
) -> None:
    """Remove one receipt only when its exact canonical input was journaled."""
    try:
        _directory_path, directory_fd = _directory(repo_root, create=False)
    except FileNotFoundError:
        return
    try:
        name = _filename(pr_number)
        try:
            receipt, _batch_nonce = _parse(_read_record(directory_fd, name))
        except FileNotFoundError:
            return
        if receipt.review_input_sha256 != expected_review_input_sha256:
            raise ValueError("remediation prepublication receipt removal identity is invalid")
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def canonical_source_receipt_json(receipt: SourceWorkspaceReceipt) -> str:
    """Encode the complete receipt with sorted keys and compact separators."""
    return _canonical_json(receipt.to_dict())


def source_receipt_digest(receipt: SourceWorkspaceReceipt) -> str:
    """Hash the canonical complete receipt, independent of file whitespace."""
    return hashlib.sha256(canonical_source_receipt_json(receipt).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RemediationPretestCandidate:
    """Bind a successful remediation candidate before its test job."""

    phase: str
    repository: str
    issue_number: int
    pr_number: int
    repo_root: str
    worktree_path: str
    branch: str
    expected_remote_sha: str
    source_receipt: SourceWorkspaceReceipt
    source_receipt_sha256: str
    source_repository_identity: str
    source_ownership_key: str
    source_generation: int
    candidate_tree_sha: str
    add_paths: tuple[str, ...]
    update_paths: tuple[str, ...]
    diff: str
    diff_sha256: str
    content_snapshot: tuple[tuple[str, str], ...]
    thread_snapshot_json: str
    batch_nonce: str
    candidate_sequence: int
    successful_job_id: str
    successful_result_sha256: str
    addressed_replies: tuple[tuple[str, str], ...]
    consumed_head: str | None = None

    def __post_init__(self) -> None:
        """Validate source, candidate and successful-result evidence."""
        _validate_pretest_identity(self)
        _validate_pretest_content(self)
        if len(self.canonical_bytes) > _STORE_MAX_BYTES:
            raise ValueError("remediation pretest candidate exceeds its byte limit")

    def as_dict(self) -> dict[str, Any]:
        """Return the closed JSON-compatible candidate payload."""
        value = {field.name: getattr(self, field.name) for field in fields(self)}
        value["source_receipt"] = self.source_receipt.to_dict()
        value["add_paths"] = list(self.add_paths)
        value["update_paths"] = list(self.update_paths)
        value["content_snapshot"] = [list(item) for item in self.content_snapshot]
        value["addressed_replies"] = dict(self.addressed_replies)
        return value

    @classmethod
    def from_dict(cls, value: object) -> Self:
        """Restore a candidate from its exact schema without type coercion."""
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise ValueError("remediation pretest candidate schema is invalid")
        payload = dict(value)
        receipt = payload["source_receipt"]
        try:
            if not isinstance(receipt, dict):
                raise ValueError("source receipt is not an object")
            parsed = SourceWorkspaceReceipt.from_dict(receipt)
            if _canonical_json(parsed.to_dict()) != _canonical_json(receipt):
                raise ValueError("source receipt types are invalid")
            payload["source_receipt"] = parsed
            for key in ("add_paths", "update_paths"):
                if not isinstance(payload[key], list):
                    raise ValueError("candidate paths are not lists")
                payload[key] = tuple(payload[key])
            snapshot = payload["content_snapshot"]
            if not isinstance(snapshot, list) or any(
                not isinstance(item, list) for item in snapshot
            ):
                raise ValueError("content snapshot is not a list of pairs")
            payload["content_snapshot"] = tuple(tuple(item) for item in snapshot)
            replies = payload["addressed_replies"]
            if not isinstance(replies, dict) or any(not isinstance(key, str) for key in replies):
                raise ValueError("addressed replies are not a reply map")
            payload["addressed_replies"] = tuple(sorted(replies.items()))
            return cls(**payload)
        except (TypeError, AttributeError, UnicodeError, SourceWorkspaceError) as error:
            raise ValueError("remediation pretest candidate schema is invalid") from error

    @property
    def canonical_bytes(self) -> bytes:
        """Return the versioned canonical store envelope."""
        return _canonical_json([1, "pretest-candidate", self.as_dict()]).encode("utf-8")

    @property
    def digest(self) -> str:
        """Return the digest used by the store compare-and-swap operation."""
        return hashlib.sha256(self.canonical_bytes).hexdigest()


def _validate_pretest_identity(candidate: RemediationPretestCandidate) -> None:
    """Validate the exact source receipt and host result identity."""
    for name in ("issue_number", "pr_number", "source_generation", "candidate_sequence"):
        value = getattr(candidate, name)
        if type(value) is not int or value <= 0:
            raise ValueError("remediation pretest identifiers must be positive integers")
    for name in (
        "repository",
        "branch",
        "source_repository_identity",
        "source_ownership_key",
        "successful_job_id",
        "batch_nonce",
        "phase",
    ):
        value = getattr(candidate, name)
        if not isinstance(value, str) or not value or value.strip() != value or "\0" in value:
            raise ValueError("remediation pretest identity is invalid")
    if (
        re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", candidate.repository) is None
        or _BATCH_RE.fullmatch(candidate.batch_nonce) is None
        or len(candidate.successful_job_id) > 256
        or candidate.phase not in {"ready", "invalidated", "consumed"}
    ):
        raise ValueError("remediation pretest identity is invalid")
    for name in ("expected_remote_sha", "candidate_tree_sha"):
        value = getattr(candidate, name)
        if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
            raise ValueError("remediation pretest commit identity is invalid")
    for name in ("source_receipt_sha256", "diff_sha256", "successful_result_sha256"):
        value = getattr(candidate, name)
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise ValueError("remediation pretest digest is invalid")
    _validate_pretest_consumed_head(candidate)
    _validate_pretest_source(candidate)


def _validate_pretest_consumed_head(candidate: RemediationPretestCandidate) -> None:
    """Require a head only for the terminal consumed phase."""
    if candidate.phase == "consumed":
        if (
            not isinstance(candidate.consumed_head, str)
            or _FULL_SHA_RE.fullmatch(candidate.consumed_head) is None
        ):
            raise ValueError("consumed candidate requires a commit head")
    elif candidate.consumed_head is not None:
        raise ValueError("unconsumed candidate cannot carry a commit head")


def _validate_pretest_source(candidate: RemediationPretestCandidate) -> None:
    """Require canonical confined paths and the complete attached source identity."""
    for value in (candidate.repo_root, candidate.worktree_path):
        if (
            not isinstance(value, str)
            or "\0" in value
            or not Path(value).is_absolute()
            or os.path.normpath(value) != value
        ):
            raise ValueError("remediation pretest path is invalid")
    if Path(candidate.repo_root) not in Path(candidate.worktree_path).parents:
        raise ValueError("remediation pretest writer must be inside its repository")
    if (
        ".." in candidate.branch
        or candidate.branch.startswith(("/", "-"))
        or any(ord(char) < 32 for char in candidate.branch)
    ):
        raise ValueError("remediation pretest branch is invalid")
    receipt = candidate.source_receipt
    if not isinstance(receipt, SourceWorkspaceReceipt):
        raise ValueError("remediation pretest source receipt is invalid")
    raw = receipt.to_dict()
    if _canonical_json(SourceWorkspaceReceipt.from_dict(raw).to_dict()) != _canonical_json(raw):
        raise ValueError("remediation pretest source receipt types are invalid")
    if (
        receipt.detached is not False
        or receipt.lane != SourceLane.IMPLEMENTATION
        or receipt.repository.casefold()
        not in {candidate.repository, candidate.repository.split("/")[1]}
        or receipt.dirty_claim is not None
        or receipt.repository_identity != candidate.source_repository_identity
        or receipt.ownership_key != candidate.source_ownership_key
        or receipt.ownership_key != f"{receipt.repository_identity}:{candidate.issue_number}:impl"
        or receipt.item_number != candidate.issue_number
        or type(receipt.item_number) is not int
        or type(receipt.generation) is not int
        or receipt.generation != candidate.source_generation
        or str(receipt.path) != candidate.worktree_path
        or receipt.branch != candidate.branch
        or receipt.revision != candidate.expected_remote_sha
        or source_receipt_digest(receipt) != candidate.source_receipt_sha256
    ):
        raise ValueError("remediation pretest source identity does not match")


def _validate_pretest_content(candidate: RemediationPretestCandidate) -> None:
    """Check bounded safe paths, candidate bytes and exact thread replies."""
    if not isinstance(candidate.add_paths, tuple) or not isinstance(candidate.update_paths, tuple):
        raise ValueError("remediation pretest paths must be tuples")
    paths = (*candidate.add_paths, *candidate.update_paths)
    if (
        not paths
        or len(paths) > DIRTY_SNAPSHOT_CHANGED_FILE_MAX
        or any(
            not isinstance(path, str)
            or not path
            or "\0" in path
            or Path(path).is_absolute()
            or Path(path).as_posix() != path
            or any(part in {".", "..", ".git"} for part in Path(path).parts)
            for path in paths
        )
        or len(set(paths)) != len(paths)
        or sum(len(os.fsencode(path)) + 1 for path in paths)
        > IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES
    ):
        raise ValueError("remediation pretest paths are invalid")
    if (
        not isinstance(candidate.diff, str)
        or len(candidate.diff.encode("utf-8")) > IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES
        or hashlib.sha256(candidate.diff.encode("utf-8")).hexdigest() != candidate.diff_sha256
    ):
        raise ValueError("remediation pretest diff is invalid")
    if not isinstance(candidate.content_snapshot, tuple) or any(
        not isinstance(item, tuple)
        or len(item) != 2
        or any(not isinstance(value, str) for value in item)
        for item in candidate.content_snapshot
    ):
        raise ValueError("remediation pretest content snapshot is invalid")
    snapshot = dict(candidate.content_snapshot)
    if (
        len(snapshot) != len(candidate.content_snapshot)
        or set(snapshot) != {"index_sha256", "worktree_sha256", "untracked_sha256"}
        or any(_SHA256_RE.fullmatch(value) is None for value in snapshot.values())
    ):
        raise ValueError("remediation pretest content snapshot is invalid")
    if (
        not isinstance(candidate.thread_snapshot_json, str)
        or RemediationReviewInput.canonical_thread_snapshot(
            json.loads(candidate.thread_snapshot_json)
        )
        != candidate.thread_snapshot_json
    ):
        raise ValueError("remediation pretest thread snapshot is not canonical")
    replies = RemediationReplyResult(
        candidate.successful_result_sha256, candidate.addressed_replies
    )
    RemediationReplyResult.create(
        review_input_sha256=candidate.successful_result_sha256,
        replies=dict(replies.replies),
        thread_snapshot_json=candidate.thread_snapshot_json,
    )


def _pretest_filename(pr_number: int) -> str:
    """Use a separate namespace from legacy prepublication authority."""
    return _filename(pr_number).removesuffix(".json") + "-pretest.json"


@contextmanager
def _pretest_directory(repo_root: Path, pr_number: int, *, create: bool) -> Iterator[int]:
    """Lock the record after the caller's source lane lock, without reacquiring that lane."""
    if any(not hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")):
        raise ValueError("secure remediation pretest storage is unavailable")
    import fcntl

    _path, directory_fd = _directory(repo_root, create=create)
    lock_fd = -1
    try:
        lock_name = _pretest_filename(pr_number) + ".lock"
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            lock_fd = os.open(lock_name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            lock_fd = os.open(lock_name, flags, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError("remediation pretest lock is not a regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield directory_fd
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def _read_pretest(directory_fd: int, name: str) -> RemediationPretestCandidate | None:
    """Read a bounded regular canonical record without following links."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > _STORE_MAX_BYTES:
            raise ValueError("remediation pretest record is not a bounded regular file")
        data = stream.read(_STORE_MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    path_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or (before.st_dev, before.st_ino) != (path_info.st_dev, path_info.st_ino)
        or len(data) > _STORE_MAX_BYTES
    ):
        raise ValueError("remediation pretest record changed during read")
    raw = json.loads(data)
    if (
        not isinstance(raw, list)
        or len(raw) != 3
        or type(raw[0]) is not int
        or raw[:2] != [1, "pretest-candidate"]
    ):
        raise ValueError("remediation pretest envelope is invalid")
    candidate = RemediationPretestCandidate.from_dict(raw[2])
    if candidate.canonical_bytes != data:
        raise ValueError("remediation pretest record is not canonical")
    return candidate


def load_pretest_candidate(
    *, repo_root: Path, pr_number: int
) -> RemediationPretestCandidate | None:
    """Load every phase as evidence; only the worker can admit a ready candidate."""
    try:
        with _pretest_directory(repo_root, pr_number, create=False) as directory_fd:
            candidate = _read_pretest(directory_fd, _pretest_filename(pr_number))
            if candidate is not None and (
                candidate.repo_root != str(repo_root.resolve(strict=True))
                or candidate.pr_number != pr_number
            ):
                raise ValueError("remediation pretest record belongs to another operation")
            return candidate
    except FileNotFoundError:
        return None


def _validate_pretest_transition(
    current: RemediationPretestCandidate | None,
    candidate: RemediationPretestCandidate,
    expected_digest: str | None,
    previous_successful_job_id: str | None,
) -> None:
    """Enforce exact previous evidence and bounded phase progression."""
    if current is None:
        if (
            expected_digest is not None
            or candidate.phase != "ready"
            or candidate.candidate_sequence != 1
        ):
            raise ValueError("remediation pretest initial state is invalid")
        return
    if current.digest != expected_digest:
        raise ValueError("remediation pretest compare-and-swap digest changed")
    if current.phase == "ready" and candidate.phase in {"invalidated", "consumed"}:
        expected = replace(current, phase=candidate.phase, consumed_head=candidate.consumed_head)
        if candidate != expected:
            raise ValueError("remediation pretest phase update changed candidate evidence")
        return
    if current.phase == "invalidated" and candidate.phase == "ready":
        stable_fields = (
            "repository",
            "issue_number",
            "pr_number",
            "repo_root",
            "worktree_path",
            "branch",
            "expected_remote_sha",
            "source_receipt",
            "source_receipt_sha256",
            "source_repository_identity",
            "source_ownership_key",
            "source_generation",
            "thread_snapshot_json",
            "batch_nonce",
        )
        if (
            any(getattr(current, name) != getattr(candidate, name) for name in stable_fields)
            or candidate.candidate_sequence != current.candidate_sequence + 1
            or previous_successful_job_id != current.successful_job_id
            or candidate.successful_job_id == current.successful_job_id
        ):
            raise ValueError("remediation pretest replacement lineage is invalid")
        return
    raise ValueError("remediation pretest phase transition is invalid")


def _write_pretest_bytes(directory_fd: int, name: str, data: bytes) -> None:
    """Create one private, durable file without replacing an existing entry."""
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def save_pretest_candidate(
    *,
    repo_root: Path,
    candidate: RemediationPretestCandidate,
    expected_digest: str | None = None,
    previous_successful_job_id: str | None = None,
) -> str:
    """Compare and replace host-validated evidence under the exclusive record lock."""
    if candidate.repo_root != str(repo_root.resolve(strict=True)):
        raise ValueError("remediation pretest repository path changed")
    with _pretest_directory(repo_root, candidate.pr_number, create=True) as directory_fd:
        name = _pretest_filename(candidate.pr_number)
        current = _read_pretest(directory_fd, name)
        if current == candidate:
            if expected_digest not in (None, candidate.digest):
                raise ValueError("remediation pretest duplicate has a stale digest")
            return candidate.digest
        _validate_pretest_transition(
            current, candidate, expected_digest, previous_successful_job_id
        )
        if current is not None:
            history = name.removesuffix(".json") + f"-{current.digest}.json"
            archived = _read_pretest(directory_fd, history)
            if archived is None:
                _write_pretest_bytes(directory_fd, history, current.canonical_bytes)
                os.fsync(directory_fd)
            elif archived != current:
                raise ValueError("remediation pretest archived evidence changed")
        temporary = name + "." + uuid.uuid4().hex + ".next"
        _write_pretest_bytes(directory_fd, temporary, candidate.canonical_bytes)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
        if _read_pretest(directory_fd, name) != candidate:
            raise ValueError("remediation pretest write did not retain exact evidence")
        return candidate.digest
