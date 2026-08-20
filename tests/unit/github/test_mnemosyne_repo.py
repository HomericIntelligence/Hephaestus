"""Tests for Athena-compatible Mnemosyne dependency resolution."""
# ruff: noqa: D103

from __future__ import annotations

from threading import Event
from unittest.mock import patch

import pytest

from hephaestus.github import mnemosyne_repo
from hephaestus.github.mnemosyne_repo import (
    LEGACY_OWNER_ENV_VAR,
    OWNER_ENV_VAR,
    UPSTREAM_SLUG,
    CurrentRepositoryMetadata,
    MnemosyneResolutionError,
    MnemosyneTarget,
    MnemosyneTrustBasis,
    RepositoryMetadata,
    fetch_current_repository_metadata,
    fetch_repository_metadata,
    resolve_mnemosyne_target,
    validate_owner,
)

SHA = "a" * 40
UPSTREAM_METADATA = RepositoryMetadata(
    slug=UPSTREAM_SLUG,
    default_branch="main",
    head_sha=SHA,
)


@pytest.fixture(autouse=True)
def _clear_owner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OWNER_ENV_VAR, raising=False)
    monkeypatch.delenv(LEGACY_OWNER_ENV_VAR, raising=False)
    monkeypatch.setattr(mnemosyne_repo, "_legacy_owner_warning_emitted", Event())


def _repo_metadata(slug: str, *, missing_ok: bool = False) -> RepositoryMetadata | None:
    del missing_ok
    if slug == UPSTREAM_SLUG:
        return UPSTREAM_METADATA
    return RepositoryMetadata(
        slug=slug,
        default_branch="trunk",
        head_sha="b" * 40,
        is_fork=True,
        parent_full_name=UPSTREAM_SLUG,
    )


def test_validate_owner_rejects_slugs_and_unsafe_names() -> None:
    assert validate_owner("good-owner") == "good-owner"
    for owner in ("bad/owner", "-bad", "bad-", "bad--owner", "", "with space"):
        with pytest.raises(MnemosyneResolutionError):
            validate_owner(owner)


def test_current_repository_metadata_reads_owner_type_when_repo_view_omits_it() -> None:
    with (
        patch.object(
            mnemosyne_repo,
            "_repo_view_json",
            return_value={
                "owner": {"login": "HomericIntelligence"},
                "viewerPermission": "ADMIN",
            },
        ),
        patch.object(
            mnemosyne_repo,
            "_gh_json",
            return_value={"type": "Organization"},
        ) as gh_json,
    ):
        metadata = fetch_current_repository_metadata()

    assert metadata == CurrentRepositoryMetadata(
        owner="HomericIntelligence",
        owner_type="Organization",
        viewer_permission="ADMIN",
    )
    gh_json.assert_called_once_with(["api", "users/HomericIntelligence"])


def test_repository_metadata_reads_default_head_ref_when_repo_view_omits_oid() -> None:
    with (
        patch.object(
            mnemosyne_repo,
            "_repo_view_json",
            return_value={
                "defaultBranchRef": {"name": "main"},
                "isFork": False,
                "parent": None,
            },
        ),
        patch.object(
            mnemosyne_repo,
            "_gh_json",
            return_value={"object": {"sha": SHA}},
        ) as gh_json,
    ):
        metadata = fetch_repository_metadata(UPSTREAM_SLUG)

    assert metadata == UPSTREAM_METADATA
    gh_json.assert_called_once_with(["api", f"repos/{UPSTREAM_SLUG}/git/ref/heads/main"])


def test_explicit_owner_uses_new_env_var_and_skips_current_repo_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OWNER_ENV_VAR, "acme")

    with (
        patch.object(mnemosyne_repo, "fetch_current_repository_metadata") as current,
        patch.object(mnemosyne_repo, "fetch_repository_metadata", side_effect=_repo_metadata),
    ):
        target = resolve_mnemosyne_target()

    assert target == MnemosyneTarget(
        owner="acme",
        slug="acme/Mnemosyne",
        is_fork_of_upstream=True,
        default_branch="trunk",
        head_sha="b" * 40,
        trust_basis=MnemosyneTrustBasis.EXPLICIT_OVERRIDE,
    )
    current.assert_not_called()


