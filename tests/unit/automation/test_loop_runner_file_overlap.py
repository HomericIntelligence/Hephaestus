"""File-overlap serialization for the issue-major loop (#1623).

Five ``state:plan-go`` refactor issues all edited the same
``hephaestus/automation/`` files and were dispatched concurrently by the
loop; the first PR to merge stranded the rest as ``CONFLICTING/DIRTY``. This
module tests the within-round file-overlap guard that defers issues whose
planned file sets intersect an in-flight peer's until the next loop round
(against freshly-merged trunk), plus the convergence-predicate change that
keeps a round with pending deferrals from early-exiting.
"""

from __future__ import annotations

from unittest.mock import patch

from hephaestus.automation import loop_runner
from hephaestus.automation.loop_runner import LoopConfig, RepoResult

# ---------------------------------------------------------------------------
# _parse_planned_files — heading-anchored plan-body parsing
# ---------------------------------------------------------------------------


def test_parse_planned_files_modify_section() -> None:
    """A ``## Files to Modify`` body yields its backticked in-tree paths."""
    body = (
        "# Implementation Plan\n\n"
        "## Files to Modify\n\n"
        "### `hephaestus/automation/address_review.py`\n"
        "Do a thing.\n"
        "- `hephaestus/automation/ci_driver.py`\n"
    )
    assert loop_runner._parse_planned_files(body) == {
        "hephaestus/automation/address_review.py",
        "hephaestus/automation/ci_driver.py",
    }


def test_parse_planned_files_create_section() -> None:
    """A ``## Files to Create`` body is scanned too (both headings)."""
    body = (
        "# Implementation Plan\n\n## Files to Create\n\n### `tests/unit/automation/test_new.py`\n"
    )
    assert loop_runner._parse_planned_files(body) == {"tests/unit/automation/test_new.py"}


def test_parse_planned_files_no_section_returns_empty() -> None:
    """A plan with neither Files heading yields an empty set."""
    body = "# Implementation Plan\n\n## Objective\n\nJust do `x/y.py` inline."
    assert loop_runner._parse_planned_files(body) == set()


def test_parse_planned_files_stops_at_next_heading() -> None:
    """Backticked paths after the section's closing ``## `` heading are ignored."""
    body = (
        "# Implementation Plan\n\n"
        "## Files to Modify\n\n"
        "- `hephaestus/automation/ci_driver.py`\n\n"
        "## Verification\n\n"
        "- `hephaestus/automation/should_not_count.py`\n"
    )
    assert loop_runner._parse_planned_files(body) == {"hephaestus/automation/ci_driver.py"}


# ---------------------------------------------------------------------------
# _fetch_planned_files — fail-open on missing/empty plan
# ---------------------------------------------------------------------------


def test_fetch_planned_files_no_plan_comment_returns_none() -> None:
    """Comments present but none is a plan comment → None (fail-open)."""
    comments = [{"body": "just a chat comment"}, {"body": "## 🔍 Plan Review"}]
    with patch.object(loop_runner, "_fetch_issue_comment_ids", return_value=comments):
        assert loop_runner._fetch_planned_files(101) is None


def test_fetch_planned_files_empty_comment_list_returns_none() -> None:
    """An empty fetch (the swallowed-error signal) → None; no try/except needed."""
    with patch.object(loop_runner, "_fetch_issue_comment_ids", return_value=[]):
        assert loop_runner._fetch_planned_files(102) is None


def test_fetch_planned_files_returns_plan_file_set() -> None:
    """A real plan comment yields its parsed file set."""
    comments = [
        {"body": "chatter"},
        {
            "body": (
                "# Implementation Plan\n\n## Files to Modify\n\n"
                "- `hephaestus/automation/address_review.py`\n"
            )
        },
    ]
    with patch.object(loop_runner, "_fetch_issue_comment_ids", return_value=comments):
        assert loop_runner._fetch_planned_files(103) == {"hephaestus/automation/address_review.py"}


# ---------------------------------------------------------------------------
# _select_non_overlapping — greedy first-fit partitioning (AC1/AC2)
# ---------------------------------------------------------------------------


def test_select_non_overlapping_defers_second_of_overlapping_pair() -> None:
    """AC1/AC2: two issues both listing address_review.py → first runs, second defers."""
    plans = {
        1: {"hephaestus/automation/address_review.py", "hephaestus/automation/a.py"},
        2: {"hephaestus/automation/address_review.py", "hephaestus/automation/b.py"},
    }
    with patch.object(loop_runner, "_fetch_planned_files", side_effect=lambda i: plans[i]):
        dispatch, defer = loop_runner._select_non_overlapping([1, 2])
    assert dispatch == [1]
    assert defer == [2]


def test_select_non_overlapping_disjoint_both_dispatched() -> None:
    """Non-intersecting file sets → both dispatched, none deferred."""
    plans = {
        1: {"hephaestus/automation/a.py"},
        2: {"hephaestus/automation/b.py"},
    }
    with patch.object(loop_runner, "_fetch_planned_files", side_effect=lambda i: plans[i]):
        dispatch, defer = loop_runner._select_non_overlapping([1, 2])
    assert dispatch == [1, 2]
    assert defer == []


