"""Immutable evidence for failed-remediation recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import zlib
from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from hephaestus.automation.pipeline.git_jobs import (
    DIRTY_SNAPSHOT_CHANGED_FILE_MAX,
    IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES,
    IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES,
)
from hephaestus.automation.reply_limits import MAX_ADDRESS_REPLY_CHARS

REMEDIATION_REVIEW_INPUT_FORMAT = 3
REMEDIATION_CANONICAL_INPUT_MAX_BYTES = 512 * 1024
REMEDIATION_THREAD_SNAPSHOT_MAX_BYTES = 192 * 1024
REMEDIATION_DIAGNOSTIC_MAX_BYTES = 4 * 1024
REMEDIATION_REPLY_RESULT_MAX_BYTES = 256 * 1024
REMEDIATION_JOURNAL_COMPRESSED_MAX_BYTES = 40 * 1024
REMEDIATION_JOURNAL_COMMENT_MAX_BYTES = 60 * 1024
REMEDIATION_JOURNAL_ENCODING = "zlib+base64;json-array-v3"

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REPOSITORY_RE = re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+")
_DIRTY_CONTENT_SNAPSHOT_KEYS = frozenset({"index_sha256", "worktree_sha256", "untracked_sha256"})


def encode_remediation_review_input(encoded: bytes) -> tuple[str, str]:
    """Return bounded journal metadata for exact canonical input bytes."""
    if not isinstance(encoded, bytes) or len(encoded) > REMEDIATION_CANONICAL_INPUT_MAX_BYTES:
        raise ValueError("canonical remediation review input exceeds its byte limit")
    compressed = zlib.compress(encoded, level=9)
    if len(compressed) > REMEDIATION_JOURNAL_COMPRESSED_MAX_BYTES:
        raise ValueError("remediation journal compressed data exceeds its limit")
    data = b64encode(compressed).decode("ascii")
    if decode_remediation_review_input(REMEDIATION_JOURNAL_ENCODING, data) != encoded:
        raise ValueError("remediation journal input does not round trip")
    return REMEDIATION_JOURNAL_ENCODING, data


def decode_remediation_review_input(encoding: object, data: object) -> bytes:
    """Decode one complete bounded remediation journal input."""
    if encoding != REMEDIATION_JOURNAL_ENCODING or not isinstance(data, str):
        raise ValueError("remediation journal input encoding is invalid")
    try:
        compressed = b64decode(data.encode("ascii"), validate=True)
    except (UnicodeEncodeError, Base64Error) as error:
        raise ValueError("remediation journal input data is invalid") from error
    if len(compressed) > REMEDIATION_JOURNAL_COMPRESSED_MAX_BYTES:
        raise ValueError("remediation journal compressed data exceeds its limit")
    decoder = zlib.decompressobj()
    try:
        decoded = decoder.decompress(compressed, REMEDIATION_CANONICAL_INPUT_MAX_BYTES + 1)
    except zlib.error as error:
        raise ValueError("remediation journal compressed data is invalid") from error
    if (
        len(decoded) > REMEDIATION_CANONICAL_INPUT_MAX_BYTES
        or decoder.unconsumed_tail
        or not decoder.eof
        or decoder.unused_data
    ):
        raise ValueError("remediation journal compressed data has unsafe expansion")
    return decoded


def _canonical_json(value: object) -> str:
    """Return one deterministic JSON value."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("value must contain finite JSON data") from error


