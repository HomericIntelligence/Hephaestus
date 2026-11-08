"""Behaviour of the stage-neutral direct-scope payload keys (issue #3521)."""

from __future__ import annotations

import pytest

import hephaestus.automation.issue_waves as issue_waves
from hephaestus.automation.pipeline import direct_scope_keys

_NONCE_ACCEPTED = [
    pytest.param("a" * 32, id="lowercase"),
    pytest.param("0123456789abcdef" * 2, id="digits-and-letters"),
]

_NONCE_REJECTED = [
    pytest.param("a" * 31, id="31-chars"),
    pytest.param("a" * 33, id="33-chars"),
    pytest.param("", id="empty"),
    pytest.param(None, id="none"),
    pytest.param(b"a" * 32, id="bytes"),
    pytest.param("g" * 32, id="non-hex"),
    pytest.param("A" * 32, id="uppercase"),
    pytest.param("a" * 32 + "\n", id="trailing-newline"),
]


@pytest.mark.parametrize("value", _NONCE_ACCEPTED)
def test_direct_scope_worktree_nonce_accepts_one_uuid4_hex(value: str) -> None:
    """The coordinator's UUID4 hex token is the only accepted shape."""
    assert direct_scope_keys.is_direct_scope_worktree_nonce(value) is True


@pytest.mark.parametrize("value", _NONCE_REJECTED)
def test_direct_scope_worktree_nonce_rejects_other_shapes(value: object) -> None:
    """A token of another length, type, or alphabet is not a nonce."""
    assert direct_scope_keys.is_direct_scope_worktree_nonce(value) is False


def test_stage_modules_import_direct_scope_names_from_the_neutral_owner() -> None:
    """Stage-side consumers use the one definition in the neutral module.

    ``repo`` writes the bootstrap keys and ``finished`` reads the reservation
    key. Each object must be the same one that the neutral module defines.
    """
    import hephaestus.automation.pipeline.stages.finished as finished
    import hephaestus.automation.pipeline.stages.repo as repo_stage

    consumers = (
        (repo_stage, "DIRECT_SCOPE_BASE_SHA_KEY"),
        (repo_stage, "DIRECT_SCOPE_BOOTSTRAP_KEY"),
        (finished, "DIRECT_SCOPE_RESERVATION_KEY"),
        (finished, "DIRECT_SCOPE_LOCAL_BRANCH_CLEANUP_KEY"),
    )
    for module, name in consumers:
        assert getattr(module, name) is getattr(direct_scope_keys, name), name
        assert module.is_full_commit_sha is direct_scope_keys.is_full_commit_sha, name


def test_no_module_defines_a_direct_scope_key_outside_the_neutral_owner() -> None:
    """One owner: only ``direct_scope_keys`` assigns a ``DIRECT_SCOPE_*`` name."""
    import ast
    import pathlib

    import hephaestus

    root = pathlib.Path(hephaestus.__file__).parent
    offenders: list[str] = []
    for py in sorted(root.rglob("*.py")):
        if py == pathlib.Path(direct_scope_keys.__file__):
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in tree.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id.startswith("DIRECT_SCOPE_"):
                    offenders.append(f"{py.relative_to(root)}:{node.lineno}")
    assert not offenders, offenders


def test_direct_scope_keys_has_one_commit_sha_owner() -> None:
    """``is_full_commit_sha`` stays the ``issue_waves`` function object."""
    assert direct_scope_keys.is_full_commit_sha is issue_waves.is_full_commit_sha
    assert direct_scope_keys.is_full_commit_sha("f" * 40) is True
    assert direct_scope_keys.is_full_commit_sha("f" * 39) is False
