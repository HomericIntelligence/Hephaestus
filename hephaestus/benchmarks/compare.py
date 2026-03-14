#!/usr/bin/env python3

"""Compare benchmark results against baseline and detect regressions.

Provides utilities for analyzing benchmark results, detecting performance
regressions, and generating formatted reports for CI/CD integration.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Regression:
    """Represents a detected performance regression."""

    benchmark: str
    baseline_ms: float
    current_ms: float
    change_percent: float
    severity: str  # "critical", "high", "medium"


def load_benchmark_results(filepath: Path) -> dict[str, Any]:
    """Load benchmark results from JSON file.

    Args:
        filepath: Path to benchmark results JSON file.

    Returns:
        Parsed benchmark results dictionary.

    Raises:
        FileNotFoundError: If file doesn't exist.
        json.JSONDecodeError: If file is invalid JSON.

    """
    with open(filepath) as f:
        return json.load(f)


def extract_timings(results: dict[str, Any]) -> dict[str, float]:
    """Extract benchmark name to duration mapping.

    Args:
        results: Benchmark results dictionary.

    Returns:
        Dictionary mapping benchmark names to durations in ms.

    """
    timings = {}

    benchmarks = results.get("benchmarks", [])
    for bench in benchmarks:
        name = bench.get("name")
        duration = bench.get("duration_ms")

        if name and duration is not None:
            timings[name] = float(duration)

    return timings


def detect_regressions(
    current: dict[str, float],
    baseline: dict[str, float],
    critical_threshold: float = 25.0,
    high_threshold: float = 10.0,
    medium_threshold: float = 5.0,
) -> tuple[list[Regression], list[dict[str, Any]]]:
    """Detect performance regressions by comparing current to baseline.

    Args:
        current: Current benchmark timings (name -> ms).
        baseline: Baseline benchmark timings (name -> ms).
        critical_threshold: Threshold for critical regression (%).
        high_threshold: Threshold for high severity regression (%).
        medium_threshold: Threshold for medium severity regression (%).

    Returns:
        Tuple of (regressions list, improvements list).

    """
    regressions = []
    improvements = []

    for name, current_ms in current.items():
        baseline_ms = baseline.get(name)

        if baseline_ms is None:
            continue  # New benchmark, no comparison

        if baseline_ms == 0:
            continue  # Avoid division by zero

        # Calculate percentage change (positive = slower = regression)
        change_percent = ((current_ms - baseline_ms) / baseline_ms) * 100

        if change_percent > critical_threshold:
            regressions.append(
                Regression(
                    benchmark=name,
                    baseline_ms=baseline_ms,
                    current_ms=current_ms,
                    change_percent=change_percent,
                    severity="critical",
                )
            )
        elif change_percent > high_threshold:
            regressions.append(
                Regression(
                    benchmark=name,
                    baseline_ms=baseline_ms,
                    current_ms=current_ms,
                    change_percent=change_percent,
                    severity="high",
                )
            )
        elif change_percent > medium_threshold:
            regressions.append(
                Regression(
                    benchmark=name,
                    baseline_ms=baseline_ms,
                    current_ms=current_ms,
                    change_percent=change_percent,
                    severity="medium",
                )
            )
        elif change_percent < -medium_threshold:
            # Improvement (negative change = faster)
            improvements.append(
                {
                    "benchmark": name,
                    "baseline_ms": baseline_ms,
                    "current_ms": current_ms,
                    "improvement_percent": abs(change_percent),
                }
            )

    return regressions, improvements


def format_markdown_report(
    regressions: list[Regression],
    improvements: list[dict[str, Any]],
    current_results: dict[str, Any],
    baseline_results: dict[str, Any],
) -> str:
    """Generate markdown report of regression analysis.

    Args:
        regressions: List of detected regressions.
        improvements: List of detected improvements.
        current_results: Full current benchmark results.
        baseline_results: Full baseline benchmark results.

    Returns:
        Markdown formatted report string.

    """
    lines = []
    lines.append("# Performance Regression Report")
    lines.append("")

    # Summary
    critical_count = sum(1 for r in regressions if r.severity == "critical")
    high_count = sum(1 for r in regressions if r.severity == "high")
    medium_count = sum(1 for r in regressions if r.severity == "medium")

    lines.append("## Summary")
    lines.append("")

    if critical_count > 0:
        lines.append(f"**{critical_count} CRITICAL regressions detected** (>25% slower)")
    if high_count > 0:
        lines.append(f"**{high_count} high severity regressions** (10-25% slower)")
    if medium_count > 0:
        lines.append(f"**{medium_count} medium severity regressions** (5-10% slower)")
    if len(improvements) > 0:
        lines.append(f"**{len(improvements)} improvements detected** (>5% faster)")
    if not regressions and not improvements:
        lines.append("No significant performance changes detected.")

    lines.append("")

    # Regressions table
    if regressions:
        lines.append("## Regressions")
        lines.append("")
        lines.append("| Benchmark | Baseline (ms) | Current (ms) | Change | Severity |")
        lines.append("|-----------|---------------|--------------|--------|----------|")

        for reg in sorted(regressions, key=lambda r: r.change_percent, reverse=True):
            severity_emoji = {
                "critical": "🔴",
                "high": "🟠",
                "medium": "🟡",
            }.get(reg.severity, "")

            lines.append(
                f"| {reg.benchmark} | {reg.baseline_ms:.2f} | {reg.current_ms:.2f} | "
                f"+{reg.change_percent:.1f}% | {severity_emoji} {reg.severity} |"
            )

        lines.append("")

    # Improvements table
    if improvements:
        lines.append("## Improvements")
        lines.append("")
        lines.append("| Benchmark | Baseline (ms) | Current (ms) | Improvement |")
        lines.append("|-----------|---------------|--------------|-------------|")

        for imp in sorted(improvements, key=lambda i: i["improvement_percent"], reverse=True):
            lines.append(
                f"| {imp['benchmark']} | {imp['baseline_ms']:.2f} | {imp['current_ms']:.2f} | "
                f"-{imp['improvement_percent']:.1f}% 🟢 |"
            )

        lines.append("")

    # Environment info - generalized from Mojo-specific
    lines.append("## Environment")
    lines.append("")
    env = current_results.get("environment", {})
    lines.append(f"- **OS**: {env.get('os', 'unknown')}")
    lines.append(f"- **CPU**: {env.get('cpu', 'unknown')}")
    # Changed from mojo_version to runtime_version for generalization
    lines.append(f"- **Runtime**: {env.get('runtime_version', env.get('python_version', 'unknown'))}")
    lines.append(f"- **Commit**: {env.get('git_commit', 'unknown')}")
    lines.append("")

    return "\n".join(lines)
