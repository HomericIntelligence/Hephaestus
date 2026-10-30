"""Admit an installed source profile and select its validation commands.

The source adapter must bind each supplied object and the complete path
inventory to an immutable revision. Profile admission does not execute
repository code. The CI reader permits only fixed read-only GitHub endpoints.

This module owns no repository profile. It resolves the profile that an
installed distribution registers in
:mod:`hephaestus.automation.pipeline.repository_validation_profiles`. The
profile owns the reviewed control inventory, the fixed command selection, and
the provider CI expectations. This module owns admission, identity, two
complete observations, deadlines, and transport limits.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast

from hephaestus.agents.workspace import WorkspaceBinding
from hephaestus.config.child_environments import build_gh_child_env
from hephaestus.utils.helpers import SubprocessOutputLimitExceeded, run_subprocess

from .pipeline.repository_validation import (
    RepositoryValidationCheck,
    RepositoryValidationGap,
    RepositoryValidationInvocation,
    RepositoryValidationPlan,
    RepositoryValidationReceipt,
)
from .pipeline.repository_validation_profiles import (
    RepositoryValidationProfile,
    ValidationCIJob,
    ValidationCIStep,
    require_repository_validation_profile,
)
from .remote_git import trusted_gh_executable
from .worktree_snapshot import _controlled_git_env, _run_bounded_git_output

logger = logging.getLogger(__name__)

SOURCE_INVENTORY_MAX = 500_000
TREE_PATH_BYTES_MAX = 64 * 1024 * 1024
CONTROL_CACHE_BYTES_MAX = 64 * 1024 * 1024
CONTROL_FILE_BYTES_MAX = 16 * 1024 * 1024
CONTROL_BLOB_LIMIT = 32
CONTROL_CACHE_MAX = 256
CHANGES_MAX = 8192
SOURCE_TIMEOUT_MAX_S = 120
_REGULAR_MODES = {0o100644, 0o100755}


def _valid_path(value: object) -> bool:
    """Accept one relative repository path without traversal."""
    return (
        type(value) is str
        and 0 < len(value) <= 1024
        and "\0" not in value
        and "\\" not in value
        and ":" not in value
        and not PurePosixPath(value).is_absolute()
        and all(part not in {"", ".", "..", ".git"} for part in value.split("/"))
    )


def _control_sizes(profile: RepositoryValidationProfile) -> dict[str, int]:
    """Return the admitted control size for each bound control path."""
    return {path: size for path, _mode, size, _digest in profile.controls}


def _control_modes(profile: RepositoryValidationProfile) -> dict[str, int]:
    """Return the admitted control mode for each bound control path."""
    return {path: mode for path, mode, _size, _digest in profile.controls}


class _ProfileGitSource:
    """Read immutable Git objects within one deadline and fixed output limits."""

    def __init__(self, root: Path, timeout_s: float, shutdown: threading.Event | None) -> None:
        self.root = root
        self.deadline = time.monotonic() + timeout_s
        self.shutdown = shutdown
        self.env = _controlled_git_env()
        self.env.update(
            {
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_GRAFT_FILE": os.devnull,
                "GIT_NO_LAZY_FETCH": "1",
            }
        )

    def read(self, *args: str, limit: int) -> str:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("The source inspection deadline expired.")
        result = _run_bounded_git_output(
            ("git", "--no-replace-objects", *args),
            cwd=self.root,
            timeout=remaining,
            max_bytes=limit,
            retain_text=True,
            env=self.env,
            shutdown=self.shutdown,
        )
        # The shared reader preserves arbitrary bytes. This profile requires UTF-8.
        result.text.encode("utf-8", errors="strict")
        return result.text

    def admit_revision(self, revision: str, profile: RepositoryValidationProfile) -> str | None:
        """Return the profile admitted at one immutable revision.

        A revision whose controls the profile does not bind returns ``None``.
        The caller then continues without local validation coverage instead of
        refusing the review.
        """
        tree = self.read("ls-tree", "-r", "-z", "--full-tree", revision, limit=TREE_PATH_BYTES_MAX)
        if not tree or not tree.endswith("\0"):
            raise ValueError("The Git source inventory is incomplete.")
        inventory: list[str] = []
        members: dict[str, tuple[int, str]] = {}
        for record in tree[:-1].split("\0"):
            metadata, separator, path = record.partition("\t")
            fields = metadata.split(" ")
            if (
                not separator
                or len(fields) != 3
                or re.fullmatch(r"[0-7]{6}", fields[0]) is None
                or fields[1] not in {"blob", "commit"}
                or re.fullmatch(r"[0-9a-f]{40}", fields[2]) is None
                or not _valid_path(path)
                or path in members
            ):
                raise ValueError("The Git source inventory record is invalid.")
            mode = int(fields[0], 8)
            if mode not in _REGULAR_MODES or fields[1] != "blob":
                raise ValueError("The Git source inventory contains a nonregular file.")
            inventory.append(path)
            members[path] = (mode, fields[2])
            if len(inventory) > SOURCE_INVENTORY_MAX:
                raise ValueError("The Git source inventory exceeds its limit.")
        absent = tuple(sorted(profile.control_paths - members.keys()))
        if absent:
            logger.warning(
                "validation profile %s names %d control path(s) absent from revision %s: %s; "
                "continuing without local validation coverage",
                profile.profile_id,
                len(absent),
                revision,
                ", ".join(absent[:4]),
            )
            return None
        sizes = _control_sizes(profile)
        controls: dict[str, tuple[int, bytes]] = {}
        for path in sorted(members.keys() & profile.control_paths):
            mode, object_id = members[path]
            object_size = self.read("cat-file", "-s", object_id, limit=CONTROL_BLOB_LIMIT).strip()
            if not object_size.isdigit() or int(object_size) != sizes[path]:
                logger.warning(
                    "validation profile %s binds control %s at %d bytes but revision %s has %s; "
                    "continuing without local validation coverage",
                    profile.profile_id,
                    path,
                    sizes[path],
                    revision,
                    object_size if object_size.isdigit() else "an unreadable size",
                )
                return None
            data = self.read("cat-file", "blob", object_id, limit=int(object_size) + 1).encode(
                "utf-8"
            )
            controls[path] = (mode, data)
        try:
            return profile.admit_controls(controls, tuple(inventory))
        except ValueError as error:
            logger.warning(
                "validation profile %s did not admit the controls at revision %s: %s; "
                "continuing without local validation coverage",
                profile.profile_id,
                revision,
                error,
            )
            return None

    def changes(self, base: str, head: str) -> tuple[str, tuple[tuple[str, str], ...]]:
        """Return the unique merge base and the complete changed-path records."""
        branchpoint = self.read("merge-base", "--all", base, head, limit=128).strip()
        if re.fullmatch(r"[0-9a-f]{40}", branchpoint) is None:
            raise ValueError("The review has no unique immutable merge base.")
        raw = self.read(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-status",
            "-z",
            branchpoint,
            head,
            "--",
            limit=8 * 1024 * 1024,
        )
        if not raw:
            return branchpoint, ()
        if not raw.endswith("\0"):
            raise ValueError("The Git change inventory is incomplete.")
        fields = raw[:-1].split("\0")
        if len(fields) % 2 or len(fields) > CHANGES_MAX:
            raise ValueError("The Git change inventory exceeds its record limit.")
        return branchpoint, tuple(zip(fields[::2], fields[1::2], strict=True))


def validation_plan_for_workspace(
    workspace: WorkspaceBinding,
    *,
    repository: str,
    issue_number: int | None,
    pr_number: int,
    reviewed_base: str,
    timeout_s: float,
    shutdown: threading.Event | None = None,
) -> RepositoryValidationPlan:
    """Bind admitted controls and real change statuses to immutable Git commits.

    The caller holds the source lease. The PR target commit remains distinct
    from the merge base used to collect changes. No worktree file is executed.
    When the installed profile does not admit the controls at both reviewed
    revisions, this returns a plan that does not permit execution, so the
    review continues on provider evidence instead of stopping.
    """
    profile = require_repository_validation_profile(repository)
    if type(workspace) is not WorkspaceBinding or not isinstance(workspace.revision, str):
        raise ValueError("The source binding type is invalid.")
    plan = RepositoryValidationPlan(
        repository=profile.repository,
        issue_number=issue_number,
        pr_number=pr_number,
        reviewed_head=workspace.revision,
        reviewed_base=reviewed_base,
        source_workspace=workspace,
        changes=(),
        profile_id=profile.profile_id,
        profile_digest=profile.profile_digest,
        checks=(),
        execution_allowed=False,
        coverage_reason="Immutable source inspection is incomplete.",
    )
    if type(timeout_s) not in {int, float} or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("The source inspection timeout is invalid.")
    source = _ProfileGitSource(workspace.cwd, min(timeout_s, SOURCE_TIMEOUT_MAX_S), shutdown)
    if source.read("rev-parse", "--is-shallow-repository", limit=16).strip() != "false":
        raise ValueError("A complete Git history is necessary for source inspection.")
    profiles: list[str | None] = []
    for revision in (plan.reviewed_head, plan.reviewed_base):
        commit = source.read("rev-parse", "--verify", f"{revision}^{{commit}}", limit=64).strip()
        if commit != revision:
            raise ValueError("The source revision is not an immutable commit.")
        profiles.append(source.admit_revision(revision, profile))
    if profiles[0] != profiles[1] or profiles[0] is None:
        logger.warning(
            "validation profile %s admits reviewed revisions %s and %s differently; "
            "continuing without local validation coverage",
            profile.profile_id,
            plan.reviewed_head,
            plan.reviewed_base,
        )
        return replace(
            plan,
            execution_allowed=False,
            coverage_reason="The validation controls are unavailable for one reviewed revision.",
        )
    diff_base, changes = source.changes(plan.reviewed_base, plan.reviewed_head)
    checks = profile.select(changes)
    return replace(
        plan,
        changes=changes,
        checks=checks,
        diff_base_sha=diff_base,
        execution_allowed=bool(checks),
        coverage_reason="" if checks else "No applicable validation check was selected.",
    )


_CI_ID = r"[1-9][0-9]{0,19}"
_CI_SHA = r"[0-9a-f]{40}"
_CI_RESPONSE_BYTES = 4 * 1024 * 1024
_CI_AGGREGATE_BYTES = 32 * 1024 * 1024
_CI_READS_MAX = 600
_CI_OBJECTS_MAX = 512
_CI_STATUSES = frozenset({"queued", "in_progress", "completed", "waiting", "requested", "pending"})
_CI_CONCLUSIONS = frozenset(
    {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    }
)
_CI_DEADLINE_MAX_S = 120


def _endpoint_pattern(repository: str, workflow_path: str) -> re.Pattern[str]:
    """Build the closed read-only endpoint pattern for one profile repository."""
    workflow = PurePosixPath(workflow_path).name.removesuffix(".yml").removesuffix(".yaml")
    return re.compile(
        rf"repos/{re.escape(repository)}/(?:"
        rf"pulls/{_CI_ID}"
        rf"|actions/runs/{_CI_ID}"
        rf"|actions/runs/{_CI_ID}/attempts/{_CI_ID}/jobs\?per_page=100&page=(?:[1-9]|10)"
        rf"|actions/workflows/{re.escape(workflow)}\.yml/runs"
        rf"\?event=pull_request&head_sha={_CI_SHA}&per_page=100&page=[1-5]"
        rf"|git/commits/{_CI_SHA}"
        rf"|git/trees/{_CI_SHA}(?:\?recursive=1)?"
        rf"|git/blobs/{_CI_SHA}"
        r")"
    )


class ValidationCIReadError(RuntimeError):
    """Report a bounded CI failure code without provider output."""


def _ci_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("The provider JSON repeats a member.")
        result[key] = value
    return result


def _ci_json_constant(value: str) -> NoReturn:
    raise ValueError("The provider JSON contains a nonfinite constant.")


def _ci_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("The provider JSON contains a nonfinite number.")
    return number


def _ci_json_text_is_valid(value: object) -> None:
    pending = [value]
    while pending:
        member = pending.pop()
        if isinstance(member, str):
            member.encode("utf-8", errors="strict")
        elif isinstance(member, dict):
            pending.extend(member.keys())
            pending.extend(member.values())
        elif isinstance(member, list):
            pending.extend(member)


class ValidationCIReader:
    """Keep all provider reads inside one deadline and aggregate budget."""

    def __init__(
        self,
        repository: str,
        workflow_path: str,
        *,
        deadline_s: float,
        shutdown: threading.Event | None = None,
    ) -> None:
        """Capture the fixed request deadline without starting a child process."""
        if type(deadline_s) not in {int, float} or not math.isfinite(deadline_s) or deadline_s <= 0:
            raise ValueError("The CI request deadline is invalid.")
        self.repository = repository
        self._endpoints = _endpoint_pattern(repository, workflow_path)
        self.deadline_s = min(deadline_s, time.monotonic() + _CI_DEADLINE_MAX_S)
        self.shutdown = shutdown
        self._reads = 0
        self._objects = 0
        self._json_bytes = 0
        self._failure: str | None = None
        self._executable = trusted_gh_executable()

    def _fail(self, code: str) -> NoReturn:
        self._failure = code
        raise ValidationCIReadError(code) from None

    def _remaining(self) -> float:
        if self._failure is not None:
            self._fail(self._failure)
        if self.shutdown is not None and self.shutdown.is_set():
            self._fail("ci_request_cancelled")
        remaining = self.deadline_s - time.monotonic()
        if remaining <= 0:
            self._fail("ci_request_deadline")
        return remaining

    def _run(self, endpoint: str, remaining: float, output_limit: int) -> str:
        if self._executable is None:
            self._fail("ci_cli_unavailable")
        try:
            result = run_subprocess(
                [
                    str(self._executable),
                    "api",
                    "--hostname",
                    "github.com",
                    "--method",
                    "GET",
                    endpoint,
                ],
                env=build_gh_child_env(),
                timeout=remaining,
                check=False,
                log_on_error=False,
                track_process_group=True,
                shutdown=self.shutdown,
                remaining_timeout=self._remaining,
                max_output_bytes=output_limit,
            )
        except SubprocessOutputLimitExceeded:
            self._fail(
                "ci_aggregate_byte_limit"
                if output_limit < _CI_RESPONSE_BYTES
                else "ci_response_byte_limit"
            )
        except subprocess.TimeoutExpired:
            self._fail("ci_request_deadline")
        except InterruptedError:
            self._fail("ci_request_cancelled")
        except ValidationCIReadError:
            raise
        except (OSError, RuntimeError, subprocess.SubprocessError):
            self._fail("ci_transport_failure")
        self._remaining()
        if result.returncode != 0:
            self._fail(
                "ci_rate_limit" if "rate limit" in result.stderr.lower() else "ci_provider_failure"
            )
        return result.stdout

    def read(self, endpoint: str) -> dict[str, Any]:
        """Read one finite-profile endpoint with no pagination or retry expansion."""
        remaining = self._remaining()
        if type(endpoint) is not str or self._endpoints.fullmatch(endpoint) is None:
            self._fail("ci_endpoint_invalid")
        if self._reads >= _CI_READS_MAX:
            self._fail("ci_request_count_limit")
        is_object = "/git/trees/" in endpoint or "/git/blobs/" in endpoint
        if is_object and self._objects >= _CI_OBJECTS_MAX:
            self._fail("ci_object_count_limit")
        available = _CI_AGGREGATE_BYTES - self._json_bytes
        if available <= 0:
            self._fail("ci_aggregate_byte_limit")
        output_limit = min(available, _CI_RESPONSE_BYTES)
        self._reads += 1
        self._objects += int(is_object)
        text = self._run(endpoint, remaining, output_limit)
        try:
            encoded_size = len(text.encode("utf-8", errors="strict"))
            if encoded_size > output_limit:
                self._fail("ci_response_byte_limit")
            self._json_bytes += encoded_size
            # The shared process reader replaces invalid UTF-8. Do not admit
            # that replacement as provider evidence.
            if "\ufffd" in text:
                raise ValueError("The provider JSON contains replacement text.")
            value = json.loads(
                text,
                object_pairs_hook=_ci_json_object,
                parse_constant=_ci_json_constant,
                parse_float=_ci_json_float,
            )
            if not isinstance(value, dict):
                raise ValueError("The provider JSON root is not an object.")
            _ci_json_text_is_valid(value)
        except (ValueError, RecursionError):
            self._fail("ci_json_invalid")
        self._remaining()
        return value


@dataclass(frozen=True, slots=True)
class ValidationCIIdentity:
    """Bind provider identity facts without claiming command or source coverage."""

    repository_id: int
    pr_number: int
    reviewed_head: str
    reviewed_base: str
    head_branch: str
    run_id: int
    run_attempt: int
    status: str
    conclusion: str | None
    merge_sha: str
    merge_tree: str
    workflows: tuple[tuple[str, str, str], ...]


def _ci_require(condition: bool) -> None:
    if not condition:
        raise ValidationCIReadError("ci_identity_invalid")


def _ci_object(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValidationCIReadError("ci_identity_invalid")
    return value


def _ci_positive(value: object) -> bool:
    return type(value) is int and value > 0


def _ci_full_sha(value: object) -> bool:
    return type(value) is str and re.fullmatch(_CI_SHA, value) is not None


def _ci_repository_id(value: object, repository: str) -> int:
    repo = _ci_object(value)
    name = repo.get("full_name")
    identifier = repo.get("id")
    _ci_require(
        type(name) is str
        and name.casefold() == repository.casefold()
        and _ci_positive(identifier)
        and repo.get("fork") is False
    )
    return cast(int, identifier)


def validation_ci_identity(
    profile: RepositoryValidationProfile,
    pr: object,
    run: object,
    merge_commit: object,
    *,
    pr_number: int,
    reviewed_head: str,
    reviewed_base: str,
    head_branch: str,
) -> ValidationCIIdentity:
    """Check the retained PR, run, and immutable merge witness as one identity.

    This result does not prove control bytes, job steps, or stable reads. The
    provider adapter must prove those facts before it issues any receipt.
    """
    _ci_require(
        _ci_positive(pr_number)
        and _ci_full_sha(reviewed_head)
        and _ci_full_sha(reviewed_base)
        and type(head_branch) is str
        and 0 < len(head_branch) <= 1024
        and not any(ord(char) < 32 or ord(char) == 127 for char in head_branch)
    )
    pull = _ci_object(pr)
    workflow = _ci_object(run)
    merge = _ci_object(merge_commit)
    head = _ci_object(pull.get("head"))
    base = _ci_object(pull.get("base"))
    _ci_require(
        _ci_positive(pull.get("number"))
        and pull["number"] == pr_number
        and pull.get("state") == "open"
        and head.get("sha") == reviewed_head
        and head.get("ref") == head_branch
        and base.get("sha") == reviewed_base
        and base.get("ref") == "main"
    )
    repository = profile.repository
    repository_id = _ci_repository_id(base.get("repo"), repository)
    _ci_require(
        all(
            _ci_repository_id(repo, repository) == repository_id
            for repo in (
                head.get("repo"),
                workflow.get("repository"),
                workflow.get("head_repository"),
            )
        )
    )
    run_id = workflow.get("id")
    attempt = workflow.get("run_attempt")
    status = workflow.get("status")
    conclusion = workflow.get("conclusion")
    _ci_require(
        _ci_positive(run_id)
        and _ci_positive(attempt)
        and workflow.get("head_sha") == reviewed_head
        and workflow.get("head_branch") == head_branch
        and workflow.get("path") == profile.ci_workflow_path
        and workflow.get("event") == "pull_request"
        and type(status) is str
        and status in _CI_STATUSES
        and (conclusion is None or (type(conclusion) is str and conclusion in _CI_CONCLUSIONS))
        and ((status == "completed") == (conclusion is not None))
    )
    witnesses = workflow.get("referenced_workflows")
    _ci_require(type(witnesses) is list and len(witnesses) == len(profile.ci_referenced_workflows))
    records: list[tuple[str, str, str]] = []
    paths: set[str] = set()
    merge_shas: set[str] = set()
    expected_ref = f"refs/pull/{pr_number}/merge"
    for item in cast(list[object], witnesses):
        witness = _ci_object(item)
        path, sha, ref = (witness.get(key) for key in ("path", "sha", "ref"))
        _ci_require(type(path) is str and _ci_full_sha(sha) and ref == expected_ref)
        source_path, separator, source_sha = cast(str, path).partition("@")
        _ci_require(
            separator == "@"
            and source_sha == sha
            and source_path.casefold().startswith(f"{repository}/".casefold())
        )
        local_path = source_path[len(repository) + 1 :]
        _ci_require(local_path in profile.ci_referenced_workflows and local_path not in paths)
        paths.add(local_path)
        merge_shas.add(cast(str, sha))
        records.append((local_path, cast(str, sha), expected_ref))
    _ci_require(len(merge_shas) == 1)
    merge_sha = next(iter(merge_shas))
    parents = merge.get("parents")
    _ci_require(merge.get("sha") == merge_sha and type(parents) is list and len(parents) == 2)
    _ci_require(
        [_ci_object(parent).get("sha") for parent in cast(list[object], parents)]
        == [reviewed_base, reviewed_head]
    )
    tree_sha = _ci_object(merge.get("tree")).get("sha")
    _ci_require(_ci_full_sha(tree_sha))
    return ValidationCIIdentity(
        repository_id=repository_id,
        pr_number=pr_number,
        reviewed_head=reviewed_head,
        reviewed_base=reviewed_base,
        head_branch=head_branch,
        run_id=cast(int, run_id),
        run_attempt=cast(int, attempt),
        status=cast(str, status),
        conclusion=conclusion,
        merge_sha=merge_sha,
        merge_tree=cast(str, tree_sha),
        workflows=tuple(sorted(records)),
    )


class ValidationCISourceControls:
    """Check immutable control objects with a cache limited to one request."""

    def __init__(self, reader: ValidationCIReader, profile: RepositoryValidationProfile) -> None:
        """Use the existing reader and its complete-request resource limits."""
        self._reader = reader
        self._profile = profile
        self._commits: dict[str, str] = {}
        self._profiles: dict[str, str] = {}
        self._blobs: dict[str, bytes] = {}
        self._decoded_bytes = 0

    def admit(self, commit_sha: str, *, expected_tree: str | None = None) -> str:
        """Require complete controls at the requested immutable commit."""
        self._reader._remaining()
        try:
            if not _ci_full_sha(commit_sha) or (
                expected_tree is not None and not _ci_full_sha(expected_tree)
            ):
                raise ValueError("The source object identity is invalid.")
            tree_sha = self._commits.get(commit_sha)
            if tree_sha is None:
                repository = self._profile.repository
                commit = self._reader.read(f"repos/{repository}/git/commits/{commit_sha}")
                tree = commit.get("tree")
                if (
                    commit.get("sha") != commit_sha
                    or type(tree) is not dict
                    or not _ci_full_sha(tree.get("sha"))
                ):
                    raise ValueError("The source commit is invalid.")
                tree_sha = cast(str, tree["sha"])
                self._commits[commit_sha] = tree_sha
            if expected_tree is not None and tree_sha != expected_tree:
                raise ValueError("The source tree does not match the witness.")
            if tree_sha not in self._profiles:
                self._profiles[tree_sha] = self._admit_tree(tree_sha)
            profile = self._profiles[tree_sha]
        except (ValueError, TypeError, KeyError):
            raise ValidationCIReadError("ci_source_invalid") from None
        self._reader._remaining()
        return profile

    def _admit_tree(self, tree_sha: str) -> str:
        profile = self._profile
        tree = self._reader.read(f"repos/{profile.repository}/git/trees/{tree_sha}?recursive=1")
        entries = tree.get("tree")
        if (
            tree.get("sha") != tree_sha
            or tree.get("truncated") is not False
            or type(entries) is not list
            or len(entries) > SOURCE_INVENTORY_MAX
        ):
            raise ValueError("The source tree is incomplete.")
        directories: set[str] = set()
        members: dict[str, tuple[int, str, int]] = {}
        seen: set[str] = set()
        path_bytes = 0
        for entry in entries:
            if type(entry) is not dict:
                raise ValueError("The source tree member is invalid.")
            path = entry.get("path")
            if not _valid_path(path) or path in seen or not _ci_full_sha(entry.get("sha")):
                raise ValueError("The source path is invalid.")
            path = cast(str, path)
            seen.add(path)
            path_bytes += len(path.encode("utf-8")) + 1
            if path_bytes > TREE_PATH_BYTES_MAX:
                raise ValueError("The source path inventory exceeds its limit.")
            if entry.get("mode") == "040000" and entry.get("type") == "tree":
                directories.add(path)
                continue
            size = entry.get("size")
            if (
                entry.get("mode") not in ("100644", "100755")
                or entry.get("type") != "blob"
                or type(size) is not int
                or size < 0
            ):
                raise ValueError("The source file is not regular.")
            members[path] = (int(entry["mode"], 8), entry["sha"], size)
        if any(
            str(parent) != "." and str(parent) not in directories
            for path in seen
            for parent in PurePosixPath(path).parents
        ):
            raise ValueError("The source tree has a missing parent directory.")
        modes = _control_modes(profile)
        sizes = _control_sizes(profile)
        if not members.keys() >= profile.control_paths:
            raise ValueError("The source control inventory is incomplete.")
        controls: dict[str, tuple[int, bytes]] = {}
        for path in sorted(members.keys() & profile.control_paths):
            mode, blob_sha, size = members[path]
            if mode != modes[path] or size != sizes[path] or size > CONTROL_FILE_BYTES_MAX:
                raise ValueError("The source control metadata changed.")
            controls[path] = (mode, self._blob(blob_sha, size))
        return profile.admit_controls(controls, tuple(members))

    def _blob(self, blob_sha: str, expected_size: int) -> bytes:
        cached = self._blobs.get(blob_sha)
        if cached is not None:
            if len(cached) != expected_size:
                raise ValueError("The cached source size is inconsistent.")
            return cached
        if (
            len(self._blobs) >= CONTROL_CACHE_MAX
            or self._decoded_bytes + expected_size > CONTROL_CACHE_BYTES_MAX
        ):
            raise ValueError("The source control cache exceeds its limit.")
        blob = self._reader.read(f"repos/{self._profile.repository}/git/blobs/{blob_sha}")
        content = blob.get("content")
        if (
            blob.get("sha") != blob_sha
            or type(blob.get("size")) is not int
            or blob["size"] != expected_size
            or blob.get("encoding") != "base64"
            or type(content) is not str
        ):
            raise ValueError("The source blob metadata is invalid.")
        data = base64.b64decode(content.replace("\n", ""), validate=True)
        if (
            len(data) != expected_size
            or hashlib.sha1(
                f"blob {len(data)}\0".encode("ascii") + data, usedforsecurity=False
            ).hexdigest()
            != blob_sha
        ):
            raise ValueError("The source blob digest is invalid.")
        self._blobs[blob_sha] = data
        self._decoded_bytes += len(data)
        return data


@dataclass(frozen=True, slots=True)
class ValidationCICollection:
    """Return only receipts admitted by the complete provider proof."""

    receipts: tuple[RepositoryValidationReceipt, ...] = ()
    gaps: tuple[RepositoryValidationGap, ...] = ()


def _ci_pages(
    reader: ValidationCIReader, endpoint: str, member: str, page_limit: int
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    total: int | None = None
    for page in range(1, page_limit + 1):
        response = reader.read(f"{endpoint}&page={page}")
        count, items = response.get("total_count"), response.get(member)
        if (
            type(count) is not int
            or not 0 <= count <= page_limit * 100
            or (total is not None and total != count)
            or type(items) is not list
            or len(items) != min(100, count - len(records))
            or response.get("truncated", False) is not False
        ):
            raise ValidationCIReadError("ci_pagination_invalid")
        total = count
        for item in items:
            if type(item) is not dict or not _ci_positive(item.get("id")) or item["id"] in seen:
                raise ValidationCIReadError("ci_pagination_invalid")
            seen.add(item["id"])
            records.append(item)
        if len(records) == total:
            reader._remaining()
            return tuple(records)
    raise ValidationCIReadError("ci_pagination_limit")


def _ci_status(record: dict[str, Any]) -> tuple[str, str | None]:
    status, conclusion = record.get("status"), record.get("conclusion")
    if (
        type(status) is not str
        or status not in _CI_STATUSES
        or (
            conclusion is not None
            and (type(conclusion) is not str or conclusion not in _CI_CONCLUSIONS)
        )
        or ((status == "completed") != (conclusion is not None))
    ):
        raise ValidationCIReadError("ci_job_status_invalid")
    return status, conclusion


def _ci_step(value: object) -> ValidationCIStep:
    record = _ci_object(value)
    number, name = record.get("number"), record.get("name")
    if not _ci_positive(number) or type(name) is not str or not 0 < len(name) <= 2048:
        raise ValidationCIReadError("ci_step_invalid")
    status, conclusion = _ci_status(record)
    return ValidationCIStep(cast(int, number), name, status, conclusion)


def _ci_jobs(
    records: tuple[dict[str, Any], ...], identity: ValidationCIIdentity
) -> tuple[ValidationCIJob, ...]:
    jobs: list[ValidationCIJob] = []
    for record in records:
        name, steps = record.get("name"), record.get("steps")
        if (
            not _ci_positive(record.get("id"))
            or type(record.get("run_id")) is not int
            or record["run_id"] != identity.run_id
            or type(record.get("run_attempt")) is not int
            or record["run_attempt"] != identity.run_attempt
            or record.get("head_sha") != identity.reviewed_head
            or type(name) is not str
            or not 0 < len(name) <= 1024
            or type(steps) is not list
            or len(steps) > 100
        ):
            raise ValidationCIReadError("ci_job_identity_invalid")
        normalized = tuple(sorted((_ci_step(step) for step in steps), key=lambda step: step.number))
        if len({step.number for step in normalized}) != len(normalized):
            raise ValidationCIReadError("ci_step_duplicate")
        status, conclusion = _ci_status(record)
        jobs.append(ValidationCIJob(record["id"], name, status, conclusion, normalized))
    return tuple(sorted(jobs, key=lambda job: job.identifier))


def _ci_observation(
    reader: ValidationCIReader,
    controls: ValidationCISourceControls,
    profile: RepositoryValidationProfile,
    invocation: RepositoryValidationInvocation,
    head_branch: str,
    run_id: int,
    attempt: int,
) -> tuple[ValidationCIIdentity, tuple[ValidationCIJob, ...]]:
    """Read one complete identity and job observation for the admitted profile."""
    plan = invocation.plan
    pull = reader.read(f"repos/{profile.repository}/pulls/{plan.pr_number}")
    run_endpoint = f"repos/{profile.repository}/actions/runs/{run_id}"
    run = reader.read(run_endpoint)
    if run.get("id") != run_id or run.get("run_attempt") != attempt:
        raise ValidationCIReadError("ci_run_changed")
    witnesses = run.get("referenced_workflows")
    if type(witnesses) is not list or not witnesses or type(witnesses[0]) is not dict:
        raise ValidationCIReadError("ci_merge_witness_invalid")
    merge_sha = witnesses[0].get("sha")
    if not _ci_full_sha(merge_sha):
        raise ValidationCIReadError("ci_merge_witness_invalid")
    merge = reader.read(f"repos/{profile.repository}/git/commits/{merge_sha}")
    identity = validation_ci_identity(
        profile,
        pull,
        run,
        merge,
        pr_number=plan.pr_number,
        reviewed_head=plan.reviewed_head,
        reviewed_base=plan.reviewed_base,
        head_branch=head_branch,
    )
    for sha, tree in (
        (plan.reviewed_head, None),
        (plan.reviewed_base, None),
        (identity.merge_sha, identity.merge_tree),
    ):
        if controls.admit(sha, expected_tree=tree) != plan.profile_id:
            raise ValidationCIReadError("ci_profile_changed")
    records = _ci_pages(reader, f"{run_endpoint}/attempts/{attempt}/jobs?per_page=100", "jobs", 10)
    jobs = _ci_jobs(records, identity)
    after = validation_ci_identity(
        profile,
        pull,
        reader.read(run_endpoint),
        merge,
        pr_number=plan.pr_number,
        reviewed_head=plan.reviewed_head,
        reviewed_base=plan.reviewed_base,
        head_branch=head_branch,
    )
    if after != identity:
        raise ValidationCIReadError("ci_run_changed")
    reader._remaining()
    return identity, jobs


def _ci_receipt(
    profile: RepositoryValidationProfile,
    plan: RepositoryValidationPlan,
    check: RepositoryValidationCheck,
    jobs: tuple[ValidationCIJob, ...],
) -> RepositoryValidationReceipt | None:
    """Return one receipt when the profile proves the exact reviewed command."""
    try:
        status = profile.ci_receipt_status(plan, check.check_id, jobs)
    except ValueError as error:
        raise ValidationCIReadError(str(error)) from None
    if status is None:
        return None
    return RepositoryValidationReceipt(
        repository=plan.repository,
        pr_number=plan.pr_number,
        plan_id=plan.plan_id,
        check_id=check.check_id,
        reviewed_head=plan.reviewed_head,
        reviewed_base=plan.reviewed_base,
        argv=check.argv,
        source_digests=check.source_digests,
        evidence_kind="ci",
        status=status,
    )


def collect_repository_validation_ci(
    invocation: RepositoryValidationInvocation,
    *,
    head_branch: str,
    reader: ValidationCIReader,
) -> ValidationCICollection:
    """Require two complete provider observations before issuing CI receipts."""
    try:
        if (
            type(invocation) is not RepositoryValidationInvocation
            or replace(invocation) != invocation
        ):
            raise ValueError("The CI invocation changed.")
        plan = invocation.plan
        profile = require_repository_validation_profile(plan.repository)
        if (
            invocation.evidence_kind != "ci"
            or not plan.execution_allowed
            or profile.profile_id != plan.profile_id
            or profile.profile_digest != plan.profile_digest
            or plan.checks != profile.select(plan.changes)
            or not set(invocation.check_ids) <= set(profile.ci_covered_check_ids(plan.checks))
        ):
            raise ValueError("The CI selection does not match the admitted profile.")
    except (ValueError, TypeError, AttributeError):
        return ValidationCICollection(gaps=(RepositoryValidationGap("*", "ci_plan_invalid"),))
    try:
        reader._remaining()
        workflow = PurePosixPath(profile.ci_workflow_path).name
        runs = _ci_pages(
            reader,
            f"repos/{profile.repository}/actions/workflows/{workflow}"
            f"/runs?event=pull_request&head_sha={plan.reviewed_head}&per_page=100",
            "workflow_runs",
            5,
        )
        if not runs:
            return ValidationCICollection()
        latest = max(runs, key=lambda run: run["id"])
        run_id, attempt = latest["id"], latest.get("run_attempt")
        if not _ci_positive(attempt):
            raise ValidationCIReadError("ci_run_attempt_invalid")
        controls = ValidationCISourceControls(reader, profile)
        first = _ci_observation(
            reader, controls, profile, invocation, head_branch, run_id, cast(int, attempt)
        )
        second = _ci_observation(
            reader, controls, profile, invocation, head_branch, run_id, cast(int, attempt)
        )
        if first != second:
            raise ValidationCIReadError("ci_observation_changed")
        receipts = tuple(
            receipt
            for check in plan.checks
            if check.check_id in invocation.check_ids
            if (receipt := _ci_receipt(profile, plan, check, first[1])) is not None
        )
        reader._remaining()
        return ValidationCICollection(receipts=receipts)
    except ValidationCIReadError as error:
        return ValidationCICollection(gaps=(RepositoryValidationGap("*", str(error)),))
