"""Tests for host-owned Mnemosyne learning preparation."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from hephaestus.automation.mnemosyne_binding import MnemosyneBindingReceipt
from hephaestus.automation.mnemosyne_delivery import LearnDeliveryError
from hephaestus.automation.mnemosyne_learning_preparation import (
    BoundLearningWorkspace,
    GitHubLearningSourceReader,
    MnemosyneLearningBuilder,
    MnemosyneLearningPreparationService,
    MnemosynePluginValidator,
    PostMergeLearningSource,
    PreparedLearningChange,
    PreparedLearningWorkspace,
)
from hephaestus.automation.pipeline.work_item import LearningIntent
from hephaestus.automation.review_journal import (
    IssueComment,
)


def _binding(tmp_path: Path) -> MnemosyneBindingReceipt:
    return MnemosyneBindingReceipt(
        root=str(tmp_path / "mnemosyne"),
        repository="HomericIntelligence/Mnemosyne",
        default_branch="main",
        version="3.0.0",
        commit_sha="b" * 40,
        sync_status="updated",
        trust_basis="test",
        athena_contract={},
    )


def _intent() -> LearningIntent:
    return LearningIntent.post_merge(repo="org/repo", issue=2754, pr=2800)


def _source() -> PostMergeLearningSource:
    return PostMergeLearningSource(
        repository="org/repo",
        issue=2754,
        pr=2800,
        title="Fix workers",
        body="Closes #2754",
        merged_at="2026-08-14T12:00:00Z",
        merge_commit_sha="c" * 40,
        url="https://github.com/org/repo/pull/2800",
        verified_head="d" * 40,
        verification_evidence=("tests: https://example.test/check",),
    )


class CandidateBuilder(MnemosyneLearningBuilder):
    """Supply a host-authored candidate through the existing builder seam."""

    def build(self, intent: LearningIntent, source: object) -> PreparedLearningChange:
        return PreparedLearningChange(
            PurePosixPath("skills/worker-recovery.md"),
            '---\nverification: "production-host"\n---\n# Worker recovery\n',
            "Worker recovery",
        )


@pytest.mark.parametrize("validation_fails", [False, True])
def test_preparation_creates_complete_bound_delivery_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validation_fails: bool
) -> None:
    """Semantic intent becomes one validated, delivery-ready host request."""
    from types import SimpleNamespace

    from hephaestus.automation.mnemosyne_skill_host import DefaultCorpusReader

    monkeypatch.setattr(DefaultCorpusReader, "read", lambda *_: SimpleNamespace(blocks=()))
    binding = _binding(tmp_path)
    worktree = Path(binding.root) / "build" / "mnemosyne-learning" / "prepared"

    class Reader:
        def read(self, intent: LearningIntent) -> PostMergeLearningSource:
            assert intent == _intent()
            return _source()

    class Workspace:
        def prepare(
            self,
            binding: MnemosyneBindingReceipt,
            branch: str,
        ) -> PreparedLearningWorkspace:
            assert binding == _binding(tmp_path)
            assert branch.startswith("learn/")
            worktree.mkdir(parents=True)
            return PreparedLearningWorkspace(path=worktree, existing_pr_number=None)

    class Validator:
        def validate(self, path: Path) -> tuple[str, ...]:
            assert path == worktree
            if validation_fails:
                raise LearnDeliveryError("learning plugin validation failed")
            return ("/prepared/environment/bin/python scripts/validate_plugins.py",)

    service = MnemosyneLearningPreparationService(
        source_reader=Reader(),
        builder=CandidateBuilder(),
        workspace=Workspace(),
        validator=Validator(),
    )

    if validation_fails:
        with pytest.raises(LearnDeliveryError, match="learning plugin validation failed"):
            service.prepare(_intent().to_payload(), binding)
        assert (worktree / "skills/worker-recovery.md").is_file()
        return
    request = service.prepare(_intent().to_payload(), binding)

    assert request.repository == binding.repository
    assert request.base_branch == binding.default_branch
    assert request.worktree_path == worktree
    assert request.allowed_paths == (request.allowed_paths[0],)
    assert request.allowed_paths[0].startswith("skills/")
    assert (worktree / request.allowed_paths[0]).is_file()
    content = (worktree / request.allowed_paths[0]).read_text()
    assert "production-host" not in content
    assert "verification: verified-ci" in content
    assert _source().verified_head in content
    assert request.validation_evidence == (
        "/prepared/environment/bin/python scripts/validate_plugins.py",
    )


def test_workspace_rejects_symlinked_ancestor_before_creating_directories(tmp_path: Path) -> None:
    """The bound checkout's build ancestors cannot redirect a worktree outside it."""
    root = tmp_path / "mnemosyne"
    root.mkdir()
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (root / "build").symlink_to(escaped, target_is_directory=True)

    workspace = BoundLearningWorkspace()

    with pytest.raises(LearnDeliveryError, match="symlinked workspace ancestor"):
        workspace.prepare(_binding(tmp_path), "learn/" + "a" * 16)

    assert not (escaped / "mnemosyne-learning").exists()


