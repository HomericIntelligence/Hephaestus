"""Tests for selected Mnemosyne corpus loading."""
# ruff: noqa: D103

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hephaestus.automation.athena_contract import AthenaContractReceipt
from hephaestus.automation.mnemosyne_binding import MnemosyneBindingReceipt
from hephaestus.automation.mnemosyne_corpus import (
    MnemosyneCorpusError,
    SkillSelection,
    read_selected_skill_corpus,
)

SHA = "e" * 40


def _contract() -> AthenaContractReceipt:
    return AthenaContractReceipt(
        athena_repository="github.com/HomericIntelligence/Athena",
        athena_commit="a" * 40,
        advise_sha256="1" * 64,
        learn_sha256="2" * 64,
        dependency_resolution_sha256="3" * 64,
        trust_source="test",
    )


def _binding() -> MnemosyneBindingReceipt:
    return MnemosyneBindingReceipt(
        root="/tmp/knowledge",
        repository="HomericIntelligence/Mnemosyne",
        default_branch="main",
        version="3.0.0",
        commit_sha=SHA,
        sync_status="updated",
        trust_basis="canonical upstream",
        athena_contract=_contract().to_dict(),
    )


class FakeGit:
    """Git object reader for committed fixture blobs."""

    def __init__(self, blobs: dict[str, str], modes: dict[str, str] | None = None) -> None:
        self.blobs = blobs
        self.modes = modes or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        cwd: Path,
        argv: tuple[str, ...],
        timeout_s: int,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout_s
        self.calls.append(argv)
        if argv[0] == "ls-tree":
            path = argv[2]
            mode = self.modes.get(path, "100644")
            return _completed(stdout=f"{mode} blob {SHA}\t{path}\n")
        if argv[0] == "cat-file":
            path = argv[2].split(":", 1)[1]
            if path in self.blobs:
                return _completed(stdout="blob\n")
            return _completed(returncode=128, stderr="missing")
        if argv[0] == "show":
            path = argv[1].split(":", 1)[1]
            return _completed(stdout=self.blobs[path])
        raise AssertionError(f"unexpected git argv {argv}")


def _completed(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr=stderr)


def test_reads_complete_flat_skill_blobs_without_marketplace_or_truncation(tmp_path: Path) -> None:
    long_content = "A" * 50_000
    git = FakeGit(
        {
            "skills/debugging.md": long_content,
            "skills/testing.md": "# Testing\n\nUse behavior tests.\n",
        }
    )

    result = read_selected_skill_corpus(
        root=tmp_path,
        binding=_binding(),
        contract=_contract(),
        selections=(
            SkillSelection(name="debugging", source="skills/debugging.md", reason="repro"),
            SkillSelection(name="testing", source="skills/testing.md", reason="coverage"),
        ),
        git=git,
    )

    assert long_content in result.context
    assert "[truncated]" not in result.context
    assert result.evidence["selected_paths"] == ["skills/debugging.md", "skills/testing.md"]
    assert not any(".claude-plugin" in " ".join(call) for call in git.calls)


@pytest.mark.parametrize(
    "source",
    [
        "skills/debugging.notes.md",
        "skills/nested/debugging.md",
        "docs/debugging.md",
        "../skills/debugging.md",
    ],
)
def test_rejects_out_of_contract_paths(tmp_path: Path, source: str) -> None:
    with pytest.raises(MnemosyneCorpusError, match="out-of-contract"):
        read_selected_skill_corpus(
            root=tmp_path,
            binding=_binding(),
            contract=_contract(),
            selections=(SkillSelection("bad", source, "reason"),),
            git=FakeGit({source: "content"}),
        )


def test_rejects_duplicate_selection_paths(tmp_path: Path) -> None:
    with pytest.raises(MnemosyneCorpusError, match="duplicate"):
        read_selected_skill_corpus(
            root=tmp_path,
            binding=_binding(),
            contract=_contract(),
            selections=(
                SkillSelection("one", "skills/debugging.md", "first"),
                SkillSelection("two", "skills/debugging.md", "second"),
            ),
            git=FakeGit({"skills/debugging.md": "content"}),
        )


def test_rejects_more_than_five_selected_entries(tmp_path: Path) -> None:
    selections = tuple(
        SkillSelection(f"skill-{index}", f"skills/skill-{index}.md", "reason") for index in range(6)
    )

    with pytest.raises(MnemosyneCorpusError, match="at most five"):
        read_selected_skill_corpus(
            root=tmp_path,
            binding=_binding(),
            contract=_contract(),
            selections=selections,
            git=FakeGit({selection.source: "content" for selection in selections}),
        )


def test_rejects_symlink_and_non_blob_entries(tmp_path: Path) -> None:
    with pytest.raises(MnemosyneCorpusError, match="symlink"):
        read_selected_skill_corpus(
            root=tmp_path,
            binding=_binding(),
            contract=_contract(),
            selections=(SkillSelection("debugging", "skills/debugging.md", "reason"),),
            git=FakeGit({"skills/debugging.md": "content"}, {"skills/debugging.md": "120000"}),
        )

    with pytest.raises(MnemosyneCorpusError, match="non-blob"):
        read_selected_skill_corpus(
            root=tmp_path,
            binding=_binding(),
            contract=_contract(),
            selections=(SkillSelection("debugging", "skills/debugging.md", "reason"),),
            git=FakeGit({"skills/debugging.md": "content"}, {"skills/debugging.md": "040000"}),
        )
