#!/usr/bin/env python3
"""Regression tests for documented deprecations.

Every symbol that ``COMPATIBILITY.md`` lists as "deprecated" MUST continue to
emit a ``DeprecationWarning`` when used. If a deprecation is removed (vs. just
upgraded to a hard error), update the COMPATIBILITY.md deprecation section and
remove the corresponding row here in the same PR.

This file is the single canonical regression guard against accidentally
silencing a documented deprecation warning during refactors.
"""

import warnings

import pytest


class TestDocumentedDeprecations:
    """Each test corresponds to a row in COMPATIBILITY.md's deprecation section."""

    def test_retry_with_jitter_emits_deprecation_warning(self) -> None:
        """`retry_with_jitter` was deprecated in favor of `retry_with_backoff(jitter=True)`.

        Documented in COMPATIBILITY.md under "hephaestus.utils → Deprecated
        lazy-loaded symbols" as scheduled for removal no earlier than the
        next major version after 1.0.
        """
        from hephaestus.utils import retry_with_jitter

        def noop() -> int:
            return 42

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = retry_with_jitter(noop, max_retries=1)

        assert result == 42, "deprecated wrapper must still execute the wrapped function"
        deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecation_warnings, (
            "retry_with_jitter must emit DeprecationWarning. If you intentionally "
            "removed the deprecation, also update COMPATIBILITY.md and delete "
            "this test in the same PR."
        )
        msg = str(deprecation_warnings[0].message)
        assert "retry_with_jitter" in msg, (
            f"DeprecationWarning message should name the deprecated symbol; got: {msg!r}"
        )

    def test_retry_with_jitter_reachable_from_top_level_lazy_loader(self) -> None:
        """`hephaestus.retry_with_jitter` must still resolve via PEP 562 lazy loading.

        Removing the symbol from `hephaestus/__init__.py`'s lazy-loader map
        without first amending COMPATIBILITY.md's deprecation policy would
        break downstream users mid-deprecation-window.
        """
        import hephaestus

        # Trip the lazy loader. We expect a successful attribute lookup
        # (whether or not it also emits a DeprecationWarning on access — only
        # CALLING the symbol triggers the warning in the current
        # implementation; the lazy resolve itself is silent).
        symbol = hephaestus.retry_with_jitter
        assert callable(symbol), (
            "hephaestus.retry_with_jitter must remain importable via the lazy "
            "loader until its deprecation window closes (post-2.0)."
        )


class TestCompatibilityDocReferences:
    """Light cross-check: COMPATIBILITY.md still names the deprecated symbols above."""

    def test_compatibility_doc_mentions_retry_with_jitter(self) -> None:
        """Cross-check that COMPATIBILITY.md still names the deprecated symbol.

        If COMPATIBILITY.md no longer mentions `retry_with_jitter`, either
        the deprecation has been removed (then drop the test in
        TestDocumentedDeprecations) or the doc has drifted (fix the doc).
        Either way, this test catches the discrepancy.
        """
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        compat = (repo_root / "COMPATIBILITY.md").read_text(encoding="utf-8")
        if "retry_with_jitter" not in compat:
            pytest.fail(
                "COMPATIBILITY.md no longer mentions `retry_with_jitter`, but "
                "`tests/unit/test_deprecation_warnings.py` still asserts the "
                "deprecation. Drop the corresponding tests when removing a "
                "documented deprecation."
            )
