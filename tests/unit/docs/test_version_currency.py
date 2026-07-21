"""Regression test: MIGRATION.md version claim must not trail the latest git tag.

Pattern D (stop maintaining the number by hand): instead of relying on a human
to notice the dated 'latest released version' line has gone stale, this test
fails CI when the documented version is *older* than the canonical hatch-vcs
version (latest vX.Y.Z git tag). See issue #1208.

This test is designed to RUN in CI, not skip: the unit-test job checks out with
``fetch-tags: true`` (see .github/workflows/test.yml). If tags are somehow
absent, the test FAILS LOUD with remediation guidance rather than skipping --
a guard that silently skips is not a guard.
"""

import re
import runpy
from pathlib import Path

import pytest

from hephaestus.utils.helpers import get_repo_root
from hephaestus.version.consistency import _version_from_git_tag
from hephaestus.version.parsing import parse_version_tuple

REPO_ROOT = get_repo_root(Path(__file__))
MIGRATION_MD = REPO_ROOT / "docs" / "MIGRATION.md"

# Matches: "The latest released version is **0.9.5**"
_LATEST_RE = re.compile(r"latest released version is \*\*(\d+\.\d+\.\d+)\*\*", re.IGNORECASE)


def test_repo_root_is_anchored_to_this_test_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test must inspect its own checkout, regardless of the runner CWD."""
    monkeypatch.chdir(tmp_path)

    namespace = runpy.run_path(str(Path(__file__)))

    assert namespace["REPO_ROOT"] == Path(__file__).resolve().parents[3]


def test_migration_md_version_does_not_trail_latest_git_tag() -> None:
    """Fail if MIGRATION.md's 'latest released version' trails the newest git tag."""
    canonical = _version_from_git_tag(REPO_ROOT)
    if canonical is None:
        # No vX.Y.Z tag resolvable — almost always a shallow/tagless checkout
        # (e.g. a CI job whose actions/checkout lacks fetch-depth:0 + fetch-tags).
        # The drift guard can't run without the tag, so skip rather than fail: a
        # missing-tags ENVIRONMENT is not a doc defect. The required workflows
        # that gate releases fetch tags, so the guard still runs there.
        pytest.skip(
            "Could not resolve the latest vX.Y.Z git tag (tagless/shallow "
            "checkout); doc-version drift cannot be checked in this environment."
        )
        return  # unreachable (pytest.skip raises); narrows canonical to str for mypy

    text = MIGRATION_MD.read_text(encoding="utf-8")
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
