"""Tests for hephaestus.version.parsing shared version-tuple core.

These tests pin each supported non-numeric-segment policy.  The strict mode is
also checked against the active version-consistency call site.
"""

from __future__ import annotations

import pytest

from hephaestus.version.parsing import parse_version_tuple


class TestDropMode:
    """``on_non_numeric='drop'`` skips non-numeric segments."""

    def _parse(self, v: str) -> tuple[int, ...]:
        return parse_version_tuple(v, split_pattern=r"[.\-]", on_non_numeric="drop")

    def test_simple_version(self) -> None:
        assert self._parse("1.2.3") == (1, 2, 3)

    def test_single_component(self) -> None:
        assert self._parse("2") == (2,)

    def test_two_components(self) -> None:
        assert self._parse("1.0") == (1, 0)

    def test_splits_on_dash(self) -> None:
        assert self._parse("1.2-3") == (1, 2, 3)

    def test_drops_non_digit_segment(self) -> None:
        assert self._parse("1.a.3") == (1, 3)

    def test_four_components(self) -> None:
        assert self._parse("1.2.3.4") == (1, 2, 3, 4)

    def test_prerelease_suffix_dropped(self) -> None:
        # "3rc1" is not all-digits -> dropped entirely
        assert self._parse("1.2.3rc1") == (1, 2)

    def test_dev_suffix_segment_kept(self) -> None:
        # "0" segments survive; "dev1" dropped
        assert self._parse("2.0.0.dev1") == (2, 0, 0)

    def test_empty_string(self) -> None:
        assert self._parse("") == ()

    def test_all_non_numeric(self) -> None:
        assert self._parse("a.b.c") == ()


class TestZeroMode:
    """``on_non_numeric='zero'`` coerces non-numeric segments."""

    def _parse(self, v: str) -> tuple[int, ...]:
        return parse_version_tuple(v, on_non_numeric="zero")

    def test_simple_version(self) -> None:
        assert self._parse("1.19.1") == (1, 19, 1)

    def test_non_integer_segment_becomes_zero(self) -> None:
        assert self._parse("1.2.rc1") == (1, 2, 0)

    def test_does_not_split_on_dash(self) -> None:
        # No dash splitting -> "2-3" is one non-int segment -> 0
        assert self._parse("1.2-3") == (1, 0)

    def test_empty_segment_becomes_zero(self) -> None:
        assert self._parse("1..2") == (1, 0, 2)

    def test_empty_string(self) -> None:
        assert self._parse("") == (0,)


class TestRaiseMode:
    """``on_non_numeric='raise'`` — consistency semantics (strict, split on '.')."""

    def _parse(self, v: str) -> tuple[int, ...]:
        return parse_version_tuple(v, on_non_numeric="raise")

    def test_simple_version(self) -> None:
        assert self._parse("1.2.3") == (1, 2, 3)

    def test_two_components(self) -> None:
        assert self._parse("1.2") == (1, 2)

    def test_raises_on_non_numeric(self) -> None:
        with pytest.raises(ValueError):
            self._parse("1.a.3")

    def test_raises_on_prerelease(self) -> None:
        with pytest.raises(ValueError):
            self._parse("1.2.3rc1")

    def test_raises_on_empty_string(self) -> None:
        with pytest.raises(ValueError):
            self._parse("")


class TestActiveDelegationParity:
    """The active call site must equal the shared core for the same options."""

    def test_consistency_delegates(self) -> None:
        from hephaestus.version.consistency import _parse_version_tuple

        for v in ("1.2.3", "0.0.1", "10.20.30", "1.2"):
            assert _parse_version_tuple(v) == parse_version_tuple(v, on_non_numeric="raise")
