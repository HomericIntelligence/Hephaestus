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

import logging
from collections.abc import Callable
from unittest.mock import patch

import pytest

from hephaestus.automation import loop_runner
from hephaestus.automation.loop_runner import LoopConfig, PhaseResult, RepoResult

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
    # Patch the admission module where _fetch_issue_comment_ids is used
    with patch(
        "hephaestus.automation.pipeline.admission._fetch_issue_comment_ids", return_value=comments
    ):
        assert loop_runner._fetch_planned_files(101) is None


def test_fetch_planned_files_empty_comment_list_returns_none() -> None:
    """An empty fetch (the swallowed-error signal) → None; no try/except needed."""
    with patch(
        "hephaestus.automation.pipeline.admission._fetch_issue_comment_ids", return_value=[]
    ):
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
    with patch(
        "hephaestus.automation.pipeline.admission._fetch_issue_comment_ids", return_value=comments
    ):
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
    # Patch in the admission module where _select_non_overlapping is defined
    with patch(
        "hephaestus.automation.pipeline.admission._fetch_planned_files",
        side_effect=lambda i: plans[i],
    ):
        dispatch, defer = loop_runner._select_non_overlapping([1, 2])
    assert dispatch == [1]
    assert defer == [2]


def test_select_non_overlapping_disjoint_both_dispatched() -> None:
    """Non-intersecting file sets → both dispatched, none deferred."""
    plans = {
        1: {"hephaestus/automation/a.py"},
        2: {"hephaestus/automation/b.py"},
    }
    with patch(
        "hephaestus.automation.pipeline.admission._fetch_planned_files",
        side_effect=lambda i: plans[i],
    ):
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
    with patch(
        "hephaestus.automation.pipeline.admission._fetch_planned_files",
        side_effect=lambda i: plans[i],
    ):
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
    with patch(
        "hephaestus.automation.pipeline.admission._fetch_planned_files",
        side_effect=lambda i: plans[i],
    ):
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
        # Patch both: admission (where _select_non_overlapping uses it) and loop_runner (the shim)
        patch(
            "hephaestus.automation.pipeline.admission._fetch_planned_files",
            side_effect=lambda i: plans[i],
        ),
        patch(
            "hephaestus.automation.pipeline.admission._fetch_planned_files",
            side_effect=lambda i: plans[i],
        ),
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
# Terminal deferred-issue catch-up (#1762, option (b))
# ---------------------------------------------------------------------------


def _make_fake_process_repo(
    calls: list[tuple[str, int, list[int], int, bool]],
    deferred: list[int],
) -> Callable[[str, int, LoopConfig], RepoResult]:
    """Fake ``process_repo`` recording calls; the unpinned call defers *deferred*."""

    def fake_process_repo(repo: str, loop_idx: int, cfg: LoopConfig) -> RepoResult:
        calls.append(
            (repo, loop_idx, list(cfg.issues), cfg.max_workers, cfg.serialize_file_overlap)
        )
        result = RepoResult(
            repo=repo,
            loop_idx=loop_idx,
            phases=[PhaseResult(name="plan", rc=0, work_units=1)],
        )
        if not cfg.issues:
            result.deferred_issues = list(deferred)
        return result

    return fake_process_repo


