"""Tests for binding a local Mnemosyne checkout to a trusted target."""
# ruff: noqa: D103

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hephaestus.automation.athena_contract import AthenaContractReceipt
from hephaestus.automation.mnemosyne_binding import (
    MnemosyneBindingError,
    MnemosyneBindingService,
    default_mnemosyne_root,
)
from hephaestus.github.mnemosyne_repo import MnemosyneTarget, MnemosyneTrustBasis

SHA = "c" * 40
UNSAFE_CONFIG_COMMAND = (
    "config --local --get-regexp "
    "^(alias\\.|include\\.|includeIf\\.|core\\.hooksPath|core\\.fsmonitor|core\\.sshCommand)"
)


def _target() -> MnemosyneTarget:
    return MnemosyneTarget(
        owner="HomericIntelligence",
        slug="HomericIntelligence/Mnemosyne",
        is_fork_of_upstream=False,
        default_branch="main",
        head_sha=SHA,
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
        key = " ".join(argv)
        if key in self.overrides:
            return self.overrides[key]
        responses = {
            "rev-parse --is-inside-work-tree": _completed(stdout="true\n"),
            "config --get remote.origin.url": _completed(
                stdout="https://github.com/HomericIntelligence/Mnemosyne.git\n"
            ),
            UNSAFE_CONFIG_COMMAND: _completed(returncode=1),
            "status --porcelain": _completed(),
            "fetch origin": _completed(),
            "checkout main": _completed(),
            "merge --ff-only origin/main": _completed(),
            "rev-parse HEAD": _completed(stdout=f"{SHA}\n"),
        }
        return responses[key]


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


def test_binding_success_reports_target_revision_and_contract(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    service = MnemosyneBindingService(
        root=root,
        resolver=_target,
        git=FakeGit(),
    )

    receipt = service.bind(contract=_contract())

    assert receipt.repository == "HomericIntelligence/Mnemosyne"
    assert receipt.commit_sha == SHA
    assert receipt.default_branch == "main"
    assert receipt.trust_basis == "canonical upstream"
    assert receipt.athena_contract["athena_commit"] == "a" * 40


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
            {UNSAFE_CONFIG_COMMAND: _completed(stdout="core.hooksPath .githooks\n")},
            "unsafe Git config",
        ),
        ({"rev-parse HEAD": _completed(stdout=f"{'d' * 40}\n")}, "revision drift"),
    ],
)
def test_binding_rejects_untrusted_checkout_states(
    tmp_path: Path,
    override: dict[str, subprocess.CompletedProcess[str]],
    match: str,
) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    service = MnemosyneBindingService(root=root, resolver=_target, git=FakeGit(**override))

    with pytest.raises(MnemosyneBindingError, match=match):
        service.bind(contract=_contract())


def test_binding_rejects_symlinked_checkout(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    root = tmp_path / "knowledge"
    root.symlink_to(real, target_is_directory=True)
    service = MnemosyneBindingService(root=root, resolver=_target, git=FakeGit())

    with pytest.raises(MnemosyneBindingError, match="symlink"):
        service.bind(contract=_contract())