def _positive_identifier(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _full_sha(value: str, name: str) -> None:
    if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a full lowercase commit SHA")


def _absolute_canonical_path(value: str, name: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{name} must be a canonical absolute path")
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value:
        raise ValueError(f"{name} must be a canonical absolute path")
    return path


def _thread_ids(thread_snapshot_json: str) -> tuple[str, ...]:
    try:
        threads = json.loads(thread_snapshot_json)
    except json.JSONDecodeError as error:
        raise ValueError("thread_snapshot_json must contain canonical JSON") from error
    if (
        not isinstance(threads, list)
        or not threads
        or _canonical_json(threads) != thread_snapshot_json
    ):
        raise ValueError("thread_snapshot_json must contain a canonical non-empty list")
    ids: list[str] = []
    for thread in threads:
        if not isinstance(thread, dict):
            raise ValueError("thread_snapshot_json must contain thread objects")
        thread_id = thread.get("thread_id") or thread.get("id")
        if not isinstance(thread_id, str) or not thread_id or thread_id in ids:
            raise ValueError("thread_snapshot_json must contain unique thread IDs")
        ids.append(thread_id)
    return tuple(ids)


@dataclass(frozen=True, slots=True)
class RemediationReviewInput:
    """Bind one remediation review to its candidate and recovery commit."""

    format_version: int
    repository: str
    issue_number: int
    pr_number: int
    repo_root: str
    worktree_path: str
    branch: str
    reviewed_parent_sha: str
    candidate_tree_sha: str
    recovery_commit_sha: str
    changed_paths: tuple[str, ...]
    committed_diff_sha256: str
    committed_diff: str
    failure_diagnostic: str
    thread_snapshot_sha256: str
    thread_snapshot_json: str

    def __post_init__(self) -> None:
        """Validate all identities, limits, paths, and nested digests."""
        if self.format_version != REMEDIATION_REVIEW_INPUT_FORMAT:
            raise ValueError("format_version must identify remediation review input v3")
        if (
            not isinstance(self.repository, str)
            or _REPOSITORY_RE.fullmatch(self.repository) is None
            or self.repository != self.repository.casefold()
        ):
            raise ValueError("repository must be one canonical lowercase OWNER/REPOSITORY")
        _positive_identifier(self.issue_number, "issue_number")
        _positive_identifier(self.pr_number, "pr_number")
        repo_root = _absolute_canonical_path(self.repo_root, "repo_root")
        worktree = _absolute_canonical_path(self.worktree_path, "worktree_path")
        if worktree != repo_root and repo_root not in worktree.parents:
            raise ValueError("worktree_path must be inside repo_root")
        if (
            not isinstance(self.branch, str)
            or not self.branch
            or self.branch.strip() != self.branch
            or ".." in self.branch
            or self.branch.startswith(("/", "-"))
            or any(ord(character) < 32 for character in self.branch)
        ):
            raise ValueError("branch must be a canonical branch name")
        _full_sha(self.reviewed_parent_sha, "reviewed_parent_sha")
        _full_sha(self.candidate_tree_sha, "candidate_tree_sha")
        _full_sha(self.recovery_commit_sha, "recovery_commit_sha")
        if (
            not isinstance(self.changed_paths, tuple)
            or not self.changed_paths
            or len(self.changed_paths) > DIRTY_SNAPSHOT_CHANGED_FILE_MAX
            or len(set(self.changed_paths)) != len(self.changed_paths)
            or any(
                not isinstance(path, str)
                or not path
                or "\0" in path
                or Path(path).is_absolute()
                or Path(path).as_posix() != path
                or any(part in {"", ".", "..", ".git"} for part in Path(path).parts)
                for path in self.changed_paths
            )
            or sum(len(os.fsencode(path)) + 1 for path in self.changed_paths)
            > IMPLEMENTATION_INSPECTION_STATUS_MAX_BYTES
        ):
            raise ValueError("changed_paths must be a bounded ordered safe-path tuple")
        if (
            not isinstance(self.committed_diff, str)
            or len(self.committed_diff.encode("utf-8")) > IMPLEMENTATION_INSPECTION_DIFF_MAX_BYTES
        ):
            raise ValueError("committed_diff exceeds its UTF-8 limit")
        if (
            self.committed_diff_sha256
            != hashlib.sha256(self.committed_diff.encode("utf-8")).hexdigest()
        ):
            raise ValueError("committed_diff_sha256 does not match committed_diff")
        if (
            not isinstance(self.failure_diagnostic, str)
            or len(self.failure_diagnostic.encode("utf-8")) > REMEDIATION_DIAGNOSTIC_MAX_BYTES
        ):
            raise ValueError("failure_diagnostic exceeds its UTF-8 limit")
        if (
            not isinstance(self.thread_snapshot_json, str)
            or len(self.thread_snapshot_json.encode("utf-8"))
            > REMEDIATION_THREAD_SNAPSHOT_MAX_BYTES
        ):
            raise ValueError("thread_snapshot_json exceeds its UTF-8 limit")
        _thread_ids(self.thread_snapshot_json)
        if (
            self.thread_snapshot_sha256
            != hashlib.sha256(self.thread_snapshot_json.encode("utf-8")).hexdigest()
        ):
            raise ValueError("thread_snapshot_sha256 does not match thread_snapshot_json")
        if len(self.canonical_bytes) > REMEDIATION_CANONICAL_INPUT_MAX_BYTES:
            raise ValueError("canonical remediation review input exceeds its byte limit")

    @staticmethod
    def canonical_thread_snapshot(threads: object) -> str:
        """Return canonical JSON for one thread snapshot."""
        if not isinstance(threads, list) or not threads:
            raise ValueError("thread snapshot must be a non-empty list")
        projected: list[dict[str, object]] = []
        seen_thread_ids: set[str] = set()
        for thread in threads:
            if not isinstance(thread, dict):
                raise ValueError("thread snapshot must contain thread objects")
            thread_id = thread.get("thread_id") or thread.get("id")
            resolved = thread.get("isResolved", False)
            path = thread.get("path", "")
            line = thread.get("line")
            side = thread.get("side") or "RIGHT"
            comments = thread.get("comments")
            if (
                not isinstance(thread_id, str)
                or not thread_id
                or thread_id in seen_thread_ids
                or not isinstance(resolved, bool)
                or not isinstance(path, str)
                or isinstance(line, bool)
                or (line is not None and not isinstance(line, int))
                or not isinstance(side, str)
                or not side
                or not isinstance(comments, list)
                or not comments
            ):
                raise ValueError("thread snapshot contains an invalid thread")
            seen_thread_ids.add(thread_id)
            projected_comments: list[dict[str, str]] = []
            seen_comment_ids: set[str] = set()
            for comment in comments:
                if not isinstance(comment, dict):
                    raise ValueError("thread snapshot contains an invalid comment")
                comment_id = comment.get("id")
                body = comment.get("body")
                author_value = comment.get("author")
                author = (
                    author_value.get("login") if isinstance(author_value, dict) else author_value
                )
                if (
                    not isinstance(comment_id, str)
                    or not comment_id
                    or comment_id in seen_comment_ids
                    or not isinstance(author, str)
                    or not isinstance(body, str)
                ):
                    raise ValueError("thread snapshot contains an invalid comment")
                seen_comment_ids.add(comment_id)
                projected_comments.append({"id": comment_id, "author": author, "body": body})
            projected.append(
                {
                    "id": thread_id,
                    "isResolved": resolved,
                    "line": line,
                    "path": path,
                    "side": side,
                    "comments": projected_comments,
                }
            )
        encoded = _canonical_json(projected)
        _thread_ids(encoded)
        if len(encoded.encode("utf-8")) > REMEDIATION_THREAD_SNAPSHOT_MAX_BYTES:
            raise ValueError("thread snapshot exceeds its UTF-8 limit")
        return encoded

    @staticmethod
    def thread_snapshot_digest(threads: object) -> str:
        """Return the digest of one canonical thread snapshot."""
        return hashlib.sha256(
            RemediationReviewInput.canonical_thread_snapshot(threads).encode("utf-8")
        ).hexdigest()

    @property
    def canonical_bytes(self) -> bytes:
        """Return the canonical ordered record bytes."""
        return _canonical_json(
            [
                self.format_version,
                self.repository,
                self.issue_number,
                self.pr_number,
                self.repo_root,
                self.worktree_path,
                self.branch,
                self.reviewed_parent_sha,
                self.candidate_tree_sha,
                self.recovery_commit_sha,
                list(self.changed_paths),
                self.committed_diff_sha256,
                self.committed_diff,
                self.failure_diagnostic,
                self.thread_snapshot_sha256,
                self.thread_snapshot_json,
            ]
        ).encode("utf-8")

    @property
    def review_input_sha256(self) -> str:
        """Return the canonical record digest."""
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @classmethod
    def from_canonical_bytes(cls, encoded: bytes) -> Self:
        """Restore one canonical review input."""
        if not isinstance(encoded, bytes) or len(encoded) > REMEDIATION_CANONICAL_INPUT_MAX_BYTES:
            raise ValueError("canonical remediation review input exceeds its byte limit")
        try:
            raw = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("canonical remediation review input is invalid") from error
        if not isinstance(raw, list) or len(raw) != 16 or _canonical_json(raw).encode() != encoded:
            raise ValueError("canonical remediation review input does not use its exact schema")
        changed_paths = raw[10]
        if not isinstance(changed_paths, list):
            raise ValueError("canonical remediation changed_paths is invalid")
        try:
            return cls(
                format_version=raw[0],
                repository=raw[1],
                issue_number=raw[2],
                pr_number=raw[3],
                repo_root=raw[4],
                worktree_path=raw[5],
                branch=raw[6],
                reviewed_parent_sha=raw[7],
                candidate_tree_sha=raw[8],
                recovery_commit_sha=raw[9],
                changed_paths=tuple(changed_paths),
                committed_diff_sha256=raw[11],
                committed_diff=raw[12],
                failure_diagnostic=raw[13],
                thread_snapshot_sha256=raw[14],
                thread_snapshot_json=raw[15],
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"canonical remediation review input is invalid: {error}") from error


@dataclass(frozen=True, slots=True)
class RemediationRecoveryReceipt:
    """Hold one prepared commit and its exact recovery input."""

    review_input_bytes: str
    review_input_sha256: str
    journal_input_encoding: str
    journal_input_data: str
    expected_remote_sha: str
    content_snapshot: tuple[tuple[str, str], ...]
    add_paths: tuple[str, ...]
    update_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate the prepared commit, snapshot, and path identities."""
        try:
            review_input = RemediationReviewInput.from_canonical_bytes(
                self.review_input_bytes.encode("utf-8")
            )
        except (AttributeError, UnicodeError, ValueError) as error:
            raise ValueError("remediation recovery receipt input is invalid") from error
        if (
            review_input.review_input_sha256 != self.review_input_sha256
            or decode_remediation_review_input(
                self.journal_input_encoding,
                self.journal_input_data,
            )
            != review_input.canonical_bytes
            or review_input.reviewed_parent_sha != self.expected_remote_sha
            or (*self.add_paths, *self.update_paths) != review_input.changed_paths
            or tuple(sorted(self.content_snapshot)) != self.content_snapshot
            or {key for key, _value in self.content_snapshot} != _DIRTY_CONTENT_SNAPSHOT_KEYS
            or any(
                not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
                for _key, value in self.content_snapshot
            )
        ):
            raise ValueError("remediation recovery receipt identity is invalid")

    @property
    def review_input(self) -> RemediationReviewInput:
        """Return the validated canonical review input."""
        return RemediationReviewInput.from_canonical_bytes(self.review_input_bytes.encode("utf-8"))

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-safe durable receipt."""
        return {
            "review_input_bytes": self.review_input_bytes,
            "review_input_sha256": self.review_input_sha256,
            "journal_input_encoding": self.journal_input_encoding,
            "journal_input_data": self.journal_input_data,
            "expected_remote_sha": self.expected_remote_sha,
            "content_snapshot": dict(self.content_snapshot),
            "add_paths": list(self.add_paths),
            "update_paths": list(self.update_paths),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        """Restore one exact durable receipt."""
        required = {
            "review_input_bytes",
            "review_input_sha256",
            "journal_input_encoding",
            "journal_input_data",
            "expected_remote_sha",
            "content_snapshot",
            "add_paths",
            "update_paths",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError("remediation recovery receipt schema is invalid")
        snapshot = value["content_snapshot"]
        add_paths = value["add_paths"]
        update_paths = value["update_paths"]
        if (
            not isinstance(snapshot, dict)
            or not all(
                isinstance(key, str) and isinstance(item, str) for key, item in snapshot.items()
            )
            or not isinstance(add_paths, list)
            or not all(isinstance(path, str) for path in add_paths)
            or not isinstance(update_paths, list)
            or not all(isinstance(path, str) for path in update_paths)
        ):
            raise ValueError("remediation recovery receipt schema is invalid")
        return cls(
            review_input_bytes=value["review_input_bytes"],
            review_input_sha256=value["review_input_sha256"],
            journal_input_encoding=value["journal_input_encoding"],
            journal_input_data=value["journal_input_data"],
            expected_remote_sha=value["expected_remote_sha"],
            content_snapshot=tuple(sorted(snapshot.items())),
            add_paths=tuple(add_paths),
            update_paths=tuple(update_paths),
        )


@dataclass(frozen=True, slots=True)
class RemediationReplyResult:
    """Bind one exhaustive reply map to a remediation review input."""

    review_input_sha256: str
    replies: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        """Validate the input digest and immutable exhaustive reply map."""
        if (
            not isinstance(self.review_input_sha256, str)
            or _SHA256_RE.fullmatch(self.review_input_sha256) is None
        ):
            raise ValueError("review_input_sha256 must be a lowercase SHA-256 digest")
        if (
            not isinstance(self.replies, tuple)
            or not self.replies
            or any(
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not entry[0]
                or not isinstance(entry[1], str)
                or not 0 < len(entry[1].strip()) <= MAX_ADDRESS_REPLY_CHARS
                or entry[1] != entry[1].strip()
                for entry in self.replies
            )
            or tuple(sorted(self.replies)) != self.replies
            or len({thread_id for thread_id, _reply in self.replies}) != len(self.replies)
        ):
            raise ValueError("replies must be a sorted non-empty immutable reply map")
        if len(self.canonical_bytes) > REMEDIATION_REPLY_RESULT_MAX_BYTES:
            raise ValueError("remediation reply result exceeds its byte limit")

    @classmethod
    def create(
        cls,
        *,
        review_input_sha256: str,
        replies: dict[str, str],
        thread_snapshot_json: str,
    ) -> Self:
        """Create one validated reply result."""
        expected_ids = set(_thread_ids(thread_snapshot_json))
        if not isinstance(replies, dict) or set(replies) != expected_ids:
            raise ValueError("reply IDs must exactly match the thread snapshot")
        return cls(review_input_sha256, tuple(sorted(replies.items())))

    @property
    def canonical_bytes(self) -> bytes:
        """Return the canonical reply-result bytes."""
        return _canonical_json(
            {
                "replies": dict(self.replies),
                "review_input_sha256": self.review_input_sha256,
            }
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        """Return the reply-result digest."""
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-safe reply result."""
        return {
            "review_input_sha256": self.review_input_sha256,
            "replies": dict(self.replies),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        """Restore one reply result."""
        if not isinstance(value, dict) or set(value) != {"review_input_sha256", "replies"}:
            raise ValueError("remediation reply result has an invalid schema")
        digest = value.get("review_input_sha256")
        replies = value.get("replies")
        if not isinstance(digest, str) or not isinstance(replies, dict):
            raise ValueError("remediation reply result is invalid")
        if not all(
            isinstance(key, str) and isinstance(reply, str) for key, reply in replies.items()
        ):
            raise ValueError("remediation reply result is invalid")
        return cls(digest, tuple(sorted(replies.items())))
