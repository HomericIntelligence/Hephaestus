"""Prepare bounded Mnemosyne learning changes at the host boundary.

This module converts a semantic :class:`LearningIntent` into the existing
PR-delivery request.  It deliberately has no dependency on agent providers or
generic ``AgentJob`` dispatch.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import hephaestus.automation.github_api as github_api
from hephaestus.automation.mnemosyne_binding import MnemosyneBindingReceipt
from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError, LearnDeliveryRequest
from hephaestus.automation.pipeline.work_item import LearningIntent, LearningIntentKind
from hephaestus.automation.review_journal import (
    IssueComment,
    is_plan_comment,
    journal_snapshot,
    plan_fingerprint,
)
from hephaestus.automation.state_labels import STATE_PLAN_GO, is_exclusive_plan_state
from hephaestus.github.client import gh_call
from hephaestus.io.utils import write_secure
from hephaestus.utils.helpers import NETWORK_TIMEOUT, run_subprocess, slugify

MAX_ARTIFACT_BYTES = 65_536
MAX_SOURCE_FIELD_CHARS = 16_384
VALIDATOR_ARGV = ("uv", "run", "--offline", "--frozen", "python", "scripts/validate_plugins.py")
VALIDATOR_TIMEOUT_S = 120
_REQUIRED_PLAN_SECTIONS = (
    "Objective",
    "Approach",
    "Implementation Order",
    "Verification",
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SHA_RE = re.compile(r"[0-9a-f]{40}")


class LearningSource(Protocol):
    """Marker protocol for typed immutable learning sources."""


@dataclass(frozen=True)
class ApprovedPlanLearningSource:
    """Validated canonical approved-plan source fields."""

    repository: str
    issue: int
    revision: int
    fingerprint: str
    comment_database_id: int
    source_date: str
    objective: str
    approach: str
    implementation_order: str
    verification: str
    changes_from_review: str = ""


@dataclass(frozen=True)
class PostMergeLearningSource:
    """Validated immutable merged-PR source fields."""

    repository: str
    issue: int
    pr: int
    title: str
    body: str
    merged_at: str
    merge_commit_sha: str
    url: str


@dataclass(frozen=True)
class PreparedLearningChange:
    """One bounded Mnemosyne artifact produced from a typed source."""

    relative_path: PurePosixPath
    content: str
    title: str


@dataclass(frozen=True)
class PreparedLearningWorkspace:
    """Isolated checkout prepared for one deterministic delivery branch."""

    path: Path
    existing_pr_number: int | None


class LearningSourceReader(Protocol):
    """Read and validate the immutable GitHub source for an intent."""

    def read(self, intent: LearningIntent) -> LearningSource:
        """Return a source that exactly matches ``intent``."""


class LearningGitHubFacts(Protocol):
    """Closed read-only GitHub facts required by source validation."""

    def issue(self, repository: str, issue: int) -> dict[str, object]:
        """Read one issue's identity, state, and labels."""

    def comments(self, repository: str, issue: int) -> list[IssueComment]:
        """Read all issue comments with actor ownership metadata."""

    def pull_request(self, repository: str, pr: int) -> dict[str, object]:
        """Read one pull request's immutable merge facts."""


class LearningWorkspace(Protocol):
    """Prepare a Mnemosyne worktree from a binding receipt."""

    def prepare(
        self,
        binding: MnemosyneBindingReceipt,
        branch: str,
    ) -> PreparedLearningWorkspace:
        """Return an isolated worktree bound to a new or existing branch."""


class LearningValidator(Protocol):
    """Validate a prepared Mnemosyne checkout."""

    def validate(self, path: Path) -> tuple[str, ...]:
        """Return bounded validation evidence or raise."""


def _normalized_text(value: str, *, field: str) -> str:
    """Normalize untrusted text while preserving its literal meaning."""
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text).strip()
    if not text:
        raise LearnDeliveryError(f"learning source lacks non-empty {field}")
    if len(text) > MAX_SOURCE_FIELD_CHARS:
        raise LearnDeliveryError(f"learning source {field} exceeds {MAX_SOURCE_FIELD_CHARS} chars")
    return text


