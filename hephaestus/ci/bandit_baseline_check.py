"""Compare a Bandit LOW-severity JSON report against the checked-in baseline."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from hephaestus.cli.localization import text
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


def low_findings(report: dict[str, Any]) -> list[tuple[int, str, str | None]]:
    """Validate a Bandit report and return (index, test_id, filename) LOW tuples.

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

    findings: list[tuple[int, str, str | None]] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError(f"Bandit result {index} must be an object")
        test_id = result.get("test_id")
        severity = result.get("issue_severity")
        if not isinstance(test_id, str) or not test_id:
            raise ValueError(f"Bandit result {index} has an invalid test_id")
        if not isinstance(severity, str) or not severity:
            raise ValueError(f"Bandit result {index} has an invalid issue_severity")
        if severity != "LOW":
            continue
        filename = result.get("filename")
        if filename is not None and (
            not isinstance(filename, str) or not filename.strip() or "\r" in filename
        ):
            raise ValueError(f"Bandit result {index} has an invalid filename")
        findings.append((index, test_id, filename))
    return findings


def count_by_file_test_id(report: dict[str, Any]) -> dict[str, int]:
    """Return LOW-severity counts keyed by ``filename::test_id``.

    Identical validation to :func:`count_by_test_id`; the per-file key lets the
    diff cancel same-ID findings only within the same file, instead of hiding
    offsetting drift repository-wide.

    Raises:
        ValueError: If the report or any finding has an invalid structure.

    """
    counts: dict[str, int] = Counter()
    for index, test_id, filename in low_findings(report):
        if filename is None:
            raise ValueError(f"Bandit result {index} has an invalid filename")
        counts[f"{filename}::{test_id}"] += 1
    return dict(counts)


def count_by_test_id(report: dict[str, Any]) -> dict[str, int]:
    """Return LOW-severity finding counts from a validated Bandit report.

    Bandit's ``--severity-level low`` flag is a minimum threshold, so the
    report can also contain MEDIUM/HIGH findings. Those are filtered out here
    so they are never compared against the LOW-only baseline.

    Raises:
        ValueError: If the report or any finding has an invalid structure.

    """
    return dict(Counter(test_id for _, test_id, _ in low_findings(report)))


def _validated_counts_object(counts: Any, label: str) -> dict[str, int]:
    """Validate one ``{key: int}`` object from the baseline document."""
    if not isinstance(counts, dict):
        raise ValueError(f"baseline must define a '{label}' object")
    validated: dict[str, int] = {}
    for key, count in counts.items():
        if (
            not isinstance(key, str)
            or not key
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
        ):
            raise ValueError(f"invalid baseline {label} count for {key!r}: {count!r}")
        validated[key] = count
    return validated


def _baseline_files(document: dict[str, Any]) -> dict[str, int] | None:
    """Validate the optional per-file counts; return ``None`` when absent.

    Baselines written before the per-file comparison was introduced omit this
    section; callers fall back to the repository-wide ``counts`` comparison so
    they keep validating exactly as before.
    """
    files = document.get("files")
    if files is None:
        return None
    return _validated_counts_object(files, "files")


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

    return _validated_counts_object(document.get("counts"), "counts")