def test_select_non_overlapping_unknown_plan_fails_open() -> None:
    """An issue whose plan file set is None claims no files → always dispatched."""
    plans: dict[int, set[str] | None] = {
        1: {"hephaestus/automation/address_review.py"},
        2: None,  # no plan yet — fail open
        3: {"hephaestus/automation/address_review.py"},
    }
    with patch.object(loop_runner, "_fetch_planned_files", side_effect=lambda i: plans[i]):
        dispatch, defer = loop_runner._select_non_overlapping([1, 2, 3])
    # #1 claims address_review.py; #2 unknown → dispatched; #3 overlaps #1 → deferred.
    assert dispatch == [1, 2]
    assert defer == [3]


def test_select_non_overlapping_first_issue_always_dispatched() -> None:
    """Liveness: the first issue always dispatches, so a batch is never wholly deferred."""
    plans = {
        1: {"hephaestus/automation/address_review.py"},
        2: {"hephaestus/automation/address_review.py"},
    }
    with patch.object(loop_runner, "_fetch_planned_files", side_effect=lambda i: plans[i]):
        dispatch, defer = loop_runner._select_non_overlapping([1, 2])
    assert dispatch[0] == 1
    assert defer == [2]


# ---------------------------------------------------------------------------
# RepoResult.deferred_issues — convergence predicate (AC1 cross-round)
# ---------------------------------------------------------------------------


def test_repo_result_deferred_issues_default_empty() -> None:
    """A fresh RepoResult has no deferred issues."""
    result = RepoResult(repo="Repo", loop_idx=1)
    assert result.deferred_issues == []
    assert result.produced_work is False


def test_repo_result_produced_work_true_when_deferred() -> None:
    """A round with pending deferrals is non-converged work (must keep looping)."""
    result = RepoResult(repo="Repo", loop_idx=1, deferred_issues=[42])
    assert result.produced_work is True


# ---------------------------------------------------------------------------
# _process_repo_inner — dispatch-site wiring
# ---------------------------------------------------------------------------


def test_process_repo_inner_defers_overlapping_issue(tmp_path: object) -> None:
    """Only the non-overlapping subset is submitted; the rest land in deferred_issues."""
    import pathlib

    repo_dir = pathlib.Path(str(tmp_path)) / "Repo"
    (repo_dir / ".git").mkdir(parents=True)
    cfg = LoopConfig(max_workers=2, serialize_file_overlap=True)
    result = RepoResult(repo="Repo", loop_idx=1)

    plans = {
        1: {"hephaestus/automation/address_review.py"},
        2: {"hephaestus/automation/address_review.py"},
    }
    submitted: list[int] = []

    def fake_process_one_issue(*, issue: int, **kw: object) -> list[object]:
        submitted.append(issue)
        return []

    with (
        patch.object(loop_runner, "_resolve_repo_dir", return_value=repo_dir),
        patch.object(loop_runner, "_rebase_main", return_value=("abc123", True)),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1, 2]),
        patch.object(loop_runner, "_fetch_planned_files", side_effect=lambda i: plans[i]),
        patch.object(loop_runner, "_process_one_issue", side_effect=fake_process_one_issue),
    ):
        out = loop_runner._process_repo_inner("Repo", 1, cfg, result)

    assert submitted == [1]
    assert out.deferred_issues == [2]


def test_serialize_disabled_dispatches_all(tmp_path: object) -> None:
    """serialize_file_overlap=False → every issue submitted, no deferrals."""
    import pathlib

    repo_dir = pathlib.Path(str(tmp_path)) / "Repo"
    (repo_dir / ".git").mkdir(parents=True)
    cfg = LoopConfig(max_workers=2, serialize_file_overlap=False)
    result = RepoResult(repo="Repo", loop_idx=1)

    submitted: list[int] = []

    def fake_process_one_issue(*, issue: int, **kw: object) -> list[object]:
        submitted.append(issue)
        return []

    with (
        patch.object(loop_runner, "_resolve_repo_dir", return_value=repo_dir),
        patch.object(loop_runner, "_rebase_main", return_value=("abc123", True)),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[1, 2]),
        patch.object(loop_runner, "_process_one_issue", side_effect=fake_process_one_issue),
    ):
        out = loop_runner._process_repo_inner("Repo", 1, cfg, result)

    assert sorted(submitted) == [1, 2]
    assert out.deferred_issues == []


# ---------------------------------------------------------------------------
# CLI flag
# ---------------------------------------------------------------------------


def test_parse_args_serialize_file_overlap_default_on() -> None:
    """Default is ON; --no-serialize-file-overlap opts out (#1623)."""
    assert loop_runner._parse_args([]).serialize_file_overlap is True
    assert loop_runner._parse_args(["--no-serialize-file-overlap"]).serialize_file_overlap is False