def _indented(value: str) -> str:
    return "\n".join(f"    {line}" if line else "    " for line in value.splitlines())


def _yaml_string(value: str) -> str:
    """Return a JSON-quoted scalar, which is also a safe YAML scalar."""
    return json.dumps(value, ensure_ascii=False)


def _source_date(value: str) -> str:
    """Return the immutable source's calendar date or reject it."""
    date = value[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) is None:
        raise LearnDeliveryError("learning source lacks a valid immutable date")
    return date


def _plan_sections(plan: str) -> dict[str, str]:
    """Parse exact level-two sections from a canonical implementation plan."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in plan.splitlines():
        match = re.fullmatch(r"## ([^#].*?)\s*", line)
        if match:
            current = match.group(1).strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    parsed = {name: "\n".join(lines).strip() for name, lines in sections.items()}
    missing = [name for name in _REQUIRED_PLAN_SECTIONS if not parsed.get(name)]
    if missing:
        raise LearnDeliveryError("approved plan lacks required sections: " + ", ".join(missing))
    return parsed


class GitHubLearningSourceAdapter:
    """Repo-scoped, read-only GitHub facts used by preparation."""

    def __init__(self, gh: Callable[..., Any] = gh_call) -> None:
        """Initialize the adapter with the shared GitHub command boundary."""
        self._gh = gh
        self._viewer_login: str | None = None

    @staticmethod
    def _split_repository(repository: str) -> tuple[str, str]:
        parts = repository.split("/")
        if len(parts) != 2 or any(not part for part in parts):
            raise LearnDeliveryError("learning intent repository must be owner/name")
        return parts[0], parts[1]

    def issue(self, repository: str, issue: int) -> dict[str, object]:
        """Read repo-scoped issue labels and identity."""
        result = self._gh(
            [
                "issue",
                "view",
                str(issue),
                "--repo",
                repository,
                "--json",
                "number,state,labels",
            ],
            check=False,
            track_process_group=True,
        )
        return self._json_object(result, "issue source")

    def comments(self, repository: str, issue: int) -> list[IssueComment]:
        """Read every issue comment in chronological order with actor ownership."""
        owner, name = self._split_repository(repository)
        raw = github_api._fetch_issue_comments_paginated(
            issue,
            owner=owner,
            name=name,
            call=lambda argv: self._gh(argv, check=False, track_process_group=True),
        )
        login = self._current_login()
        return [
            IssueComment(
                body=str(comment.get("body", "")),
                author_login=str((comment.get("user") or {}).get("login", "")),
                author_association=str(comment.get("author_association", "")),
                created_at=str(comment.get("created_at", "")),
                updated_at=str(comment.get("updated_at", "")),
                viewer_did_author=str((comment.get("user") or {}).get("login", "")).lower()
                == login.lower(),
                database_id=(
                    int(comment["databaseId"]) if comment.get("databaseId") is not None else None
                ),
                url=str(comment.get("html_url", "")),
            )
            for comment in raw
        ]

    def pull_request(self, repository: str, pr: int) -> dict[str, object]:
        """Read immutable merged-PR proof and closing-issue references."""
        result = self._gh(
            [
                "pr",
                "view",
                str(pr),
                "--repo",
                repository,
                "--json",
                "number,state,title,body,url,mergedAt,mergeCommit,closingIssuesReferences",
            ],
            check=False,
            track_process_group=True,
        )
        return self._json_object(result, "merged PR source")

    def _current_login(self) -> str:
        if self._viewer_login is None:
            result = self._gh(
                ["api", "user", "--jq", ".login"],
                check=False,
                track_process_group=True,
            )
            if result.returncode != 0 or not (result.stdout or "").strip():
                raise LearnDeliveryError("cannot verify canonical plan actor ownership")
            self._viewer_login = (result.stdout or "").strip()
        return self._viewer_login

    @staticmethod
    def _json_object(result: Any, label: str) -> dict[str, object]:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise LearnDeliveryError(f"{label} read failed: {detail or result.returncode}")
        try:
            value = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise LearnDeliveryError(f"{label} returned malformed JSON") from exc
        if not isinstance(value, dict):
            raise LearnDeliveryError(f"{label} returned non-object JSON")
        return value


class GitHubLearningSourceReader:
    """Validate live GitHub facts against a semantic learning intent."""

    def __init__(self, adapter: LearningGitHubFacts | None = None) -> None:
        """Use the concrete repo-scoped adapter unless supplied a test seam."""
        self._adapter = adapter or GitHubLearningSourceAdapter()

    def read(self, intent: LearningIntent) -> LearningSource:
        """Read the source selected by ``intent.kind`` and verify exact identity."""
        if intent.kind is LearningIntentKind.APPROVED_PLAN:
            return self._approved_plan(intent)
        return self._post_merge(intent)

    def _approved_plan(self, intent: LearningIntent) -> ApprovedPlanLearningSource:
        issue = self._adapter.issue(intent.repo, intent.issue)
        if issue.get("number") != intent.issue or issue.get("state") != "OPEN":
            raise LearnDeliveryError("approved plan issue identity or state changed")
        raw_labels = issue.get("labels")
        if not isinstance(raw_labels, list):
            raise LearnDeliveryError("approved plan issue lacks labels")
        labels = [str(label.get("name", "")) for label in raw_labels if isinstance(label, dict)]
        if not is_exclusive_plan_state(labels, STATE_PLAN_GO):
            raise LearnDeliveryError("approved plan no longer has exclusive state:plan-go")
        comments = self._adapter.comments(intent.repo, intent.issue)
        snapshot = journal_snapshot(comments)
        owned_plans = [
            comment
            for comment in comments
            if comment.viewer_did_author and is_plan_comment(comment.body)
        ]
        if (
            len(owned_plans) != 1
            or owned_plans[0].database_id is None
            or owned_plans[0].database_id <= 0
            or not snapshot.current_plan
        ):
            raise LearnDeliveryError("approved plan canonical comment is absent or ambiguous")
        if (
            snapshot.revision != intent.plan_revision
            or plan_fingerprint(snapshot.current_plan) != intent.plan_fingerprint
        ):
            raise LearnDeliveryError("approved plan revision or fingerprint changed")
        sections = _plan_sections(snapshot.current_plan)
        return ApprovedPlanLearningSource(
            repository=intent.repo,
            issue=intent.issue,
            revision=snapshot.revision,
            fingerprint=plan_fingerprint(snapshot.current_plan),
            comment_database_id=owned_plans[0].database_id,
            source_date=_source_date(owned_plans[0].created_at),
            objective=_normalized_text(sections["Objective"], field="Objective"),
            approach=_normalized_text(sections["Approach"], field="Approach"),
            implementation_order=_normalized_text(
                sections["Implementation Order"], field="Implementation Order"
            ),
            verification=_normalized_text(sections["Verification"], field="Verification"),
            changes_from_review=(
                _normalized_text(sections["Changes from Review"], field="Changes from Review")
                if sections.get("Changes from Review")
                else ""
            ),
        )

    def _post_merge(self, intent: LearningIntent) -> PostMergeLearningSource:
        if intent.pr is None:
            raise LearnDeliveryError("post-merge intent lacks PR identity")
        data = self._adapter.pull_request(intent.repo, intent.pr)
        merge_commit = data.get("mergeCommit")
        merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        closing = data.get("closingIssuesReferences")
        closing_list = closing if isinstance(closing, list) else []
        closing_numbers = {ref.get("number") for ref in closing_list if isinstance(ref, dict)}
        if (
            data.get("number") != intent.pr
            or data.get("state") != "MERGED"
            or not isinstance(data.get("mergedAt"), str)
            or not data.get("mergedAt")
            or not isinstance(merge_sha, str)
            or _SHA_RE.fullmatch(merge_sha) is None
            or intent.issue not in closing_numbers
        ):
            raise LearnDeliveryError("post-merge source lacks exact merged closing proof")
        return PostMergeLearningSource(
            repository=intent.repo,
            issue=intent.issue,
            pr=intent.pr,
            title=_normalized_text(str(data.get("title", "")), field="PR title"),
            body=_normalized_text(str(data.get("body", "")), field="PR body"),
            merged_at=str(data["mergedAt"]),
            merge_commit_sha=merge_sha,
            url=_normalized_text(str(data.get("url", "")), field="PR URL"),
        )


class MnemosyneLearningBuilder:
    """Render one deterministic, structurally safe Mnemosyne skill artifact."""

    def build(
        self,
        intent: LearningIntent,
        source: LearningSource,
    ) -> PreparedLearningChange:
        """Build one flat ``skills/*.md`` change for a validated source."""
        digest = intent.key.rsplit(":", 1)[-1]
        base_slug = slugify(f"{intent.kind.value}-{intent.repo.rsplit('/', 1)[-1]}-{intent.issue}")
        name = f"{base_slug}-{digest[:12]}"
        path = PurePosixPath("skills", f"{name}.md")
        if isinstance(source, ApprovedPlanLearningSource):
            title = f"Approved implementation plan learning for #{source.issue}"
            workflow = (
                "### Objective\n\n"
                f"{_indented(source.objective)}\n\n"
                "### Approach\n\n"
                f"{_indented(source.approach)}\n\n"
                "### Implementation Order\n\n"
                f"{_indented(source.implementation_order)}\n\n"
                "### Verification\n\n"
                f"{_indented(source.verification)}"
            )
            if source.changes_from_review:
                workflow += (
                    f"\n\n### Changes from Review\n\n{_indented(source.changes_from_review)}"
                )
            provenance = (
                f"- Source repository: `{source.repository}`\n"
                f"- Issue: `#{source.issue}`\n"
                f"- Canonical plan revision: `{source.revision}`\n"
                f"- Canonical comment database ID: `{source.comment_database_id}`\n"
                f"- Plan fingerprint: `{source.fingerprint}`"
            )
            date = _source_date(source.source_date)
            description = (
                f"Use when implementing the approved plan from {source.repository}#{source.issue}."
            )
        elif isinstance(source, PostMergeLearningSource):
            title = f"Merged implementation learning for PR #{source.pr}"
            workflow = (
                "### Merged change\n\n"
                f"{_indented(source.title)}\n\n"
                "### Pull request context\n\n"
                f"{_indented(source.body)}"
            )
            provenance = (
                f"- Source repository: `{source.repository}`\n"
                f"- Closing issue: `#{source.issue}`\n"
                f"- Pull request: `{source.url}`\n"
                f"- Merged at: `{source.merged_at}`\n"
                f"- Merge commit: `{source.merge_commit_sha}`"
            )
            date = _source_date(source.merged_at)
            description = (
                f"Use when applying lessons from merged PR {source.repository}#{source.pr}."
            )
        else:
            raise LearnDeliveryError("unsupported learning source type")
        content = (
            "---\n"
            f"name: {_yaml_string(name)}\n"
            f"description: {_yaml_string(description)}\n"
            'category: "tooling"\n'
            f"date: {_yaml_string(date)}\n"
            'version: "1.0.0"\n'
            "user-invocable: false\n"
            'verification: "production-host"\n'
            "tags: [automation, learning, mnemosyne]\n"
            "---\n\n"
            f"# {title}\n\n"
            "## Overview\n\n"
            f"{description}\n\n"
            "## When to Use\n\n"
            "Use this learning when the same repository workflow, constraint, "
            "or failure mode recurs.\n\n"
            "## Verified Workflow\n\n"
            f"{workflow}\n\n"
            "## Failed Attempts\n\n"
            "No failed attempt is asserted beyond the bounded source material above.\n\n"
            "## Results & Parameters\n\n"
            f"{provenance}\n\n"
            "## Verified On\n\n"
            "Prepared by the provider-neutral Mnemosyne host boundary.\n"
        )
        return PreparedLearningChange(relative_path=path, content=content, title=title)


GitRunner = Callable[[Path, tuple[str, ...], int], subprocess.CompletedProcess[str]]


def _run_git(cwd: Path, argv: tuple[str, ...], timeout_s: int) -> subprocess.CompletedProcess[str]:
    return run_subprocess(
        ["git", *argv],
        cwd=cwd,
        timeout=timeout_s,
        check=False,
        track_process_group=True,
    )


def _git_success(result: subprocess.CompletedProcess[str], action: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise LearnDeliveryError(f"{action} failed: {detail or result.returncode}")
    return (result.stdout or "").strip()


class BoundLearningWorkspace:
    """Create deterministic isolated worktrees from the Mnemosyne binding."""

    def __init__(
        self,
        *,
        git: GitRunner = _run_git,
        gh: Callable[..., Any] = gh_call,
        timeout_s: int = NETWORK_TIMEOUT,
    ) -> None:
        """Initialize closed Git and GitHub seams."""
        self._git = git
        self._gh = gh
        self._timeout_s = timeout_s

    def prepare(
        self,
        binding: MnemosyneBindingReceipt,
        branch: str,
    ) -> PreparedLearningWorkspace:
        """Create a clean worktree from the binding or a live retry PR head."""
        root = Path(binding.root).resolve()
        if not root.is_dir() or root.is_symlink():
            raise LearnDeliveryError("Mnemosyne binding root is not a safe directory")
        digest = sha256(branch.encode("utf-8")).hexdigest()[:16]
        parent = root / "build" / "mnemosyne-learning"
        path = parent / digest
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise LearnDeliveryError("Mnemosyne learning worktree must not be a symlink")
        if path.exists():
            _git_success(
                self._git(root, ("worktree", "remove", "--force", str(path)), self._timeout_s),
                "stale learning worktree removal",
            )
        existing_pr, start = self._existing_pr(binding, branch)
        if existing_pr is not None:
            _git_success(
                self._git(root, ("fetch", "origin", branch), self._timeout_s),
                "learning retry branch fetch",
            )
            remote_head = _git_success(
                self._git(
                    root,
                    ("rev-parse", f"refs/remotes/origin/{branch}"),
                    self._timeout_s,
                ),
                "learning retry branch read",
            )
            if remote_head != start:
                raise LearnDeliveryError("learning retry branch moved before worktree creation")
        _git_success(
            self._git(root, ("worktree", "add", "--detach", str(path), start), self._timeout_s),
            "learning worktree creation",
        )
        _git_success(
            self._git(path, ("switch", "-C", branch), self._timeout_s),
            "learning branch binding",
        )
        return PreparedLearningWorkspace(path=path, existing_pr_number=existing_pr)

    def _existing_pr(
        self,
        binding: MnemosyneBindingReceipt,
        branch: str,
    ) -> tuple[int | None, str]:
        result = self._gh(
            [
                "pr",
                "list",
                "--repo",
                binding.repository,
                "--head",
                branch,
                "--base",
                binding.default_branch,
                "--state",
                "open",
                "--limit",
                "2",
                "--json",
                "number,headRefOid",
            ],
            check=False,
            track_process_group=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise LearnDeliveryError(
                f"learning retry PR discovery failed: {detail or result.returncode}"
            )
        try:
            values = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise LearnDeliveryError("learning retry PR discovery returned malformed JSON") from exc
        if not isinstance(values, list) or len(values) > 1:
            raise LearnDeliveryError("learning retry PR discovery is ambiguous")
        if not values:
            return None, binding.commit_sha
        value = values[0]
        number = value.get("number") if isinstance(value, dict) else None
        head = value.get("headRefOid") if isinstance(value, dict) else None
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
            or not isinstance(head, str)
            or _SHA_RE.fullmatch(head) is None
        ):
            raise LearnDeliveryError("learning retry PR lacks a valid bound head")
        return number, head


class MnemosynePluginValidator:
    """Run Mnemosyne's fixed validator under a restricted environment."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = run_subprocess,
    ) -> None:
        """Initialize the subprocess seam."""
        self._runner = runner

    def validate(self, path: Path) -> tuple[str, ...]:
        """Run the fixed no-network validator command without a shell."""
        cache = path / "build" / "uv-cache"
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "UV_CACHE_DIR": str(cache),
        }
        result = self._runner(
            list(VALIDATOR_ARGV),
            cwd=path,
            timeout=VALIDATOR_TIMEOUT_S,
            check=False,
            log_on_error=False,
            env=env,
            track_process_group=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            detail = _CONTROL_RE.sub("", detail)[-1000:]
            raise LearnDeliveryError(
                f"Mnemosyne plugin validation failed: {detail or result.returncode}"
            )
        return (" ".join(VALIDATOR_ARGV),)


class MnemosyneLearningPreparationService:
    """Create one complete delivery request from a semantic learning intent."""

    def __init__(
        self,
        *,
        source_reader: LearningSourceReader | None = None,
        builder: MnemosyneLearningBuilder | None = None,
        workspace: LearningWorkspace | None = None,
        validator: LearningValidator | None = None,
    ) -> None:
        """Initialize provider-neutral preparation seams."""
        self._source_reader = source_reader or GitHubLearningSourceReader()
        self._builder = builder or MnemosyneLearningBuilder()
        self._workspace = workspace or BoundLearningWorkspace()
        self._validator = validator or MnemosynePluginValidator()

    def prepare(
        self,
        payload: Mapping[str, object],
        binding: MnemosyneBindingReceipt,
    ) -> LearnDeliveryRequest:
        """Prepare, validate, and return a binding-complete delivery request."""
        intent = LearningIntent.from_payload(dict(payload))
        source = self._source_reader.read(intent)
        change = self._builder.build(intent, source)
        content_bytes = change.content.encode("utf-8")
        if len(content_bytes) > MAX_ARTIFACT_BYTES:
            raise ValueError(f"learning artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
        if (
            change.relative_path.is_absolute()
            or len(change.relative_path.parts) != 2
            or change.relative_path.parts[0] != "skills"
            or any(part in {"", ".", ".."} for part in change.relative_path.parts)
        ):
            raise LearnDeliveryError("learning artifact path is outside flat skills allowlist")
        digest = intent.key.rsplit(":", 1)[-1]
        branch = f"learn/{digest}"
        prepared = self._workspace.prepare(binding, branch)
        if prepared.path.is_symlink():
            raise LearnDeliveryError("prepared learning worktree must not be a symlink")
        root = prepared.path.resolve()
        expected_parent = (Path(binding.root).resolve() / "build" / "mnemosyne-learning").resolve()
        if root.parent != expected_parent:
            raise LearnDeliveryError("prepared learning worktree is outside bound build directory")
        target = root.joinpath(*change.relative_path.parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.is_symlink() or target.parent.is_symlink():
            raise LearnDeliveryError("learning artifact path must not contain symlinks")
        if target.resolve().parent != (root / "skills").resolve():
            raise LearnDeliveryError("learning artifact escaped the prepared worktree")
        write_secure(target, change.content)
        evidence = self._validator.validate(root)
        relative = change.relative_path.as_posix()
        return LearnDeliveryRequest(
            repository=binding.repository,
            worktree_path=root,
            branch=branch,
            base_branch=binding.default_branch,
            allowed_paths=(relative,),
            commit_message=f"docs(skills): capture learning for {intent.repo}#{intent.issue}",
            pr_title=f"docs(skills): {change.title}",
            pr_body=(
                f"Prepared by the host learning boundary from `{intent.repo}#{intent.issue}`.\n"
            ),
            disposition="reuse" if prepared.existing_pr_number is not None else "create",
            validation_evidence=evidence,
            existing_pr_number=prepared.existing_pr_number,
        )