def test_invalid_explicit_owner_is_fatal() -> None:
    with pytest.raises(MnemosyneResolutionError, match="invalid Mnemosyne owner"):
        resolve_mnemosyne_target(override_owner="bad/owner")


def test_maintained_organization_fork_wins_before_canonical_upstream() -> None:
    current = CurrentRepositoryMetadata(
        owner="HomericLab",
        owner_type="Organization",
        viewer_permission="WRITE",
    )

    with (
        patch.object(mnemosyne_repo, "fetch_current_repository_metadata", return_value=current),
        patch.object(mnemosyne_repo, "fetch_repository_metadata", side_effect=_repo_metadata),
    ):
        target = resolve_mnemosyne_target()

    assert target.slug == "HomericLab/Mnemosyne"
    assert target.trust_basis == MnemosyneTrustBasis.MAINTAINED_ORGANIZATION_FORK
    assert target.default_branch == "trunk"
    assert target.head_sha == "b" * 40


@pytest.mark.parametrize(
    "current",
    [
        CurrentRepositoryMetadata("mvillmow", "User", "ADMIN"),
        CurrentRepositoryMetadata("HomericLab", "Organization", "READ"),
    ],
)
def test_ineligible_current_owner_falls_back_to_canonical_upstream(
    current: CurrentRepositoryMetadata,
) -> None:
    with (
        patch.object(mnemosyne_repo, "fetch_current_repository_metadata", return_value=current),
        patch.object(mnemosyne_repo, "fetch_repository_metadata", side_effect=_repo_metadata),
    ):
        target = resolve_mnemosyne_target()

    assert target.slug == UPSTREAM_SLUG
    assert target.trust_basis == MnemosyneTrustBasis.CANONICAL_UPSTREAM
    assert target.head_sha == SHA


def test_missing_or_unproven_organization_fork_falls_back_to_canonical_upstream() -> None:
    current = CurrentRepositoryMetadata("HomericLab", "Organization", "MAINTAIN")

    def metadata(slug: str, *, missing_ok: bool = False) -> RepositoryMetadata | None:
        if slug == "HomericLab/Mnemosyne":
            assert missing_ok is True
            return None
        return UPSTREAM_METADATA

    with (
        patch.object(mnemosyne_repo, "fetch_current_repository_metadata", return_value=current),
        patch.object(mnemosyne_repo, "fetch_repository_metadata", side_effect=metadata),
    ):
        target = resolve_mnemosyne_target()

    assert target.slug == UPSTREAM_SLUG
    assert target.trust_basis == MnemosyneTrustBasis.CANONICAL_UPSTREAM


def test_github_api_failure_is_fatal_when_trust_cannot_be_decided() -> None:
    with patch.object(
        mnemosyne_repo,
        "fetch_current_repository_metadata",
        side_effect=MnemosyneResolutionError("auth failed"),
    ):
        with pytest.raises(MnemosyneResolutionError, match="auth failed"):
            resolve_mnemosyne_target()


def test_legacy_owner_warning_is_emitted_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(LEGACY_OWNER_ENV_VAR, "legacy-owner")
    current = CurrentRepositoryMetadata("mvillmow", "User", "ADMIN")

    with (
        patch.object(mnemosyne_repo, "fetch_current_repository_metadata", return_value=current),
        patch.object(mnemosyne_repo, "fetch_repository_metadata", side_effect=_repo_metadata),
    ):
        resolve_mnemosyne_target()
        resolve_mnemosyne_target()

    messages = [
        record.message for record in caplog.records if LEGACY_OWNER_ENV_VAR in record.message
    ]
    assert len(messages) == 1