def test_workspace_preserves_existing_path_when_remote_branch_makes_outcome_ambiguous(
    tmp_path: Path,
) -> None:
    """A deterministic worktree is never force-removed before publish recovery is known."""
    root = tmp_path / "mnemosyne"
    root.mkdir()
    branch = "learn/" + "a" * 16
    # The product derives this exact value from the branch; keep the fixture coupled to it.
    from hashlib import sha256

    digest = sha256(branch.encode("utf-8")).hexdigest()[:16]
    stale = root / "build" / "mnemosyne-learning" / digest
    stale.mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    def git(_cwd: Path, argv: tuple[str, ...], _timeout_s: int) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv == ("ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"):
            return subprocess.CompletedProcess(["git"], 0, stdout="a" * 40 + "\trefs/heads/x\n")
        return subprocess.CompletedProcess(["git"], 0, stdout="")

    def gh(_argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["gh"], 0, stdout="[]")

    with pytest.raises(LearnDeliveryError, match="publication outcome is ambiguous"):
        BoundLearningWorkspace(git=git, gh=gh).prepare(_binding(tmp_path), branch)

    assert stale.is_dir()
    assert not any(call[:2] == ("worktree", "remove") for call in calls)


def test_validator_redacts_and_bounds_secret_diagnostics(tmp_path: Path) -> None:
    """Validator output cannot persist credentials through host result errors."""
    secret = "ghp_" + "a" * 30
    api_key = "sk-" + "a" * 26

    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["uv"],
            1,
            stderr=(f"token={secret}\nAuthorization: Bearer {api_key}\n" + "x" * 2000),
        )

    with pytest.raises(LearnDeliveryError) as raised:
        MnemosynePluginValidator(runner=runner).validate(tmp_path)

    diagnostic = str(raised.value)
    assert secret not in diagnostic
    assert api_key not in diagnostic
    assert "dependency input" in diagnostic
    assert len(diagnostic) <= 1100


def test_post_merge_source_requires_merged_closing_pr() -> None:
    """Post-merge preparation binds the exact PR, merge SHA, and issue."""
    intent = LearningIntent.post_merge(
        repo="HomericIntelligence/ProjectHephaestus",
        issue=2754,
        pr=2800,
    )

    class Adapter:
        def issue(self, repository: str, issue: int) -> dict[str, object]:
            raise AssertionError(f"unexpected issue read for {repository}#{issue}")

        def comments(self, repository: str, issue: int) -> list[IssueComment]:
            raise AssertionError(f"unexpected comment read for {repository}#{issue}")

        def pull_request(self, repository: str, pr: int) -> dict[str, object]:
            assert repository == intent.repo and pr == intent.pr
            return {
                "number": pr,
                "state": "MERGED",
                "title": "Prepare learning",
                "body": "Closes #2754",
                "url": f"https://github.com/{repository}/pull/{pr}",
                "mergedAt": "2026-08-14T12:00:00Z",
                "mergeCommit": {"oid": "c" * 40},
                "closingIssuesReferences": [{"number": 2754}],
                "headRefOid": "d" * 40,
            }

        def verification(self, repository: str, pr: int, head: str) -> tuple[str, ...]:
            assert head == "d" * 40
            return ("tests: https://example.test/check",)

    source = GitHubLearningSourceReader(Adapter()).read(intent)

    assert isinstance(source, PostMergeLearningSource)
    assert source.merge_commit_sha == "c" * 40
    assert source.issue == 2754


