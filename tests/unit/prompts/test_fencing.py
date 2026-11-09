"""Behavior tests for the library fencing home (issue #3509).

``hephaestus.prompts.fencing`` owns the fence primitive, the
untrusted-content notice render, and the ``FencedContent``/``fence_content``
helpers. The automation ``_shared`` module re-exports them.
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hephaestus.prompts import PromptCatalog
from hephaestus.prompts.catalog import _ACTIVE_CATALOG
from hephaestus.prompts.fencing import (
    FencedContent,
    fence_content,
    fence_untrusted,
    get_untrusted_notice,
)

_NONCE_PATTERN = re.compile(r"^[0-9A-F]{16}$")


def test_fence_content_nonce_is_uppercase_hex() -> None:
    """The nonce is 16 uppercase hex characters from secrets.token_hex(8)."""
    fenced = fence_content()

    assert _NONCE_PATTERN.match(fenced.nonce)


def test_fence_reuses_one_nonce_across_fields() -> None:
    """Two fence calls on one instance share the instance nonce."""
    fenced = fence_content()

    first = fenced.fence("ISSUE_BODY", "payload")
    second = fenced.fence("DIFF", "other")

    assert first == f"BEGIN_{fenced.nonce}_ISSUE_BODY\npayload\nEND_{fenced.nonce}_ISSUE_BODY"
    assert second == f"BEGIN_{fenced.nonce}_DIFF\nother\nEND_{fenced.nonce}_DIFF"


def test_untrusted_notice_matches_packaged_catalog_render() -> None:
    """The notice equals the packaged default catalog render of the template."""
    fenced = fence_content()

    expected = PromptCatalog().render("shared/untrusted_notice.j2")

    assert fenced.untrusted_notice == expected
    assert get_untrusted_notice() == expected


def test_untrusted_notice_follows_active_catalog(tmp_path: Path) -> None:
    """The notice owner reads through PromptCatalog.current(), not a fixed catalog."""
    override = tmp_path / "overrides"
    (override / "shared").mkdir(parents=True)
    (override / "shared" / "untrusted_notice.j2").write_text("OVERRIDE NOTICE", encoding="utf-8")

    token = _ACTIVE_CATALOG.set(PromptCatalog(override_root=override))
    try:
        assert get_untrusted_notice() == "OVERRIDE NOTICE"
        assert fence_content().untrusted_notice == "OVERRIDE NOTICE"
    finally:
        _ACTIVE_CATALOG.reset(token)


def test_fence_untrusted_marker_shape() -> None:
    """The primitive keeps the BEGIN/END marker contract."""
    assert fence_untrusted("LABEL", "body", "NONCE123") == (
        "BEGIN_NONCE123_LABEL\nbody\nEND_NONCE123_LABEL"
    )


def test_fenced_content_is_frozen() -> None:
    """The record does not allow a nonce change after construction."""
    fenced = FencedContent(nonce="ABC")

    with pytest.raises(FrozenInstanceError):
        fenced.nonce = "DEF"  # type: ignore[misc]
