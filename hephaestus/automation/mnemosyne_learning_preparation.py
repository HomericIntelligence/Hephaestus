"""Prepare bounded Mnemosyne learning changes at the host boundary.

This module converts a semantic :class:`LearningIntent` into the existing
PR-delivery request.  It deliberately has no dependency on agent providers or
generic ``AgentJob`` dispatch.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import yaml

from hephaestus.automation.github_api import gh_call
from hephaestus.automation.mnemosyne_binding import MnemosyneBindingReceipt
from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError, LearnDeliveryRequest
from hephaestus.automation.mnemosyne_node_runtime import node_runtime_files
from hephaestus.automation.mnemosyne_validator_dependencies import (
    prepare_dependencies,
    run_learning_subprocess,
)
from hephaestus.automation.pipeline.work_item import LearningIntent, LearningIntentKind
from hephaestus.automation.remote_git import TrustedRemoteGit
from hephaestus.config.child_environments import build_git_child_env
from hephaestus.io.utils import write_secure
from hephaestus.utils.helpers import NETWORK_TIMEOUT, run_subprocess

MAX_ARTIFACT_BYTES = 65_536
MAX_SOURCE_FIELD_CHARS = 16_384
VALIDATOR_SCRIPT = "scripts/validate_plugins.py"
VALIDATOR_TIMEOUT_S = 120
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SHA_RE = re.compile(r"[0-9a-f]{40}")


class LearningSource(Protocol):
    """Marker protocol for typed immutable learning sources."""


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
    verified_head: str = ""
    verification_evidence: tuple[str, ...] = ()


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

    def pull_request(self, repository: str, pr: int) -> dict[str, object]:
        """Read one pull request's immutable merge facts."""

    def verification(self, repository: str, pr: int, head: str) -> tuple[str, ...]:
        """Read passing required checks bound to the merged source head."""


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


class GitHubLearningSourceAdapter:
    """Repo-scoped, read-only GitHub facts used by preparation."""

    def __init__(self, gh: Callable[..., Any] = gh_call) -> None:
        """Initialize the adapter with the shared GitHub command boundary."""
        self._gh = gh

    @staticmethod
    def _split_repository(repository: str) -> tuple[str, str]:
        parts = repository.split("/")
        if len(parts) != 2 or any(not part for part in parts):
            raise LearnDeliveryError("learning intent repository must be owner/name")
        return parts[0], parts[1]

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
                "number,state,title,body,url,mergedAt,mergeCommit,closingIssuesReferences,headRefOid",
            ],
            check=False,
            track_process_group=True,
        )
        return self._json_object(result, "merged PR source")

    def verification(self, repository: str, pr: int, head: str) -> tuple[str, ...]:
        """Read required checks and confirm their source head after the read."""
        result = self._gh(
            [
                "pr",
                "checks",
                str(pr),
                "--repo",
                repository,
                "--required",
                "--json",
                "name,bucket,link",
            ],
            check=False,
            track_process_group=True,
        )
        try:
            checks = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            raise LearnDeliveryError("post-merge required checks returned invalid JSON") from None
        if (
            result.returncode != 0
            or not isinstance(checks, list)
            or not checks
            or any(
                not isinstance(check, dict)
                or check.get("bucket") != "pass"
                or not check.get("name")
                for check in checks
            )
        ):
            raise LearnDeliveryError(
                "learning_deferred:post-merge source lacks passing required checks"
            )
        current = self.pull_request(repository, pr)
        if current.get("state") != "MERGED" or current.get("headRefOid") != head:
            raise LearnDeliveryError("post-merge checked head changed")
        return tuple(f"{check['name']}: {check.get('link', '')}" for check in checks)

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
            raise LearnDeliveryError("plan_only_learning_rejected")
        return self._post_merge(intent)

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
            raise LearnDeliveryError(
                "learning_deferred:post-merge source lacks exact merged closing proof"
            )
        head = data.get("headRefOid")
        if not isinstance(head, str) or _SHA_RE.fullmatch(head) is None:
            raise LearnDeliveryError(
                "learning_deferred:post-merge source lacks passing required checks"
            )
        evidence = self._adapter.verification(intent.repo, intent.pr, head)
        if not evidence:
            raise LearnDeliveryError(
                "learning_deferred:post-merge source lacks passing required checks"
            )
        return PostMergeLearningSource(
            repository=intent.repo,
            issue=intent.issue,
            pr=intent.pr,
            title=_normalized_text(str(data.get("title", "")), field="PR title"),
            body=_normalized_text(str(data.get("body", "")), field="PR body"),
            merged_at=str(data["mergedAt"]),
            merge_commit_sha=merge_sha,
            url=_normalized_text(str(data.get("url", "")), field="PR URL"),
            verified_head=head,
            verification_evidence=evidence,
        )