def test_terminal_catchup_runs_after_post_loop_with_guard_on() -> None:
    """Final-round deferrals replay AFTER post-loop stages, guard kept ON.

    The catch-up must run only once the post-loop drive-green sweep has had
    its chance to merge the overlapping peers' PRs, must keep
    ``serialize_file_overlap=True``, must tag results ``is_catchup=True``,
    and must consult the rate budget between serial dispatches.
    """
    cfg = LoopConfig(
        loops=1,
        max_workers=3,
        phases=loop_runner.ALL_SELECTABLE,
        serialize_file_overlap=True,
    )
    calls: list[tuple[str, int, list[int], int, bool]] = []
    order: list[str] = []
    sleeps: list[tuple[int, int]] = []

    fake_process_repo = _make_fake_process_repo(calls, deferred=[2, 3])

    def tracking_process_repo(repo: str, loop_idx: int, cfg: LoopConfig) -> RepoResult:
        order.append(f"process_repo:{list(cfg.issues)}")
        return fake_process_repo(repo, loop_idx, cfg)

    def fake_post_loop(cfg: LoopConfig, repos: list[str]) -> list[RepoResult]:
        order.append("post_loop")
        return []

    plans = {2: {"a.py"}, 3: {"b.py"}}
    with (
        patch.object(loop_runner, "process_repo", side_effect=tracking_process_repo),
        patch.object(loop_runner, "_run_post_loop_stages", side_effect=fake_post_loop),
        # Both deferred issues are the only open ones → no claiming peers.
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[2, 3]),
        patch(
            "hephaestus.automation.pipeline.admission._fetch_planned_files",
            side_effect=lambda i: plans.get(i),
        ),
        patch.object(loop_runner, "find_pr_for_issue", return_value=None),
        patch.object(
            loop_runner,
            "_maybe_sleep_for_rate_budget",
            side_effect=lambda idx, total: sleeps.append((idx, total)),
        ),
    ):
        results = loop_runner.run_loop(cfg, repos=["Repo"])

    # Catch-up dispatches strictly AFTER the post-loop drive-green sweep.
    assert order == ["process_repo:[]", "post_loop", "process_repo:[2]", "process_repo:[3]"]
    # Serial, single-worker, overlap guard STILL ON (never forced off).
    assert calls == [
        ("Repo", 1, [], 3, True),
        ("Repo", 1, [2], 1, True),
        ("Repo", 1, [3], 1, True),
    ]
    # Rate budget consulted between the two serial catch-up dispatches.
    assert (0, 1) in sleeps
    # Catch-up records are tagged so per-loop accounting excludes them.
    assert [r.is_catchup for r in results] == [False, True, True]
    assert [r.deferred_issues for r in results] == [[2, 3], [], []]


