#!/usr/bin/env python3

"""
Compare benchmark results against baseline and detect regressions.

Provides utilities for analyzing benchmark results, detecting performance
regressions, and generating formatted reports for CI/CD integration.

Usage:
    python scripts/compare_benchmarks.py current.json baseline.json
    python scripts/compare_benchmarks.py current.json baseline.json --threshold 10
    python scripts/compare_benchmarks.py current.json baseline.json --output report.md
"""

import argparse
import json
import sys
from pathlib import Path

from hephaestus.benchmarks.compare import (
    load_benchmark_results,
    extract_timings,
    detect_regressions,
    format_markdown_report,
)


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Compare benchmark results and detect regressions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "current",
        type=Path,
        help="Current benchmark results JSON file",
    )
    parser.add_argument(
        "baseline",
        type=Path,
        help="Baseline benchmark results JSON file",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=10.0,
        help="Regression threshold percentage (default: 10)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output markdown report file",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        default=True,
        help="Exit with error code if critical regressions detected",
    )

    args = parser.parse_args()

    # Load results
    try:
        current_results = load_benchmark_results(args.current)
    except FileNotFoundError:
        print(f"Error: Current results file not found: {args.current}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in current results: {e}", file=sys.stderr)
        return 1

    try:
        baseline_results = load_benchmark_results(args.baseline)
    except FileNotFoundError:
        print(f"Warning: Baseline not found: {args.baseline}", file=sys.stderr)
        print("No regression comparison possible without baseline.", file=sys.stderr)
        return 0
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in baseline: {e}", file=sys.stderr)
        return 1

    # Extract timings
    current_timings = extract_timings(current_results)
    baseline_timings = extract_timings(baseline_results)

    if not current_timings:
        print("Warning: No timing data in current results", file=sys.stderr)
        return 0

    if not baseline_timings:
        print("Warning: No timing data in baseline", file=sys.stderr)
        return 0

    # Detect regressions
    regressions, improvements = detect_regressions(
        current_timings,
        baseline_timings,
        critical_threshold=25.0,
        high_threshold=args.threshold,
        medium_threshold=5.0,
    )

    # Generate report
    report = format_markdown_report(
        regressions, improvements, current_results, baseline_results
    )

    # Output report
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report)
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)

    # Summary to stderr
    critical_count = sum(1 for r in regressions if r.severity == "critical")
    high_count = sum(1 for r in regressions if r.severity == "high")
    medium_count = sum(1 for r in regressions if r.severity == "medium")

    print(f"\nRegressions: {critical_count} critical, {high_count} high, {medium_count} medium", file=sys.stderr)
    print(f"Improvements: {len(improvements)}", file=sys.stderr)

    # Exit with error if critical regressions and flag is set
    if args.fail_on_regression and critical_count > 0:
        print(f"\n❌ FAILED: {critical_count} critical regressions detected", file=sys.stderr)
        return 1

    print("\n✅ No critical regressions detected", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
