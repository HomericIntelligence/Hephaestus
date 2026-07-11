"""Compare a bandit LOW-severity JSON report against the checked-in baseline."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def count_by_test_id(report: dict[str, Any]) -> dict[str, int]:
    """Return {test_id: count} for LOW-severity results in a bandit JSON report.

    Bandit's ``--severity-level low`` flag is a *minimum* threshold, so the
    report can also contain MEDIUM/HIGH findings. Those are filtered out here
    so they are never compared against the LOW-only baseline.
    """
    low_test_ids = (
        r["test_id"] for r in report.get("results", []) if r.get("issue_severity") == "LOW"
    )
    return dict(Counter(low_test_ids))


def diff_against_baseline(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Return human-readable drift lines: new test IDs or increased counts.

    A *decrease* in a known ID's count is not flagged (fixes shrink the
    baseline; re-review updates the file to match). A brand-new test ID, or
    an increase in an existing one, is flagged for security re-review.
    """
    problems: list[str] = []
    for test_id, count in sorted(current.items()):
        base_count = baseline.get(test_id)
        if base_count is None:
            problems.append(f"{test_id}: new LOW-severity finding type ({count} occurrence(s))")
        elif count > base_count:
            problems.append(f"{test_id}: count increased {base_count} -> {count}")
    return problems


def main(report_path: Path, baseline_path: Path) -> int:
    """Exit non-zero if the live report drifts above the checked-in baseline."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["counts"]
    problems = diff_against_baseline(count_by_test_id(report), baseline)
    if problems:
        print("ERROR: bandit LOW-severity findings drifted from the baseline:")
        for p in problems:
            print(f"  {p}")
        print(
            "\nReview the new/increased findings, then either fix them or update "
            "hephaestus/ci/bandit_low_baseline.json with a security re-review "
            "(issue #1481)."
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
