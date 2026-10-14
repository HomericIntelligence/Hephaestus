"""Behavioral checks for the published privacy policy artifact."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from hephaestus.validation.markdown import extract_markdown_links, validate_file_links

REPO_ROOT = Path(__file__).resolve().parents[3]
PRIVACY = REPO_ROOT / "PRIVACY.md"


def _resolved_local_targets(source: Path) -> set[Path]:
    """Return local Markdown targets resolved relative to ``source``."""
    return {
        (source.parent / unquote(urlparse(target).path)).resolve()
        for target, _line in extract_markdown_links(source.read_text(encoding="utf-8"))
        if not urlparse(target).scheme and urlparse(target).path
    }


def test_privacy_policy_is_a_valid_published_artifact() -> None:
    """The published policy exists and has no broken Markdown links."""
    assert PRIVACY.is_file()
    assert validate_file_links(PRIVACY, REPO_ROOT)["broken_links"] == []


@pytest.mark.parametrize("source", [REPO_ROOT / "SECURITY.md", REPO_ROOT / "docs/index.md"])
def test_privacy_policy_is_reachable_from_public_indexes(source: Path) -> None:
    """Public policy indexes link to the published privacy artifact."""
    assert PRIVACY.resolve() in _resolved_local_targets(source)