def diff_against_baseline(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Return classified regression and stale-baseline differences."""
    return diff_against_keys(current, baseline)


def diff_files_against_baseline(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Return classified per-file drift differences."""
    return diff_against_keys(current, baseline)


def diff_against_keys(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Classify drift over an arbitrary ``{key: count}`` comparison space."""
    problems: list[str] = []
    for key in sorted(current.keys() | baseline.keys()):
        current_count = current.get(key, 0)
        baseline_count = baseline.get(key, 0)
        if current_count > baseline_count:
            reason = "is new" if baseline_count == 0 else "count increased"
            problems.append(f"REGRESSION: {key} {reason} ({baseline_count} -> {current_count})")
        elif current_count < baseline_count:
            reason = "is no longer observed" if current_count == 0 else "count decreased"
            problems.append(f"STALE BASELINE: {key} {reason} ({baseline_count} -> {current_count})")
    return problems


def _format_problem(problem: str) -> str:
    """Translate one classified problem while retaining its result data."""
    patterns = (
        (
            r"REGRESSION: (?P<test_id>\S+) is new \((?P<baseline>\d+) -> (?P<current>\d+)\)",
            "REGRESSION: %(test_id)s is new (%(baseline)d -> %(current)d)",
        ),
        (
            r"REGRESSION: (?P<test_id>\S+) count increased "
            r"\((?P<baseline>\d+) -> (?P<current>\d+)\)",
            "REGRESSION: %(test_id)s count increased (%(baseline)d -> %(current)d)",
        ),
        (
            r"STALE BASELINE: (?P<test_id>\S+) is no longer observed "
            r"\((?P<baseline>\d+) -> (?P<current>\d+)\)",
            "STALE BASELINE: %(test_id)s is no longer observed (%(baseline)d -> %(current)d)",
        ),
        (
            r"STALE BASELINE: (?P<test_id>\S+) count decreased "
            r"\((?P<baseline>\d+) -> (?P<current>\d+)\)",
            "STALE BASELINE: %(test_id)s count decreased (%(baseline)d -> %(current)d)",
        ),
    )
    for pattern, source in patterns:
        if match := re.fullmatch(pattern, problem):
            values: dict[str, object] = match.groupdict()
            values["baseline"] = int(match["baseline"])
            values["current"] = int(match["current"])
            return text(source, **values)
    return problem


def _write_baseline(
    baseline_path: Path,
    counts: dict[str, int],
    files: dict[str, int],
    review_reference: str,
) -> None:
    """Atomically write reviewed LOW-severity counts and per-file counts."""
    document = {
        "generated_by": review_reference.strip(),
        "severity": "LOW",
        "counts": dict(sorted(counts.items())),
        "files": dict(sorted(files.items())),
    }
    safe_write(
        baseline_path,
        json.dumps(document, indent=2) + "\n",
        backup=False,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=text("Compare a Bandit LOW-severity report with its reviewed baseline.")
    )
    parser.add_argument("report_path", type=Path)
    parser.add_argument("baseline_path", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument(
        "--review-reference",
        help=text("Issue or PR recording the security review; required when updating."),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Check or deliberately update the Bandit LOW-severity baseline."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.update_baseline and (
        args.review_reference is None or not args.review_reference.strip()
    ):
        parser.error(text("--update-baseline requires --review-reference"))
    if args.review_reference is not None and not args.update_baseline:
        parser.error(text("--review-reference requires --update-baseline"))

    try:
        report_doc = _load_json_object(args.report_path)
        current = count_by_test_id(report_doc)
        if args.update_baseline:
            _write_baseline(
                args.baseline_path,
                current,
                count_by_file_test_id(report_doc),
                args.review_reference,
            )
            print(text("Updated Bandit LOW baseline: %(value0)s", value0=args.baseline_path))
            return 0
        baseline_doc = _load_json_object(args.baseline_path)
        baseline = _baseline_counts(baseline_doc)
        baseline_files = _baseline_files(baseline_doc)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(text("ERROR: invalid Bandit baseline input: %(value0)s", value0=exc), file=sys.stderr)
        return 2

    problems = diff_against_baseline(current, baseline)
    if baseline_files is not None:
        problems += diff_files_against_baseline(count_by_file_test_id(report_doc), baseline_files)
    if not problems:
        return 0

    print(text("ERROR: Bandit LOW-severity report does not match the reviewed baseline:"))
    for problem in problems:
        print(text("  %(problem)s", problem=_format_problem(problem)))
    print(
        text(
            "\nReview every changed finding. After approval, use "
            "--update-baseline with --review-reference; see SECURITY.md."
        )
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
