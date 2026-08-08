"""Regression checks for canonical contributor-template links (#2136)."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_REPOSITORY = "https://github.com/HomericIntelligence/Hephaestus"
LEGACY_REPOSITORY = "https://github.com/mvillmow/Hephaestus"

TEMPLATE_LINKS = (
    (".github/ISSUE_TEMPLATE/config.yml", "blob/main/README.md"),
    (
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        "blob/main/docs/auto-label-needs-plan.md",
    ),
    (
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        "blob/main/docs/auto-label-needs-plan.md",
    ),
    (".github/pull_request_template.md", "blob/main/docs/DEFINITION_OF_DONE.md"),
)


@pytest.mark.parametrize(("relative_path", "target"), TEMPLATE_LINKS)
def test_contributor_template_links_use_canonical_repository(
    relative_path: str,
    target: str,
) -> None:
    """Each contributor link targets its canonical repository destination."""
    content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    expected = f"{CANONICAL_REPOSITORY}/{target}"
    assert expected in content, f"{relative_path} must contain {expected}"


@pytest.mark.parametrize("relative_path", [path for path, _target in TEMPLATE_LINKS])
def test_contributor_templates_reject_legacy_repository(relative_path: str) -> None:
    """Contributor templates must not restore the former repository owner."""
    content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert LEGACY_REPOSITORY not in content, (
        f"{relative_path} contains legacy repository URL {LEGACY_REPOSITORY}"
    )
