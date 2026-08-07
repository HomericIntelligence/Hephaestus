"""Regression test: MIGRATION.md version claim must not trail the latest git tag.

Pattern D (stop maintaining the number by hand): instead of relying on a human
to notice the dated 'latest released version' line has gone stale, this test
fails CI when the documented version is *older* than the canonical hatch-vcs
version (latest vX.Y.Z git tag). See issue #1208.

This test is designed to RUN in CI, not skip: the unit-test job checks out with
the CI checkout fetches tags before running this suite. If tags are somehow
absent, the test FAILS LOUD with remediation guidance rather than skipping --
a guard that silently skips is not a guard.
"""

import re
import sys
from pathlib import Path

import pytest

from hephaestus.version.consistency import _version_from_git_tag
from hephaestus.version.parsing import parse_version_tuple

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_MD = REPO_ROOT / "docs" / "MIGRATION.md"

# Matches: "The latest released version is **0.9.5**"
_LATEST_RE = re.compile(r"latest released version is \*\*(\d+\.\d+\.\d+)\*\*", re.IGNORECASE)


def test_migration_md_version_does_not_trail_latest_git_tag() -> None:
    """Fail if MIGRATION.md's 'latest released version' trails the newest git tag."""
    canonical = _version_from_git_tag(REPO_ROOT)
    if canonical is None:
        # No vX.Y.Z tag resolvable — almost always a shallow/tagless checkout
        # (e.g. a CI job whose actions/checkout lacks fetch-depth:0 + fetch-tags).
        # The drift guard cannot validate the release declaration without tags,
        # so fail loudly rather than recording a green-but-skipped receipt.
        pytest.fail(
            "No vX.Y.Z git tag reachable. CI checkout must be deep + tagged: "
            "run `git fetch --tags` locally; in the workflow set "
            "`fetch-depth: 0` and `fetch-tags: true` on actions/checkout."
        )
        return  # unreachable (pytest.fail raises); narrows canonical to str for mypy

    text = MIGRATION_MD.read_text(encoding="utf-8")
    # WARNING: this regex is coupled to MIGRATION.md's exact release-status
    # wording. If that sentence changes, update this pattern in lockstep.
    match = _LATEST_RE.search(text)
    assert match is not None, (
        "MIGRATION.md no longer contains a 'latest released version is **X.Y.Z**' "
        "line; update the regex in this test or restore the line."
    )
    documented = match.group(1)

    documented_tuple = parse_version_tuple(documented, on_non_numeric="raise")
    canonical_tuple = parse_version_tuple(canonical, on_non_numeric="raise")

    assert documented_tuple >= canonical_tuple, (
        f"MIGRATION.md claims the latest release is {documented} but a newer git "
        f"tag v{canonical} has shipped. The doc version is stale -- bump the "
        f"'latest released version is **X.Y.Z**' line in docs/MIGRATION.md to {canonical}."
    )


def test_migration_md_version_guard_fails_when_git_tags_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail loudly when tags are unavailable instead of recording a skipped guard."""
    monkeypatch.setattr(sys.modules[__name__], "_version_from_git_tag", lambda _root: None)

    with pytest.raises(pytest.fail.Exception, match=r"No vX\.Y\.Z git tag reachable"):
        test_migration_md_version_does_not_trail_latest_git_tag()
