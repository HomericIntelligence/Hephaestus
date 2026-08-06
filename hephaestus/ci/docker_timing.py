"""Docker build cache efficiency utilities.

Provides helper functions for measuring Docker build cache hit rates and
rendering GitHub Actions step summary tables.

No Docker daemon is required to import this module — all functions operate on
plain text (build log strings) or pure arithmetic.

Usage::

    from hephaestus.ci.docker_timing import (
        build_summary_table,
        compute_reduction,
        count_cached_layers,
    )

    cached = count_cached_layers(build_log)
    reduction = compute_reduction(cold_seconds, warm_seconds)
    table = build_summary_table(cold_seconds, warm_seconds, cached, reduction)
"""

from __future__ import annotations

import math


def _validate_durations(cold_seconds: int, warm_seconds: int) -> None:
    """Validate the duration domain used by Docker timing metrics."""
    if isinstance(cold_seconds, bool) or not isinstance(cold_seconds, int) or cold_seconds <= 0:
        raise ValueError("cold_seconds must be a positive integer")
    if isinstance(warm_seconds, bool) or not isinstance(warm_seconds, int) or warm_seconds < 0:
        raise ValueError("warm_seconds must be a non-negative integer")


def count_cached_layers(build_log: str) -> int:
    """Count the number of CACHED layer lines in a docker build --progress=plain log.

    BuildKit emits ``#N CACHED`` lines for each layer restored from the local
    layer cache.  This count indicates how effective the cache was for a build.

    Args:
        build_log: Full stdout+stderr from ``docker build --progress=plain``.

    Returns:
        Number of lines containing the word ``CACHED`` (case-insensitive).

    """
    return build_log.upper().count("CACHED")


def compute_reduction(cold_seconds: int, warm_seconds: int) -> float:
    """Compute the percentage reduction in build time from cold to warm build.

    Args:
        cold_seconds: Wall-clock seconds for the cold (no-cache) build.
        warm_seconds: Wall-clock seconds for the warm (source-change) rebuild.

    Returns:
        Percentage reduction, rounded to one decimal place.

    Raises:
        ValueError: If ``cold_seconds`` is not positive or ``warm_seconds`` is
            negative.

    """
    _validate_durations(cold_seconds, warm_seconds)
    reduction = (cold_seconds - warm_seconds) / cold_seconds * 100
    return round(reduction, 1)


def build_summary_table(
    cold_seconds: int,
    warm_seconds: int,
    cached_layers: int,
    reduction: float,
    acceptance_threshold: float = 30.0,
) -> str:
    """Render a Markdown table summarising the before/after build timing.

    The table is written to ``$GITHUB_STEP_SUMMARY`` by the CI report step so
    it appears as a structured summary in the GitHub Actions UI.

    Args:
        cold_seconds: Wall-clock seconds for the cold build.
        warm_seconds: Wall-clock seconds for the warm rebuild.
        cached_layers: Number of ``CACHED`` layers in the warm build log.
        reduction: Percentage time reduction (from :func:`compute_reduction`).
        acceptance_threshold: Minimum reduction % to pass (default 30).

    Returns:
        Markdown string containing the full summary table.

    Raises:
        ValueError: If a duration, cached-layer count, reduction, or threshold
            is outside its documented metric domain.

    """
    _validate_durations(cold_seconds, warm_seconds)
    if isinstance(cached_layers, bool) or not isinstance(cached_layers, int) or cached_layers < 0:
        raise ValueError("cached_layers must be a non-negative integer")
    if (
        isinstance(reduction, bool)
        or not isinstance(reduction, (int, float))
        or not math.isfinite(reduction)
        or reduction > 100
    ):
        raise ValueError("reduction must be a finite number no greater than 100")
    if (
        isinstance(acceptance_threshold, bool)
        or not isinstance(acceptance_threshold, (int, float))
        or not math.isfinite(acceptance_threshold)
        or not 0 <= acceptance_threshold <= 100
    ):
        raise ValueError("acceptance_threshold must be a finite percentage in 0..100")

    verdict = "PASS" if reduction >= acceptance_threshold else "FAIL"
    return (
        "## Docker Build Timing: Source-Only Change Cache Efficiency\n\n"
        "| Metric | Value |\n"
        "|--------|-------|\n"
        f"| Cold build (no cache) | {cold_seconds}s |\n"
        f"| Warm rebuild (source change only) | {warm_seconds}s |\n"
        f"| Reduction | {reduction}% |\n"
        f"| Cached layers (warm build) | {cached_layers} |\n"
        f"| Acceptance criterion (≥{acceptance_threshold:.0f}%) | {verdict} |\n"
    )
