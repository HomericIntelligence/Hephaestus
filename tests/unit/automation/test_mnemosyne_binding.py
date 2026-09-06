"""Tests for binding a local Mnemosyne checkout to a trusted target."""
# ruff: noqa: D103

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import hephaestus.automation.mnemosyne_binding as mnemosyne_binding
from hephaestus.automation.athena_contract import AthenaContractReceipt
from hephaestus.automation.mnemosyne_binding import (
    MnemosyneBindingError,
    MnemosyneBindingService,
    _origin_matches,
    default_mnemosyne_root,
)
from hephaestus.github.mnemosyne_repo import (
    MnemosyneResolutionError,
    MnemosyneTarget,
    MnemosyneTrustBasis,
)

SHA = "c" * 40
AVAILABLE_VERSION = "3.0.0"
UNSAFE_CONFIG_COMMAND = "config --null --list"


def _target() -> MnemosyneTarget:
    return MnemosyneTarget(
        owner="HomericIntelligence",
        slug="HomericIntelligence/Mnemosyne",
        is_fork_of_upstream=False,
        default_branch="main",
        trust_basis=MnemosyneTrustBasis.CANONICAL_UPSTREAM,
    )


def _contract() -> AthenaContractReceipt:
    return AthenaContractReceipt(
        athena_repository="github.com/HomericIntelligence/Athena",
        athena_commit="a" * 40,
        advise_sha256="1" * 64,
        learn_sha256="2" * 64,
        dependency_resolution_sha256="3" * 64,
        trust_source="test",
    )


