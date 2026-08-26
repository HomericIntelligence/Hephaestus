"""Regression test: MIGRATION.md derives release status from signed tags.

Release status is defined by hatch-vcs's latest signed ``vX.Y.Z`` tag. This
guard keeps the migration guide aligned with that durable contract instead of
requiring a documentation edit for every release. See issue #1208.

This test is designed to RUN in CI, not skip: the unit-test job checks out with
the CI checkout fetches tags before running this suite. If tags are somehow
absent, the test FAILS LOUD with remediation guidance rather than skipping --
a guard that silently skips is not a guard.
"""

import sys
from pathlib import Path

import pytest

from hephaestus.version.consistency import _version_from_git_tag

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_MD = REPO_ROOT / "docs" / "MIGRATION.md"

_TAG_DERIVED_STATUS = "latest signed `vX.Y.Z` git tag"


def test_migration_md_derives_release_status_from_signed_git_tag() -> None:
    """Require the migration guide to direct readers to the tag source of truth."""
    canonical = _version_from_git_tag(REPO_ROOT)
    if canonical is None:
        # No vX.Y.Z tag resolvable — almost always a shallow/tagless checkout
        # (e.g. a CI job whose actions/checkout lacks fetch-depth:0 + fetch-tags).
        # The guide's release-status contract cannot be verified without tags,
        # so fail loudly rather than recording a green-but-skipped receipt.
        pytest.fail(
            "No vX.Y.Z git tag reachable. CI checkout must be deep + tagged: "
            "run `git fetch --tags` locally; in the workflow set "
            "`fetch-depth: 0` and `fetch-tags: true` on actions/checkout."
        )
        return  # unreachable (pytest.fail raises); narrows canonical to str for mypy

    text = MIGRATION_MD.read_text(encoding="utf-8")
    assert _TAG_DERIVED_STATUS in text, (
        "MIGRATION.md must state that the latest signed vX.Y.Z tag is the "
        "release-status source of truth; do not add a per-release version claim."
    )


def test_migration_md_release_status_guard_fails_when_git_tags_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail loudly when tags are unavailable instead of recording a skipped guard."""
    monkeypatch.setattr(sys.modules[__name__], "_version_from_git_tag", lambda _root: None)

    with pytest.raises(pytest.fail.Exception, match=r"No vX\.Y\.Z git tag reachable"):
        test_migration_md_derives_release_status_from_signed_git_tag()


def test_release_docs_require_signed_tag_path_for_dynamic_versions() -> None:
    """Keep all release-facing docs aligned with the fail-closed CLI contract."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    releasing = (REPO_ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")

    assert "refuses hatch-vcs state" in readme
    assert "fails closed" in contributing
    assert "signed Auto Tag Release" in contributing
    assert "fails closed before reading the current" in releasing
    assert "Actions → Auto Tag Release → Run workflow" in releasing
