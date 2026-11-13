"""Behavior tests for the library fencing home (issue #3509).

``hephaestus.prompts.fencing`` owns the untrusted-content notice render,
the ``FencedContent`` record, and the ``fence_content()`` fencer. The
automation prompt layer re-exports these names; these tests pin the
behavior of the library home itself.
"""

from __future__ import annotations

import argparse
import secrets

import pytest

from hephaestus.prompts import PromptCatalog, add_prompt_dir_argument
from hephaestus.prompts.fencing import (
    FencedContent,
    fence_content,
    get_untrusted_notice,
)


def test_fence_content_generates_a_uppercase_hex_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nonce is 16 uppercase hex characters minted by token_hex(8)."""
    calls: list[int] = []

    def fake_token_hex(length: int) -> str:
        calls.append(length)
        return "abc123def456abcd"

    monkeypatch.setattr(secrets, "token_hex", fake_token_hex)

    fenced = fence_content()

    assert calls == [8]
    assert fenced.nonce == "ABC123DEF456ABCD"


def test_fence_content_returns_a_frozen_record() -> None:
    """The fencer record is immutable and carries only the nonce field."""
    fenced = fence_content()

    assert isinstance(fenced, FencedContent)
    with pytest.raises(Exception, match="cannot assign to field"):
        fenced.nonce = "tampered"  # type: ignore[misc]


def test_one_instance_fences_multiple_fields_with_one_nonce() -> None:
    """Two fence() calls on one instance share that instance's nonce."""
    fenced = fence_content()

    first = fenced.fence("FIRST", "one")
    second = fenced.fence("SECOND", "two")

    assert first == f"BEGIN_{fenced.nonce}_FIRST\none\nEND_{fenced.nonce}_FIRST"
    assert second == f"BEGIN_{fenced.nonce}_SECOND\ntwo\nEND_{fenced.nonce}_SECOND"


def test_untrusted_notice_renders_the_packaged_notice() -> None:
    """The notice property equals the packaged default catalog render."""
    PromptCatalog.clear_current()
    fenced = fence_content()

    expected = PromptCatalog().render("shared/untrusted_notice.j2")

    assert fenced.untrusted_notice == expected
    assert get_untrusted_notice() == expected


def test_cli_selected_catalog_changes_the_notice(tmp_path, monkeypatch) -> None:
    """The notice owner reads through PromptCatalog.current(), not a frozen copy."""
    override = tmp_path / "shared" / "untrusted_notice.j2"
    override.parent.mkdir(parents=True)
    override.write_text("HARNESS NOTICE", encoding="utf-8")

    parser = argparse.ArgumentParser()
    add_prompt_dir_argument(parser)
    try:
        parser.parse_args(["--prompt-dir", str(tmp_path)])
        fenced = fence_content()
        assert fenced.untrusted_notice == "HARNESS NOTICE"
        assert get_untrusted_notice() == "HARNESS NOTICE"
    finally:
        PromptCatalog.clear_current()
