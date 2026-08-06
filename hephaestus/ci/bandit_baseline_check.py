"""Compare a Bandit LOW-severity JSON report against the checked-in baseline."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from hephaestus.io import safe_write


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting duplicate field names."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json_object(path: Path) -> dict[str, Any]:
    """Load one JSON object, rejecting malformed and duplicate-key data."""
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a top-level JSON object")
    return value


def count_by_test_id(report: dict[str, Any]) -> dict[str, int]:
    """Return LOW-severity finding counts from a validated Bandit report.

    Bandit's ``--severity-level low`` flag is a minimum threshold, so the
    report can also contain MEDIUM/HIGH findings. Those are filtered out here
    so they are never compared against the LOW-only baseline.

    Raises:
        ValueError: If the report or any finding has an invalid structure.

    """
    errors = report.get("errors")
    if not isinstance(errors, list):
        raise ValueError("Bandit report must define an 'errors' list")
    if errors:
        raise ValueError("Bandit report contains scan errors")

    results = report.get("results")
    if not isinstance(results, list):
        raise ValueError("Bandit report must define a 'results' list")

    low_test_ids: list[str] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError(f"Bandit result {index} must be an object")
        test_id = result.get("test_id")
        severity = result.get("issue_severity")
        if not isinstance(test_id, str) or not test_id:
            raise ValueError(f"Bandit result {index} has an invalid test_id")
        if not isinstance(severity, str) or not severity:
            raise ValueError(f"Bandit result {index} has an invalid issue_severity")
        if severity == "LOW":
            low_test_ids.append(test_id)

    return dict(Counter(low_test_ids))


def _baseline_counts(document: dict[str, Any]) -> dict[str, int]:
    """Validate and return the LOW-severity baseline counts.

    Raises:
        ValueError: If the baseline metadata or counts are malformed.

    """
    if document.get("severity") != "LOW":
        raise ValueError("baseline severity must be 'LOW'")
    generated_by = document.get("generated_by")
    if not isinstance(generated_by, str) or not generated_by.strip():
        raise ValueError("baseline must contain a non-empty generated_by reference")

    counts = document.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("baseline must define a 'counts' object")

    validated_counts: dict[str, int] = {}
    for test_id, count in counts.items():
        if (
            not isinstance(test_id, str)
            or not test_id
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
        ):
            raise ValueError(f"invalid baseline count for {test_id!r}: {count!r}")
        validated_counts[test_id] = count
    return validated_counts


def diff_against_baseline(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Return classified regression and stale-baseline differences."""
    problems: list[str] = []
    for test_id in sorted(current.keys() | baseline.keys()):
        current_count = current.get(test_id, 0)
        baseline_count = baseline.get(test_id, 0)
        if current_count > baseline_count:
            reason = "is new" if baseline_count == 0 else "count increased"
            problems.append(f"REGRESSION: {test_id} {reason} ({baseline_count} -> {current_count})")
        elif current_count < baseline_count:
            reason = "is no longer observed" if current_count == 0 else "count decreased"
            problems.append(
                f"STALE BASELINE: {test_id} {reason} ({baseline_count} -> {current_count})"
            )
    return problems


def _write_baseline(
    baseline_path: Path,
    counts: dict[str, int],
    review_reference: str,
) -> None:
    """Atomically write reviewed LOW-severity counts."""
    document = {
        "generated_by": review_reference.strip(),
        "severity": "LOW",
        "counts": dict(sorted(counts.items())),
    }
    safe_write(
        baseline_path,
        json.dumps(document, indent=2) + "\n",
        backup=False,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare a Bandit LOW-severity report with its reviewed baseline."
    )
    parser.add_argument("report_path", type=Path)
    parser.add_argument("baseline_path", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument(
        "--review-reference",
        help="Issue or PR recording the security review; required when updating.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Check or deliberately update the Bandit LOW-severity baseline."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.update_baseline and (
        args.review_reference is None or not args.review_reference.strip()
    ):
        parser.error("--update-baseline requires --review-reference")
    if args.review_reference is not None and not args.update_baseline:
        parser.error("--review-reference requires --update-baseline")

    try:
        current = count_by_test_id(_load_json_object(args.report_path))
        if args.update_baseline:
            _write_baseline(args.baseline_path, current, args.review_reference)
            print(f"Updated Bandit LOW baseline: {args.baseline_path}")
            return 0
        baseline = _baseline_counts(_load_json_object(args.baseline_path))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: invalid Bandit baseline input: {exc}", file=sys.stderr)
        return 2

    problems = diff_against_baseline(current, baseline)
    if not problems:
        return 0

    print("ERROR: Bandit LOW-severity report does not match the reviewed baseline:")
    for problem in problems:
        print(f"  {problem}")
    print(
        "\nReview every changed finding. After approval, use "
        "--update-baseline with --review-reference; see SECURITY.md."
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