def test_terminal_catchup_withholds_issue_while_peer_pr_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reviewer's NOGO scenario: peer PR still open → NOT redispatched.

    Redispatching a deferred issue while its overlapping peer's PR is still
    open would recreate the #1623 same-file conflict race. The issue must
    stay deferred with a prominent WARN naming the blocking peer.
    """
    cfg = LoopConfig(
        loops=1,
        max_workers=3,
        phases=loop_runner.ALL_SELECTABLE,
        serialize_file_overlap=True,
    )
    calls: list[tuple[str, int, list[int], int, bool]] = []
    fake_process_repo = _make_fake_process_repo(calls, deferred=[3])

    plans = {2: {"hephaestus/automation/shared.py"}, 3: {"hephaestus/automation/shared.py"}}
    with (
        caplog.at_level(logging.WARNING),
        patch.object(loop_runner, "process_repo", side_effect=fake_process_repo),
        patch.object(loop_runner, "_run_post_loop_stages", return_value=[]),
        # Peer #2 is still open alongside deferred #3 ...
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[2, 3]),
        patch(
            "hephaestus.automation.pipeline.admission._fetch_planned_files",
            side_effect=lambda i: plans.get(i),
        ),
        # ... and still owns an OPEN PR → its planned files stay claimed.
        patch.object(loop_runner, "find_pr_for_issue", return_value=77),
        patch.object(loop_runner, "_maybe_sleep_for_rate_budget"),
    ):
        results = loop_runner.run_loop(cfg, repos=["Repo"])

    # Only the initial unpinned round ran — issue #3 was NOT redispatched.
    assert calls == [("Repo", 1, [], 3, True)]
    assert [r.is_catchup for r in results] == [False]
    log_text = caplog.text
    assert "withheld" in log_text
    assert "peer #2 PR still open" in log_text
    assert "remain blocked" in log_text


def test_terminal_catchup_peer_without_open_pr_does_not_block() -> None:
    """A peer whose PR already merged (no open PR) releases its file claim."""
    cfg = LoopConfig(
        loops=1,
        max_workers=2,
        phases=loop_runner.ALL_SELECTABLE,
        serialize_file_overlap=True,
    )
    calls: list[tuple[str, int, list[int], int, bool]] = []
    fake_process_repo = _make_fake_process_repo(calls, deferred=[3])

    plans = {2: {"same.py"}, 3: {"same.py"}}
    with (
        patch.object(loop_runner, "process_repo", side_effect=fake_process_repo),
        patch.object(loop_runner, "_run_post_loop_stages", return_value=[]),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[2, 3]),
        patch(
            "hephaestus.automation.pipeline.admission._fetch_planned_files",
            side_effect=lambda i: plans.get(i),
        ),
        patch.object(loop_runner, "find_pr_for_issue", return_value=None),
        patch.object(loop_runner, "_maybe_sleep_for_rate_budget"),
    ):
        results = loop_runner.run_loop(cfg, repos=["Repo"])

    assert calls == [("Repo", 1, [], 2, True), ("Repo", 1, [3], 1, True)]
    assert results[-1].is_catchup is True


def test_terminal_catchup_shutdown_stops_cleanly() -> None:
    """A shutdown request mid-catch-up stops before the next dispatch."""
    cfg = LoopConfig(
        loops=1,
        max_workers=3,
        phases=loop_runner.ALL_SELECTABLE,
        serialize_file_overlap=True,
    )
    calls: list[tuple[str, int, list[int], int, bool]] = []
    fake_process_repo = _make_fake_process_repo(calls, deferred=[2, 3])

    # Call sites: run_loop's loop-start check, then one check per catch-up
    # issue. False → loop runs; False → issue #2 dispatches; True → stop.
    with (
        patch.object(loop_runner, "process_repo", side_effect=fake_process_repo),
        patch.object(loop_runner, "_run_post_loop_stages", return_value=[]),
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[2, 3]),
        patch("hephaestus.automation.pipeline.admission._fetch_planned_files", return_value=None),
        patch.object(loop_runner, "find_pr_for_issue", return_value=None),
        patch.object(loop_runner, "_maybe_sleep_for_rate_budget"),
        patch.object(loop_runner, "_shutdown_requested", side_effect=[False, False, True]),
    ):
        results = loop_runner.run_loop(cfg, repos=["Repo"])

    assert calls == [("Repo", 1, [], 3, True), ("Repo", 1, [2], 1, True)]
    assert len(results) == 2


def test_open_peer_pr_claims_only_open_pr_peers_claim() -> None:
    """Peers claim files only with an open PR AND a plan; deferred are excluded."""
    plans = {2: {"a.py"}, 4: {"b.py"}, 5: None}
    open_prs = {2: 77}  # #4 and #5 have no open PR

    with (
        patch.object(loop_runner, "_list_open_issue_numbers", return_value=[2, 3, 4, 5]),
        patch(
            "hephaestus.automation.pipeline.admission._fetch_planned_files",
            side_effect=lambda i: plans.get(i),
        ),
        patch.object(loop_runner, "find_pr_for_issue", side_effect=lambda i: open_prs.get(i)),
    ):
        claims = loop_runner._open_peer_pr_claims(LoopConfig(), "Repo", deferred={3})

    assert claims == {2: {"a.py"}}


def test_open_peer_pr_claims_listing_failure_fails_open() -> None:
    """An issue-listing failure yields no claims (catch-up proceeds)."""
    with patch.object(
        loop_runner, "_list_open_issue_numbers", side_effect=RuntimeError("api down")
    ):
        assert loop_runner._open_peer_pr_claims(LoopConfig(), "Repo", deferred={3}) == {}


# ---------------------------------------------------------------------------
# CLI flag
# ---------------------------------------------------------------------------


def test_parse_args_serialize_file_overlap_default_on() -> None:
    """Default is ON; --no-serialize-file-overlap opts out (#1623)."""
    assert loop_runner._parse_args([]).serialize_file_overlap is True
    assert loop_runner._parse_args(["--no-serialize-file-overlap"]).serialize_file_overlap is False
