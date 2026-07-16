"""Pre-commit benchmark helpers for GitHub Actions integration."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import cast

import yaml

from hephaestus.cli.utils import add_json_arg, add_version_arg, format_output


def load_precommit_config(path: Path) -> list[dict[str, object]]:
    """Load the repository's pre-commit repository entries."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        raise ValueError(f"{path} must define a top-level 'repos' list")
    repos = data["repos"]
    if not all(isinstance(repo, dict) for repo in repos):
        raise ValueError(f"{path} contains an invalid pre-commit repository entry")
    return [dict(cast(dict[str, object], repo)) for repo in repos]


def format_summary_table(elapsed_s: int, file_count: int, hook_status: str) -> str:
    """Format a Markdown table summarising the pre-commit benchmark run.

    Args:
        elapsed_s: Wall-clock seconds the hooks took to complete.
        file_count: Number of files processed.
        hook_status: Result string, e.g. ``"passed"`` or ``"failed"``.

    Returns:
        Markdown-formatted table string including a trailing newline.

    """
    status_icon = "[PASS]" if hook_status == "passed" else "[FAIL]"
    return (
        "## Pre-commit Hook Benchmark\n\n"
        "| Metric | Value |\n"
        "|--------|-------|\n"
        f"| Hook status | {status_icon} {hook_status} |\n"
        f"| Elapsed time | {elapsed_s}s |\n"
        f"| Files processed | {file_count} |\n"
    )


def check_threshold(elapsed_s: int, threshold_s: int = 120) -> bool:
    """Return whether a pre-commit run exceeded its runtime threshold."""
    return elapsed_s > threshold_s


def emit_warning(message: str) -> None:
    """Emit a GitHub Actions warning annotation to stdout."""
    print(f"::warning::{message}")


def write_step_summary(content: str, summary_path: str | None = None) -> None:
    """Append content to the configured GitHub Actions step summary."""
    path = summary_path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as fh:
        fh.write(content)


def bench_precommit_main(argv: list[str] | None = None) -> int:
    """Report pre-commit timing without making performance advisory-only."""
    parser = argparse.ArgumentParser(description="Report pre-commit hook benchmark results.")
    parser.add_argument("--elapsed", type=int, required=True, help="Elapsed time in seconds.")
    parser.add_argument("--files", type=int, default=0, help="Number of files processed.")
    parser.add_argument(
        "--status", default="passed", help='Hook exit status string, e.g. "passed" or "failed".'
    )
    parser.add_argument(
        "--threshold", type=int, default=120, help="Warning threshold in seconds (default: 120)."
    )
    add_json_arg(parser)
    add_version_arg(parser)
    args = parser.parse_args(argv)

    over_threshold = check_threshold(args.elapsed, args.threshold)
    if args.json:
        print(
            format_output(
                {
                    "elapsed_seconds": args.elapsed,
                    "files": args.files,
                    "status": args.status,
                    "threshold_seconds": args.threshold,
                    "over_threshold": over_threshold,
                },
                "json",
            )
        )
        return 0

    table = format_summary_table(args.elapsed, args.files, args.status)
    print(table)
    write_step_summary(table)
    if over_threshold:
        emit_warning(
            f"Pre-commit hooks took {args.elapsed}s, which exceeds "
            f"the {args.threshold}s threshold. "
            "Consider reviewing hook configuration for performance regressions."
        )
    return 0


if __name__ == "__main__":
    sys.exit(bench_precommit_main())