def test_validator_rejects_unbound_dependency_input(tmp_path: Path) -> None:
    """An untracked lock cannot supply validator dependencies."""
    (tmp_path / "uv.lock").write_text("version = 1\n")
    calls: list[object] = []

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess([], 0, stdout="")

    with pytest.raises(LearnDeliveryError, match="dependency input"):
        MnemosynePluginValidator(runner=runner).validate(tmp_path)
    assert not calls


def test_unusable_validator_boundary_has_a_safe_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boundary probe failure is distinct from a plugin validation failure."""
    import platform
    from collections.abc import Iterator
    from contextlib import contextmanager
    from types import SimpleNamespace

    from hephaestus.automation import mnemosyne_learning_preparation as preparation

    @contextmanager
    def prepared(_path: Path, _runner: object) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(
            root=tmp_path,
            runtime=tmp_path,
            environment=tmp_path / "environment",
            uv=tmp_path / "uv",
            verify=lambda _path: None,
        )

    real_is_file = Path.is_file
    monkeypatch.setattr(preparation, "prepare_dependencies", prepared)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: str(path) == "/usr/bin/sandbox-exec" or real_is_file(path),
    )
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 71, stderr="sandbox-exec: secret /private/path")

    with pytest.raises(LearnDeliveryError) as error:
        MnemosynePluginValidator(runner=runner).validate(tmp_path)
    assert str(error.value) == "learning validation boundary failed"
    assert len(calls) == 1
    assert calls[0][-1] == "/usr/bin/true"


@pytest.mark.parametrize("failure", [None, "nonzero", "timeout", "launch", "artifact"])
def test_prepared_interpreter_keeps_validation_failures_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    """Only successful execution and artifact verification return evidence."""
    import platform
    import shutil
    from collections.abc import Iterator
    from contextlib import contextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock

    from hephaestus.automation import mnemosyne_learning_preparation as preparation

    node = tmp_path / "node"
    cli = tmp_path / "markdownlint-cli2"
    node.write_text("node")
    cli.write_text("cli")
    monkeypatch.setattr(shutil, "which", lambda name: str(node if name == "node" else cli))
    monkeypatch.setattr(preparation, "node_runtime_files", lambda path: (path,))
    verified = Mock()
    if failure == "artifact":
        verified.side_effect = [None, LearnDeliveryError("learning dependency artifact changed")]

    @contextmanager
    def prepared(_path: Path, _runner: object) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(
            root=tmp_path,
            runtime=tmp_path,
            environment=tmp_path / "environment",
            uv=tmp_path / "uv",
            verify=verified,
        )

    monkeypatch.setattr(preparation, "prepare_dependencies", prepared)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    real_is_file = Path.is_file
    monkeypatch.setattr(
        Path, "is_file", lambda path: str(path) == "/usr/bin/sandbox-exec" or real_is_file(path)
    )
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[-1] == "scripts/validate_plugins.py":
            assert argv[3:] == [
                str(tmp_path / "environment/bin/python"),
                "scripts/validate_plugins.py",
            ]
            if failure == "timeout":
                raise subprocess.TimeoutExpired("secret", 120)
            if failure == "launch":
                raise OSError("secret")
            if failure == "nonzero":
                return subprocess.CompletedProcess(argv, 2, stderr="secret")
        return subprocess.CompletedProcess(argv, 0)

    validator = MnemosynePluginValidator(runner=runner)
    if failure is None:
        assert validator.validate(tmp_path) == (
            f"{tmp_path / 'environment/bin/python'} scripts/validate_plugins.py",
            f"{node} {cli} --config {tmp_path / '.markdownlint.yaml'} skills/*.md",
        )
        assert verified.call_count == 3
    else:
        with pytest.raises(LearnDeliveryError) as error:
            validator.validate(tmp_path)
        assert "secret" not in str(error.value)
        assert verified.call_count == (2 if failure == "artifact" else 1)
    assert len(calls) == (3 if failure is None else 2)


def test_default_preparation_fetch_uses_trusted_transport(tmp_path: Path) -> None:
    """Default preparation uses remote credentials only for its bound fetch."""
    from unittest.mock import patch

    root = tmp_path / "mnemosyne"
    root.mkdir()
    branch = "learn/fixture"
    head = "a" * 40
    workspace = BoundLearningWorkspace()
    calls: list[tuple[str, ...]] = []

    def local(
        _cwd: Path, argv: tuple[str, ...], _timeout_s: int
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess([], 0, head if argv[0] == "rev-parse" else "")

    workspace._git = local
    with (
        patch.object(workspace, "_existing_pr", return_value=(7, head)),
        patch("hephaestus.automation.remote_git.trusted_gh_executable", return_value="/trusted/gh"),
        patch("hephaestus.automation.remote_git.trusted_gh_authenticated", return_value=True),
        patch(
            "hephaestus.automation.remote_git.trusted_remote_git_config",
            return_value=("-c", "credential.helper=trusted"),
        ),
        patch(
            "hephaestus.utils.helpers.run_subprocess",
            return_value=subprocess.CompletedProcess([], 0, ""),
        ) as run,
    ):
        workspace.prepare(_binding(tmp_path), branch)
    assert run.call_args.args[0] == [
        "git",
        "-c",
        "credential.helper=trusted",
        "fetch",
        "origin",
        branch,
    ]
    assert run.call_args.kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert all(call[0] != "fetch" for call in calls)


def test_plan_only_source_is_rejected_before_github_reads() -> None:
    """Plan approval cannot supply implementation evidence."""
    with pytest.raises(LearnDeliveryError, match="plan_only_learning_rejected"):
        GitHubLearningSourceReader(type("Facts", (), {"issue": lambda *_: {}})()).read(
            LearningIntent.approved_plan(
                repo="org/repo", issue=1, plan_revision=1, plan_fingerprint="a" * 64
            )
        )


@pytest.mark.parametrize(
    "checks", [[], [{"name": "tests", "bucket": "fail"}], [{"name": "tests", "bucket": "pending"}]]
)
def test_post_merge_rejects_missing_or_failing_checks(checks: list[dict[str, str]]) -> None:
    """Merged source needs passing required checks."""

    class Adapter:
        def pull_request(self, repository: str, pr: int) -> dict[str, object]:
            return {
                "number": pr,
                "state": "MERGED",
                "title": "Fix workers",
                "body": "Closes #2754",
                "url": "https://github.com/org/repo/pull/2800",
                "mergedAt": "2026-08-14T12:00:00Z",
                "mergeCommit": {"oid": "c" * 40},
                "headRefOid": "d" * 40,
                "closingIssuesReferences": [{"number": 2754}],
            }

        def verification(self, repository: str, pr: int, head: str) -> tuple[str, ...]:
            return tuple(check["name"] for check in checks if check["bucket"] == "pass")

    intent = LearningIntent.post_merge(repo="org/repo", issue=2754, pr=2800)
    with pytest.raises(LearnDeliveryError, match="passing required checks"):
        GitHubLearningSourceReader(Adapter()).read(intent)


def test_default_builder_defers_without_a_reviewed_candidate() -> None:
    """Passing checks do not make PR prose a reusable lesson."""
    source = PostMergeLearningSource(
        repository="org/repo",
        issue=1,
        pr=2,
        title="Fix workers",
        body="Closes #1",
        merged_at="2026-09-09T00:00:00Z",
        merge_commit_sha="a" * 40,
        url="https://github.com/org/repo/pull/2",
        verified_head="b" * 40,
        verification_evidence=("tests: https://example.test/check",),
    )
    with pytest.raises(LearnDeliveryError, match="learning_deferred:candidate_required"):
        MnemosyneLearningBuilder().build(
            LearningIntent.post_merge(repo="org/repo", issue=1, pr=2), source
        )


def test_candidate_duplicate_defers_before_workspace_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supplied candidate cannot duplicate a selected corpus entry."""
    from types import SimpleNamespace

    from hephaestus.automation.mnemosyne_corpus import MnemosyneSkillBlock
    from hephaestus.automation.mnemosyne_skill_host import DefaultCorpusReader

    monkeypatch.setattr(
        DefaultCorpusReader,
        "read",
        lambda *_: SimpleNamespace(
            blocks=(
                MnemosyneSkillBlock(
                    "worker-safety",
                    "skills/worker-safety.md",
                    "Worker recovery",
                    "# Worker safety\n",
                ),
            )
        ),
    )

    class Reader:
        def read(self, intent: LearningIntent) -> PostMergeLearningSource:
            return _source()

    service = MnemosyneLearningPreparationService(
        source_reader=Reader(), builder=CandidateBuilder()
    )
    with pytest.raises(LearnDeliveryError, match="learning_deferred:corpus_match_requires_review"):
        service.prepare(_intent().to_payload(), _binding(tmp_path))
    assert not Path(_binding(tmp_path).root).exists()