class MnemosyneLearningBuilder:
    """Require a reviewed candidate before a learning artifact is built."""

    def build(
        self,
        intent: LearningIntent,
        source: LearningSource,
    ) -> PreparedLearningChange:
        """Defer until an application supplies a reviewed candidate builder."""
        if intent.kind is LearningIntentKind.APPROVED_PLAN:
            raise LearnDeliveryError("plan_only_learning_rejected")
        raise LearnDeliveryError("learning_deferred:candidate_required")


GitRunner = Callable[[Path, tuple[str, ...], int], subprocess.CompletedProcess[str]]


def _run_git(cwd: Path, argv: tuple[str, ...], timeout_s: int) -> subprocess.CompletedProcess[str]:
    return run_subprocess(
        ["git", *argv],
        cwd=cwd,
        timeout=timeout_s,
        check=False,
        track_process_group=True,
        env=build_git_child_env(),
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
        remote_git: GitRunner | None = None,
        gh_extra_path_root: Path | None = None,
        gh: Callable[..., Any] = gh_call,
        timeout_s: int = NETWORK_TIMEOUT,
    ) -> None:
        """Initialize closed Git and GitHub seams."""
        self._git = git
        self._remote_git = remote_git or (
            git if git is not _run_git else TrustedRemoteGit(gh_extra_path_root)
        )
        self._gh = gh
        self._timeout_s = timeout_s

    def prepare(
        self,
        binding: MnemosyneBindingReceipt,
        branch: str,
    ) -> PreparedLearningWorkspace:
        """Create a clean worktree from the binding or a live retry PR head."""
        bound_root = Path(binding.root)
        if not bound_root.is_dir() or bound_root.is_symlink():
            raise LearnDeliveryError("Mnemosyne binding root is not a safe directory")
        root = bound_root.resolve()
        digest = sha256(branch.encode("utf-8")).hexdigest()[:16]
        parent = root / "build" / "mnemosyne-learning"
        path = parent / digest
        self._create_safe_parent(root, parent)
        try:
            path.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise LearnDeliveryError(
                "Mnemosyne learning worktree escaped the original binding root"
            ) from exc
        if path.is_symlink():
            raise LearnDeliveryError("Mnemosyne learning worktree must not be a symlink")
        existing_pr, start = self._existing_pr(binding, branch)
        if path.exists():
            if existing_pr is None and self._remote_branch_is_published(root, branch):
                raise LearnDeliveryError(
                    "learning worktree preserved because publication outcome is ambiguous"
                )
            if existing_pr is None:
                raise LearnDeliveryError("learning candidate requires recovery")
            _git_success(
                self._git(root, ("worktree", "remove", "--force", str(path)), self._timeout_s),
                "stale learning worktree removal",
            )
        if existing_pr is not None:
            _git_success(
                self._remote_git(root, ("fetch", "origin", branch), self._timeout_s),
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

    def _create_safe_parent(self, root: Path, parent: Path) -> None:
        """Create workspace ancestors only when none can redirect outside ``root``."""
        current = root
        for part in ("build", "mnemosyne-learning"):
            current = current / part
            if current.is_symlink():
                raise LearnDeliveryError("Mnemosyne learning has a symlinked workspace ancestor")
            current.mkdir(exist_ok=True, mode=0o700)
            if current.is_symlink():
                raise LearnDeliveryError("Mnemosyne learning has a symlinked workspace ancestor")
        try:
            parent.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise LearnDeliveryError(
                "Mnemosyne learning workspace escaped the original binding root"
            ) from exc

    def _remote_branch_is_published(self, root: Path, branch: str) -> bool:
        """Return whether a remote branch proves a prior publish attempt occurred.

        A transport failure is ambiguous too: do not delete the only recovery
        evidence merely because the classification query itself could not run.
        """
        result = self._remote_git(
            root,
            ("ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"),
            self._timeout_s,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 2:
            return False
        raise LearnDeliveryError(
            "learning worktree preserved because publication outcome is ambiguous"
        )

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
        runner: Callable[..., subprocess.CompletedProcess[str]] = run_learning_subprocess,
    ) -> None:
        """Initialize the subprocess seam."""
        self._runner = runner

    def validate(self, path: Path) -> tuple[str, ...]:
        """Run the fixed validator with prepared dependencies and no network."""
        path = path.resolve()
        with prepare_dependencies(path, self._runner) as prepared:
            sandbox = Path("/usr/bin/sandbox-exec")
            if platform.system() != "Darwin" or not sandbox.is_file():
                raise LearnDeliveryError("learning validation boundary is unavailable")
            with tempfile.TemporaryDirectory(prefix="hephaestus-learning-check-") as temporary:
                scratch = Path(temporary).resolve()
                env = {
                    "PATH": str(prepared.environment / "bin") + ":/usr/bin:/bin",
                    "HOME": str(scratch),
                    "TMPDIR": str(scratch),
                    "UV_CACHE_DIR": str(scratch / "cache"),
                    "UV_PROJECT_ENVIRONMENT": str(prepared.environment),
                    "UV_PYTHON_DOWNLOADS": "never",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                read_roots = (
                    path,
                    prepared.root,
                    prepared.runtime,
                    prepared.uv.parent,
                    Path("/usr"),
                    Path("/System"),
                    Path("/Library/Apple"),
                    Path("/dev"),
                )
                reads = " ".join(
                    f"(subpath {json.dumps(str(root.resolve()))})" for root in read_roots
                )
                profile = (
                    '(version 1)(deny default)(import "system.sb")(allow process*)'
                    "(allow signal (target same-sandbox))(allow file-read-metadata)"
                    f"(allow file-read* {reads} (subpath {json.dumps(str(scratch))}))"
                    f"(allow file-write* (subpath {json.dumps(str(scratch))}))"
                    '(allow file-write* (literal "/dev/null"))(deny network*)'
                )
                prepared.verify(path)
                try:
                    probe = self._runner(
                        [str(sandbox), "-p", profile, "/usr/bin/true"],
                        cwd=path,
                        timeout=10,
                        check=False,
                        log_on_error=False,
                        env=env,
                        track_process_group=True,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    raise LearnDeliveryError("learning validation boundary failed") from None
                if probe.returncode != 0:
                    raise LearnDeliveryError("learning validation boundary failed")
                argv = [str(prepared.environment / "bin/python"), VALIDATOR_SCRIPT]
                try:
                    result = self._runner(
                        [str(sandbox), "-p", profile, *argv],
                        cwd=path,
                        timeout=VALIDATOR_TIMEOUT_S,
                        check=False,
                        log_on_error=False,
                        env=env,
                        track_process_group=True,
                    )
                except subprocess.TimeoutExpired:
                    raise LearnDeliveryError("learning plugin validation timed out") from None
                except OSError:
                    raise LearnDeliveryError(
                        "learning validation boundary could not start"
                    ) from None
                if result.returncode != 0:
                    raise LearnDeliveryError("learning plugin validation failed")
                prepared.verify(path)
                node_value = shutil.which("node")
                cli_value = shutil.which("markdownlint-cli2")
                if not node_value or not cli_value:
                    raise LearnDeliveryError("learning markdownlint is unavailable")
                node, cli = Path(node_value).resolve(), Path(cli_value).resolve()
                runtime_files = node_runtime_files(node)
                executables = tuple(
                    (target, sha256(target.read_bytes()).hexdigest())
                    for target in (*runtime_files, cli)
                )
                lint_reads = (
                    " ".join(f"(literal {json.dumps(str(target))})" for target in runtime_files)
                    + f" (subpath {json.dumps(str(cli.parent))})"
                )
                lint_profile = profile + f"(allow file-read* {lint_reads})"
                # Offline lint does not use the host TLS configuration.
                env["OPENSSL_CONF"] = "/dev/null"
                lint_argv = [
                    str(node),
                    str(cli),
                    "--config",
                    str(path / ".markdownlint.yaml"),
                    "skills/*.md",
                ]
                try:
                    lint = self._runner(
                        [str(sandbox), "-p", lint_profile, *lint_argv],
                        cwd=path,
                        timeout=VALIDATOR_TIMEOUT_S,
                        check=False,
                        log_on_error=False,
                        env=env,
                        track_process_group=True,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    raise LearnDeliveryError("learning markdownlint runner failed") from None
                if lint.returncode != 0:
                    raise LearnDeliveryError("learning markdownlint failed")
                if any(
                    sha256(target.read_bytes()).hexdigest() != digest
                    for target, digest in executables
                ):
                    raise LearnDeliveryError("learning markdownlint executable changed")
                prepared.verify(path)
        return (" ".join(argv), " ".join(lint_argv))


class MnemosyneLearningPreparationService:
    """Create one complete delivery request from a semantic learning intent."""

    def __init__(
        self,
        *,
        gh_extra_path_root: Path | None = None,
        source_reader: LearningSourceReader | None = None,
        builder: MnemosyneLearningBuilder | None = None,
        workspace: LearningWorkspace | None = None,
        validator: LearningValidator | None = None,
    ) -> None:
        """Initialize provider-neutral preparation seams."""
        self._source_reader = source_reader or GitHubLearningSourceReader()
        self._builder = builder or MnemosyneLearningBuilder()
        self._workspace = workspace or BoundLearningWorkspace(gh_extra_path_root=gh_extra_path_root)
        self._validator = validator or MnemosynePluginValidator()

    @staticmethod
    def _check_corpus_candidate(
        intent: LearningIntent,
        source: LearningSource,
        change: PreparedLearningChange,
        binding: MnemosyneBindingReceipt,
    ) -> None:
        """Require review when a supplied candidate could duplicate an entry."""
        from hephaestus.automation.athena_contract import load_athena_contract_receipt
        from hephaestus.automation.mnemosyne_corpus_reader import DefaultCorpusReader
        from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillRequest

        if not isinstance(source, PostMergeLearningSource) or not source.verification_evidence:
            raise LearnDeliveryError("learning_deferred:implementation_evidence_required")
        request = AthenaSkillRequest(
            kind="advise",
            repo=intent.repo,
            issue=intent.issue,
            agent="",
            model="",
            cwd=Path(binding.root),
            timeout_s=NETWORK_TIMEOUT,
            payload={"issue_title": source.title, "issue_body": source.body},
        )
        corpus = DefaultCorpusReader().read(request, binding, load_athena_contract_receipt())
        matches = {block.source for block in corpus.blocks}
        if matches and (len(matches) != 1 or change.relative_path.as_posix() not in matches):
            raise LearnDeliveryError("learning_deferred:corpus_match_requires_review")

    @staticmethod
    def _bind_candidate_evidence(
        source: LearningSource, change: PreparedLearningChange
    ) -> PreparedLearningChange:
        """Set candidate verification from checked implementation evidence."""
        if not isinstance(source, PostMergeLearningSource):
            raise LearnDeliveryError("learning_deferred:implementation_evidence_required")
        parts = change.content.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise LearnDeliveryError("learning candidate lacks YAML frontmatter")
        try:
            metadata = yaml.safe_load(parts[1])
        except yaml.YAMLError:
            raise LearnDeliveryError("learning candidate has invalid YAML frontmatter") from None
        if not isinstance(metadata, dict):
            raise LearnDeliveryError("learning candidate frontmatter must be a mapping")
        metadata["verification"] = "verified-ci"
        evidence = json.dumps(
            {
                "repository": source.repository,
                "pr": source.pr,
                "merge_commit": source.merge_commit_sha,
                "checked_head": source.verified_head,
                "required_checks": source.verification_evidence,
            },
            indent=2,
        )
        content = (
            "---\n"
            + yaml.safe_dump(metadata, sort_keys=False)
            + "---"
            + parts[2].rstrip()
            + "\n\n## Implementation evidence\n\n```json\n"
            + evidence
            + "\n```\n"
        )
        if len(content.encode("utf-8")) > MAX_ARTIFACT_BYTES:
            raise LearnDeliveryError("learning candidate with evidence exceeds size limit")
        return replace(change, content=content)

    def prepare(
        self,
        payload: Mapping[str, object],
        binding: MnemosyneBindingReceipt,
    ) -> LearnDeliveryRequest:
        """Prepare, validate, and return a binding-complete delivery request."""
        intent = LearningIntent.from_payload(dict(payload))
        if intent.kind is LearningIntentKind.APPROVED_PLAN:
            raise LearnDeliveryError("plan_only_learning_rejected")
        source = self._source_reader.read(intent)
        change = self._builder.build(intent, source)
        self._check_corpus_candidate(intent, source, change, binding)
        content_bytes = change.content.encode("utf-8")
        if len(content_bytes) > MAX_ARTIFACT_BYTES:
            raise ValueError(f"learning artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
        change = self._bind_candidate_evidence(source, change)
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
