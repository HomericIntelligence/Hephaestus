"""Property-based (Hypothesis) fuzz tests for the address-review JSON parser.

Covers issue #1470 — ``_parse_addressed_block`` consumes free-form LLM output
and must never raise; on malformed/absent JSON it returns the documented
default shape. See hephaestus/automation/address_review.py:81.
"""

from __future__ import annotations

import json

from hypothesis import given, strategies as st

from hephaestus.automation.address_review import (
    _ADDRESS_PARSE_DEFAULT,
    _parse_addressed_block,
)


class TestParseAddressedBlockProperties:
    """Property-based fuzz coverage for _parse_addressed_block (#1470)."""

    @given(st.text())
    def test_never_raises_and_returns_dict(self, text: str) -> None:
        assert isinstance(_parse_addressed_block(text), dict)

    @given(st.text())
    def test_non_json_input_returns_default_shape(self, text: str) -> None:
        # Inputs with no ```json fence resolve to the documented default.
        if "```json" not in text:
            assert _parse_addressed_block(text) == _ADDRESS_PARSE_DEFAULT

    @given(st.text())
    def test_malformed_json_fence_does_not_raise(self, junk: str) -> None:
        body = f"prefix\n```json\n{junk}\n```\nsuffix"
        assert isinstance(_parse_addressed_block(body), dict)

    @given(
        st.dictionaries(st.text(), st.text(), max_size=5),
        st.dictionaries(st.text(), st.text(), max_size=5),
    )
    def test_wellformed_json_block_is_parsed(
        self, addressed: dict[str, str], replies: dict[str, str]
    ) -> None:
        payload = {"addressed": list(addressed), "replies": replies}
        body = f"```json\n{json.dumps(payload)}\n```"
        assert _parse_addressed_block(body)["replies"] == replies