def test_preparation_rejects_legacy_plan_even_with_injected_reader(tmp_path: Path) -> None:
    """The host rejects a legacy plan before it calls a candidate producer."""

    class Reader:
        def read(self, intent: LearningIntent) -> PostMergeLearningSource:
            return _source()

    intent = LearningIntent.approved_plan(
        repo="org/repo", issue=1, plan_revision=1, plan_fingerprint="a" * 64
    )
    service = MnemosyneLearningPreparationService(
        source_reader=Reader(), builder=CandidateBuilder()
    )
    with pytest.raises(LearnDeliveryError, match="plan_only_learning_rejected"):
        service.prepare(intent.to_payload(), _binding(tmp_path))


def test_markdownlint_failure_prevents_validation_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plugin validation alone cannot authorize a malformed candidate."""
    import platform
    import shutil
    from collections.abc import Iterator
    from contextlib import contextmanager
    from types import SimpleNamespace

    from hephaestus.automation import mnemosyne_learning_preparation as preparation

    node = tmp_path / "node"
    cli = tmp_path / "markdownlint-cli2"
    node.write_text("node")
    cli.write_text("cli")
    monkeypatch.setattr(shutil, "which", lambda name: str(node if name == "node" else cli))
    monkeypatch.setattr(preparation, "node_runtime_files", lambda path: (path,))

    @contextmanager
    def prepared(_path: Path, _runner: object) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(
            root=tmp_path,
            runtime=tmp_path,
            environment=tmp_path / "environment",
            uv=tmp_path / "uv",
            verify=lambda _path: None,
        )

    monkeypatch.setattr(preparation, "prepare_dependencies", prepared)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    original = Path.is_file
    monkeypatch.setattr(
        Path, "is_file", lambda path: str(path) == "/usr/bin/sandbox-exec" or original(path)
    )

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1 if str(cli) in argv else 0)

    with pytest.raises(LearnDeliveryError, match="markdownlint"):
        MnemosynePluginValidator(runner=runner).validate(tmp_path)


@pytest.mark.parametrize("bucket", ["pass", "fail", "pending", "cancel", "skipping", "unknown"])
def test_required_checks_adapter_binds_passing_evidence(bucket: str) -> None:
    """Only passing required checks at the same merged head supply evidence."""
    import json

    from hephaestus.automation.mnemosyne_learning_preparation import GitHubLearningSourceAdapter

    def gh(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[1] == "checks":
            assert "--required" in argv
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    [{"name": "tests", "bucket": bucket, "link": "https://example.test/check"}]
                ),
            )
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"state": "MERGED", "headRefOid": "a" * 40})
        )

    adapter = GitHubLearningSourceAdapter(gh)
    if bucket == "pass":
        assert adapter.verification("org/repo", 2, "a" * 40) == (
            "tests: https://example.test/check",
        )
    else:
        with pytest.raises(LearnDeliveryError, match="passing required checks"):
            adapter.verification("org/repo", 2, "a" * 40)


def test_failed_candidate_worktree_is_preserved_on_retry(tmp_path: Path) -> None:
    """A retry cannot delete the first candidate and its failure evidence."""
    from hashlib import sha256

    root = tmp_path / "mnemosyne"
    branch = "learn/" + "a" * 16
    candidate = root / "build" / "mnemosyne-learning" / sha256(branch.encode()).hexdigest()[:16]
    candidate.mkdir(parents=True)
    artifact = candidate / "candidate.md"
    artifact.write_text("First candidate")
    calls: list[tuple[str, ...]] = []

    def git(_cwd: Path, argv: tuple[str, ...], _timeout_s: int) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 2 if argv[0] == "ls-remote" else 0, "")

    def gh(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "[]")

    with pytest.raises(LearnDeliveryError, match="candidate requires recovery"):
        BoundLearningWorkspace(git=git, gh=gh).prepare(_binding(tmp_path), branch)
    assert artifact.read_text() == "First candidate"
    assert not any(call[:2] == ("worktree", "remove") for call in calls)
