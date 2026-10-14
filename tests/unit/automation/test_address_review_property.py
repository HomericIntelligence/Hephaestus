"""Property-based (Hypothesis) fuzz tests for the pipeline address JSON parser.

Covers issue #1470 — ``_parse_addressed_block`` consumes free-form LLM output
and must never raise; on malformed/absent JSON it returns the documented
default shape.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, strategies as st

from hephaestus.automation.address_review_core import (
    _ADDRESS_PARSE_DEFAULT,
    _parse_addressed_block,
    parse_addressed_replies,
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

    def test_validates_and_normalizes_all_thread_replies(self) -> None:
        result = parse_addressed_replies(
            {
                "addressed": ["thread-a", "thread-b"],
                "replies": {"thread-a": " yes ", "thread-b": "done"},
            },
            [{"thread_id": "thread-a"}, {"id": "thread-b"}],
        )

        assert result == {"thread-a": "yes", "thread-b": "done"}

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {"addressed": "thread-a", "replies": {}},
            {"addressed": ["thread-a"], "replies": {"thread-b": "reply"}},
            {"addressed": ["thread-a"], "replies": {"thread-a": ""}},
        ],
    )
    def test_rejects_incomplete_or_invalid_replies(self, payload: object) -> None:
        assert parse_addressed_replies(payload, [{"thread_id": "thread-a"}]) is None
