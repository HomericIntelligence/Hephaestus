"""Behavior tests for the review JSON block reader."""

from __future__ import annotations

import pytest

from hephaestus.automation.review_json_blocks import json_block_bodies, json_block_spans


def test_reads_a_fence_with_the_object_on_the_fence_line() -> None:
    """A model that writes the fence and the object on one line is read."""
    text = '```json {"verdict": "GO"} ```'

    bodies = json_block_bodies(text)

    assert bodies == ['{"verdict": "GO"}']


def test_reads_a_fence_with_the_object_on_its_own_line() -> None:
    """A model that writes the fence and the object on separate lines is read."""
    text = '```json\n{"verdict": "GO"}\n```'

    bodies = json_block_bodies(text)

    assert bodies == ['{"verdict": "GO"}']


def test_reads_every_object_in_document_order() -> None:
    """Every object is read, whatever fence layout it used."""
    text = '```json\n{"first": 1}\n```\n```json {"second": 2} ```\n'

    bodies = json_block_bodies(text)

    assert bodies == ['{"first": 1}', '{"second": 2}']


def test_reports_the_span_of_each_object() -> None:
    """The span covers the fence and the object."""
    text = 'before\n```json\n{"a": 1}\n```\nafter'

    spans = json_block_spans(text)

    assert len(spans) == 1
    start, end, body = spans[0]
    assert text[start:end] == '```json\n{"a": 1}\n```'
    assert body == '{"a": 1}'


@pytest.mark.parametrize("text", ["", "no json here", "```json\nnot closed"])
def test_reads_nothing_without_a_fenced_object(text: str) -> None:
    """Text without a fenced object reads as no object."""
    assert json_block_bodies(text) == []