class FakeGit:
    """Scriptable git runner keyed by argv tuple."""

    def __init__(self, **overrides: subprocess.CompletedProcess[str]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.overrides = overrides

    def __call__(
        self,
        cwd: Path,
        argv: tuple[str, ...],
        timeout_s: int,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout_s
        self.calls.append(argv)
        key = " ".join(_git_command(argv))
        if key in self.overrides:
            return self.overrides[key]
        responses = {
            "rev-parse --is-inside-work-tree": _completed(stdout="true\n"),
            "config --get remote.origin.url": _completed(
                stdout="https://github.com/HomericIntelligence/Mnemosyne.git\n"
            ),
            UNSAFE_CONFIG_COMMAND: _completed(),
            "status --porcelain": _completed(),
            "fetch origin": _completed(),
            "checkout main": _completed(),
            "merge --ff-only origin/main": _completed(),
            "rev-parse HEAD": _completed(stdout=f"{SHA}\n"),
            f"show {SHA}:pyproject.toml": _completed(
                stdout=(f'[project]\nname = "Project-Mnemosyne"\nversion = "{AVAILABLE_VERSION}"\n')
            ),
        }
        return responses[key]


def _git_command(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Remove command-scoped Git configuration from a recorded command."""
    index = 0
    while argv[index : index + 1] == ("-c",):
        index += 2
    return argv[index:]


def _completed(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr=stderr)


def test_default_root_uses_agent_brain_knowledge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert default_mnemosyne_root() == tmp_path / ".agent_brain" / "knowledge"


def test_binding_success_reports_available_version_and_contract(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    service = MnemosyneBindingService(
        root=root,
        resolver=_target,
        git=FakeGit(),
        remote_git_config=(),
    )

    receipt = service.bind(contract=_contract())

    assert receipt.repository == "HomericIntelligence/Mnemosyne"
    assert receipt.version == AVAILABLE_VERSION
    assert receipt.commit_sha == SHA
    assert receipt.sync_status == "updated"
    assert receipt.default_branch == "main"
    assert receipt.trust_basis == "canonical upstream"
    assert receipt.athena_contract["athena_commit"] == "a" * 40


def test_binding_bootstraps_missing_checkout_then_binds_it(tmp_path: Path) -> None:
    root = tmp_path / ".agent_brain" / "knowledge"

    class CloningGit(FakeGit):
        def __call__(
            self,
            cwd: Path,
            argv: tuple[str, ...],
            timeout_s: int,
        ) -> subprocess.CompletedProcess[str]:
            command = _git_command(argv)
            if command[0] == "clone":
                assert cwd == root.parent
                assert command == (
                    "clone",
                    "--origin",
                    "origin",
                    "--branch",
                    "main",
                    "https://github.com/HomericIntelligence/Mnemosyne.git",
                    str(root),
                )
                root.mkdir()
                self.calls.append(argv)
                return _completed()
            return super().__call__(cwd, argv, timeout_s)

    git = CloningGit()
    receipt = MnemosyneBindingService(
        root=root, resolver=_target, git=git, remote_git_config=()
    ).bind(contract=_contract())

    assert root.parent.is_dir()
    assert receipt.commit_sha == SHA
    assert _git_command(git.calls[0])[0] == "clone"


def test_binding_authenticates_remote_fetch_with_trusted_github_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A remote fetch uses the trusted command-scoped GitHub credential helper."""
    root = tmp_path / "knowledge"
    root.mkdir()
    git = FakeGit()
    monkeypatch.setattr(
        mnemosyne_binding,
        "trusted_gh_executable",
        lambda _extra_path_root=None: "/trusted/bin/gh",
        raising=False,
    )
    monkeypatch.setattr(
        mnemosyne_binding,
        "trusted_remote_git_config",
        lambda command: (
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper=!{command} auth git-credential",
        ),
        raising=False,
    )
    monkeypatch.setattr(
        mnemosyne_binding,
        "_gh_auth_status",
        lambda _command, _timeout: True,
        raising=False,
    )

    MnemosyneBindingService(root=root, resolver=_target, git=git).bind(contract=_contract())

    fetch = next(call for call in git.calls if _git_command(call) == ("fetch", "origin"))
    assert fetch[:4] == (
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!/trusted/bin/gh auth git-credential",
    )


@pytest.mark.parametrize(
    "origin",
    [
        "HomericIntelligence/Mnemosyne",
        "https://attacker.invalid/HomericIntelligence/Mnemosyne.git",
        "attacker.invalid:HomericIntelligence/Mnemosyne.git",
        "file:///tmp/HomericIntelligence/Mnemosyne.git",
        "/tmp/HomericIntelligence/Mnemosyne",
    ],
)
def test_origin_match_rejects_noncanonical_github_origins(origin: str) -> None:
    assert not _origin_matches(origin, "HomericIntelligence/Mnemosyne")


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/HomericIntelligence/Mnemosyne.git",
        "git@github.com:HomericIntelligence/Mnemosyne.git",
        "ssh://git@github.com/HomericIntelligence/Mnemosyne.git",
    ],
)
def test_origin_match_accepts_exact_github_origins(origin: str) -> None:
    assert _origin_matches(origin, "HomericIntelligence/Mnemosyne")


def test_binding_rejects_url_rewrite_before_authenticated_fetch(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    git = FakeGit(
        **{
            UNSAFE_CONFIG_COMMAND: _completed(
                stdout=("url.https://attacker.invalid/.insteadof\nhttps://github.com/\0")
            )
        }
    )

    with pytest.raises(MnemosyneBindingError, match="unsafe Git config"):
        MnemosyneBindingService(root=root, resolver=_target, git=git, remote_git_config=()).bind(
            contract=_contract()
        )

    assert all(_git_command(call) != ("fetch", "origin") for call in git.calls)


def test_binding_uses_available_version_without_github_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    git = FakeGit()
    monkeypatch.setattr(
        mnemosyne_binding,
        "trusted_gh_executable",
        lambda _extra_path_root=None: "/trusted/bin/gh",
    )
    monkeypatch.setattr(
        mnemosyne_binding,
        "_gh_auth_status",
        lambda _command, _timeout: False,
        raising=False,
    )

    receipt = MnemosyneBindingService(root=root, resolver=_target, git=git).bind(
        contract=_contract()
    )

    assert receipt.version == AVAILABLE_VERSION
    assert receipt.sync_status == "not_updated"
    assert all(_git_command(call) != ("fetch", "origin") for call in git.calls)


def test_binding_redacts_unsafe_config_value(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    sensitive_value = "ghp_1234567890abcdefghijklmnopqrstuvwxyzABCDE"
    git = FakeGit(
        **{UNSAFE_CONFIG_COMMAND: _completed(stdout=f"core.sshCommand\nssh -i {sensitive_value}\0")}
    )

    with pytest.raises(MnemosyneBindingError) as exc_info:
        MnemosyneBindingService(root=root, resolver=_target, git=git, remote_git_config=()).bind(
            contract=_contract()
        )

    assert str(exc_info.value) == "unsafe Git configuration"
    assert sensitive_value not in str(exc_info.value)


def test_binding_uses_available_version_without_trusted_github_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing trusted helper does not block an available local version."""
    root = tmp_path / "knowledge"
    root.mkdir()
    git = FakeGit()
    monkeypatch.setattr(
        mnemosyne_binding,
        "trusted_gh_executable",
        lambda _extra_path_root=None: None,
        raising=False,
    )

    receipt = MnemosyneBindingService(root=root, resolver=_target, git=git).bind(
        contract=_contract()
    )

    assert receipt.version == AVAILABLE_VERSION
    assert receipt.commit_sha == SHA
    assert receipt.sync_status == "not_updated"
    assert all(_git_command(call) != ("fetch", "origin") for call in git.calls)


def test_binding_uses_available_version_when_remote_fetch_fails(tmp_path: Path) -> None:
    """A remote fetch failure does not block an available local version."""
    root = tmp_path / "knowledge"
    root.mkdir()
    sensitive_value = "transport-sensitive-value"
    git = FakeGit(
        **{
            "fetch origin": _completed(
                returncode=128, stderr=f"access denied for {sensitive_value}"
            )
        }
    )

    receipt = MnemosyneBindingService(
        root=root, resolver=_target, git=git, remote_git_config=()
    ).bind(contract=_contract())

    assert receipt.version == AVAILABLE_VERSION
    assert receipt.commit_sha == SHA
    assert receipt.sync_status == "not_updated"
    assert sensitive_value not in str(receipt.to_dict())


def test_binding_uses_available_version_when_fast_forward_fails(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    git = FakeGit(
        **{
            "merge --ff-only origin/main": _completed(
                returncode=1,
                stderr="not possible to fast-forward",
            )
        }
    )

    receipt = MnemosyneBindingService(
        root=root, resolver=_target, git=git, remote_git_config=()
    ).bind(contract=_contract())

    assert receipt.version == AVAILABLE_VERSION
    assert receipt.commit_sha == SHA
    assert receipt.sync_status == "not_updated"


def test_binding_can_use_available_version_without_sync(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    git = FakeGit()

    receipt = MnemosyneBindingService(
        root=root, resolver=_target, git=git, remote_git_config=()
    ).bind(contract=_contract(), sync=False)

    assert receipt.version == AVAILABLE_VERSION
    assert receipt.sync_status == "not_requested"
    assert all(_git_command(call) != ("fetch", "origin") for call in git.calls)


def test_binding_uses_canonical_available_version_when_resolution_is_offline(
    tmp_path: Path,
) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()

    def unavailable() -> MnemosyneTarget:
        raise MnemosyneResolutionError("GitHub is unavailable")

    receipt = MnemosyneBindingService(
        root=root,
        resolver=unavailable,
        git=FakeGit(),
        remote_git_config=(),
    ).bind(contract=_contract(), sync=False)

    assert receipt.repository == "HomericIntelligence/Mnemosyne"
    assert receipt.default_branch == "main"
    assert receipt.version == AVAILABLE_VERSION
    assert receipt.sync_status == "not_requested"


def test_binding_rejects_unverified_fork_when_resolution_is_offline(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()

    def unavailable() -> MnemosyneTarget:
        raise MnemosyneResolutionError("GitHub is unavailable")

    git = FakeGit(
        **{
            "config --get remote.origin.url": _completed(
                stdout="https://github.com/another-owner/Mnemosyne.git\n"
            )
        }
    )

    with pytest.raises(MnemosyneResolutionError, match="GitHub is unavailable"):
        MnemosyneBindingService(
            root=root,
            resolver=unavailable,
            git=git,
            remote_git_config=(),
        ).bind(contract=_contract(), sync=False)


def test_binding_wrong_origin_does_not_expose_embedded_credential(tmp_path: Path) -> None:
    """A foreign origin failure does not copy a URL credential to diagnostics."""
    root = tmp_path / "knowledge"
    root.mkdir()
    sensitive_value = "ghp_1234567890abcdefghijklmnopqrstuvwxyzABCDE"
    git = FakeGit(
        **{
            "config --get remote.origin.url": _completed(
                stdout=f"https://{sensitive_value}@github.com/Other/repository.git\n"
            )
        }
    )

    with pytest.raises(MnemosyneBindingError) as exc_info:
        MnemosyneBindingService(root=root, resolver=_target, git=git, remote_git_config=()).bind(
            contract=_contract()
        )

    assert "wrong origin" in str(exc_info.value)
    assert sensitive_value not in str(exc_info.value)


def test_binding_rejects_symlinked_checkout_parent_before_clone(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-agent-brain"
    real_parent.mkdir()
    parent = tmp_path / ".agent_brain"
    parent.symlink_to(real_parent, target_is_directory=True)
    root = parent / "knowledge"
    git = FakeGit()

    with pytest.raises(MnemosyneBindingError, match="parent must not be a symlink"):
        MnemosyneBindingService(root=root, resolver=_target, git=git, remote_git_config=()).bind(
            contract=_contract()
        )

    assert git.calls == []


def test_binding_fails_closed_when_checkout_clone_fails(tmp_path: Path) -> None:
    root = tmp_path / ".agent_brain" / "knowledge"

    class FailingCloneGit(FakeGit):
        def __call__(
            self,
            cwd: Path,
            argv: tuple[str, ...],
            timeout_s: int,
        ) -> subprocess.CompletedProcess[str]:
            if _git_command(argv)[0] == "clone":
                self.calls.append(argv)
                return _completed(returncode=128, stderr="access denied")
            return super().__call__(cwd, argv, timeout_s)

    git = FailingCloneGit()
    with pytest.raises(MnemosyneBindingError, match="clone failed: remote Git transport"):
        MnemosyneBindingService(root=root, resolver=_target, git=git, remote_git_config=()).bind(
            contract=_contract()
        )

    assert root.parent.is_dir()
    assert root.exists() is False
    assert [_git_command(call) for call in git.calls] == [
        (
            "clone",
            "--origin",
            "origin",
            "--branch",
            "main",
            "https://github.com/HomericIntelligence/Mnemosyne.git",
            str(root),
        )
    ]


@pytest.mark.parametrize(
    ("override", "match"),
    [
        (
            {
                "config --get remote.origin.url": _completed(
                    stdout="https://github.com/evil/Mnemosyne.git\n"
                )
            },
            "wrong origin",
        ),
        ({"status --porcelain": _completed(stdout=" M skills/example.md\n")}, "dirty"),
        (
            {UNSAFE_CONFIG_COMMAND: _completed(stdout="core.hooksPath\n.githooks\0")},
            "unsafe Git config",
        ),
    ],
)
def test_binding_rejects_untrusted_checkout_states(
    tmp_path: Path,
    override: dict[str, subprocess.CompletedProcess[str]],
    match: str,
) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    service = MnemosyneBindingService(
        root=root,
        resolver=_target,
        git=FakeGit(**override),
        remote_git_config=(),
    )

    with pytest.raises(MnemosyneBindingError, match=match):
        service.bind(contract=_contract())


def test_binding_records_local_commit_only_as_version_provenance(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    local_sha = "d" * 40
    git = FakeGit(
        **{
            "rev-parse HEAD": _completed(stdout=f"{local_sha}\n"),
            f"show {local_sha}:pyproject.toml": _completed(
                stdout=(f'[project]\nname = "Project-Mnemosyne"\nversion = "{AVAILABLE_VERSION}"\n')
            ),
        }
    )

    receipt = MnemosyneBindingService(
        root=root,
        resolver=_target,
        git=git,
        remote_git_config=(),
    ).bind(contract=_contract())

    assert receipt.version == AVAILABLE_VERSION
    assert receipt.commit_sha == local_sha
    assert receipt.sync_status == "updated"


@pytest.mark.parametrize(
    "pyproject",
    [
        '[project]\nname = "another-project"\nversion = "3.0.0"\n',
        '[project]\nname = "project-mnemosyne"\nversion = "not a version"\n',
        '[tool.example]\nversion = "3.0.0"\n',
    ],
)
def test_binding_rejects_invalid_available_version_metadata(tmp_path: Path, pyproject: str) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    git = FakeGit(**{f"show {SHA}:pyproject.toml": _completed(stdout=pyproject)})

    with pytest.raises(MnemosyneBindingError, match=r"project|version"):
        MnemosyneBindingService(
            root=root,
            resolver=_target,
            git=git,
            remote_git_config=(),
        ).bind(contract=_contract())


def test_binding_rejects_symlinked_checkout(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    root = tmp_path / "knowledge"
    root.symlink_to(real, target_is_directory=True)
    service = MnemosyneBindingService(
        root=root, resolver=_target, git=FakeGit(), remote_git_config=()
    )

    with pytest.raises(MnemosyneBindingError, match="symlink"):
        service.bind(contract=_contract())
