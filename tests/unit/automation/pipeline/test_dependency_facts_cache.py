"""Bounded TTL cache for the dependency facts batch (#3625)."""

from __future__ import annotations

import pytest

from hephaestus.automation.pipeline import admission


class _Bar:
    def __init__(self, tag: str) -> None:
        self.tag = tag


class _Reader:
    def __init__(self) -> None:
        self.calls = 0

    def batch_dependency_facts(self, canonical, *, deadline_s, shutdown):
        self.calls += 1
        return tuple(f"fact-{n}" for n in canonical)


def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission, "_dependency_facts_cache", {})


def test_facts_batch_reads_once_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second poll inside the TTL hits the cache."""
    _fresh_cache(monkeypatch)
    reader = _Reader()
    first = admission._dependency_facts_batch(
        reader, (1, 2), deadline_s=1.0, shutdown=None, clock=lambda: 1000.0
    )
    second = admission._dependency_facts_batch(
        reader, (1, 2), deadline_s=1.0, shutdown=None, clock=lambda: 1030.0
    )
    assert first is second
    assert reader.calls == 1


def test_facts_batch_rereads_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A poll after the TTL expiry re-reads the batch."""
    _fresh_cache(monkeypatch)
    reader = _Reader()
    admission._dependency_facts_batch(
        reader, (1, 2), deadline_s=1.0, shutdown=None, clock=lambda: 1000.0
    )
    admission._dependency_facts_batch(
        reader, (1, 2), deadline_s=1.0, shutdown=None, clock=lambda: 1101.0
    )
    assert reader.calls == 2


def test_facts_batch_different_dependencies_do_not_collide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache keys are the whole canonical tuple."""
    _fresh_cache(monkeypatch)
    reader = _Reader()
    a = admission._dependency_facts_batch(
        reader, (1,), deadline_s=1.0, shutdown=None, clock=lambda: 1000.0
    )
    b = admission._dependency_facts_batch(
        reader, (2,), deadline_s=1.0, shutdown=None, clock=lambda: 1000.0
    )
    assert a != b
    assert reader.calls == 2


def test_facts_batch_malformed_facts_are_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed batch is never pinned into the cache."""

    class _Broken:
        def __init__(self) -> None:
            self.calls = 0

        def batch_dependency_facts(self, canonical, *, deadline_s, shutdown):
            self.calls += 1
            return "junk"

    _fresh_cache(monkeypatch)
    reader = _Broken()
    admission._dependency_facts_batch(
        reader, (1, 2), deadline_s=1.0, shutdown=None, clock=lambda: 1000.0
    )
    admission._dependency_facts_batch(
        reader, (1, 2), deadline_s=1.0, shutdown=None, clock=lambda: 1000.0
    )
    assert reader.calls == 2
