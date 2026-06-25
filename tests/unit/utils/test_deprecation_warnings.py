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

    def test_retry_with_jitter_access_emits_deprecation_warning(self) -> None:
        """`hephaestus.retry_with_jitter` must warn at ACCESS time via the lazy loader.

        Regression for #1545: the deprecated shim is exposed at the top-level
        package surface, so binding the name (not only calling it) must signal
        the deprecation. Removing the symbol from the lazy-loader map without
        first amending COMPATIBILITY.md would break downstream users mid-window.
        """
        import hephaestus

        # __getattr__ caches resolved names into module globals (PEP 562), so a
        # prior access would skip the lazy path. Force a fresh resolve.
        hephaestus.__dict__.pop("retry_with_jitter", None)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            symbol = hephaestus.retry_with_jitter

        assert callable(symbol), (
            "hephaestus.retry_with_jitter must remain importable via the lazy "
            "loader until its deprecation window closes (post-2.0)."
        )
        access_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert access_warnings, (
            "Accessing hephaestus.retry_with_jitter must emit a DeprecationWarning "
            "(issue #1545). If you intentionally removed it, update COMPATIBILITY.md "
            "and this test in the same PR."
        )
        assert "retry_with_jitter" in str(access_warnings[0].message)
        # stacklevel=2 must point the warning at THIS test's access line, not at
        # hephaestus/__init__.py (a wrong stacklevel would otherwise pass silently).
        assert access_warnings[0].filename == __file__, (
            f"DeprecationWarning should be attributed to the caller's file "
            f"({__file__}); got {access_warnings[0].filename}. Check stacklevel."
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

        # Test file lives at tests/unit/utils/test_deprecation_warnings.py;
        # parents[0] is tests/unit/utils, [1] tests/unit, [2] tests, [3] repo root.
        repo_root = Path(__file__).resolve().parents[3]
        compat = (repo_root / "COMPATIBILITY.md").read_text(encoding="utf-8")
        if "retry_with_jitter" not in compat:
            pytest.fail(
                "COMPATIBILITY.md no longer mentions `retry_with_jitter`, but "
                "`tests/unit/utils/test_deprecation_warnings.py` still asserts "
                "the deprecation. Drop the corresponding tests when removing "
                "a documented deprecation."
            )
