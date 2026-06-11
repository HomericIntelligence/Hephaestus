"""Tests for hephaestus.automation.review_validator.

The validator runs a read-only sub-agent that compares prior review comments
against the current diff and re-opens (as NEW inline threads) the ones the diff
does not address.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from hephaestus.automation import review_validator


def _threads() -> list[dict[str, object]]:
    return [
        {"id": "T1", "path": "a.py", "line": 3, "body": "guard the null case"},
        {"id": "T2", "path": "b.py", "line": 7, "body": "rename for clarity"},
    ]


class TestValidatePriorCommentsAddressed:
    """Tests for validate_prior_comments_addressed."""

    def test_no_prior_threads_is_clean_noop(self, tmp_path: Path) -> None:
        with patch.object(review_validator, "gh_pr_review_post") as post:
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=[],
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )
        assert reopened == []
        assert is_clean is True
        post.assert_not_called()

    def test_dry_run_is_clean_noop(self, tmp_path: Path) -> None:
        with (
            patch.object(review_validator, "_run_validation_session") as run,
            patch.object(review_validator, "gh_pr_review_post") as post,
        ):
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
                dry_run=True,
            )
        assert (reopened, is_clean) == ([], True)
        run.assert_not_called()
        post.assert_not_called()

    def test_all_addressed_posts_nothing_and_resolves_all(self, tmp_path: Path) -> None:
        """Confirming all prior threads addressed resolves them all in place.

        #1083: evidence-based resolution moves from the implementer's
        self-report to the validator/reviewer.
        """
        with (
            patch.object(review_validator, "_run_validation_session", return_value=([], [])),
            patch.object(review_validator, "gh_pr_review_post") as post,
            patch.object(review_validator, "gh_pr_resolve_thread") as resolve,
        ):
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )
        assert reopened == []
        assert is_clean is True
        post.assert_not_called()
        # Both prior threads (T1, T2) were confirmed addressed → resolved
        # quietly, without adding another review-thread reply.
        resolved_ids = {c.args[0] for c in resolve.call_args_list}
        assert resolved_ids == {"T1", "T2"}
        assert all(c.kwargs == {"dry_run": False} for c in resolve.call_args_list)

    def test_partial_resolves_only_addressed_threads(self, tmp_path: Path) -> None:
        """Only the addressed thread is resolved; the unaddressed one stays open.

        #1083: a.py is unaddressed (re-opened); b.py is addressed → only T2 is
        resolved, T1 is not.
        """
        unaddressed = [
            {
                "thread_id": "T1",
                "path": "a.py",
                "line": 3,
                "original_body": "guard the null case",
                "detail": "still dereferences x",
            }
        ]
        with (
            patch.object(
                review_validator, "_run_validation_session", return_value=(unaddressed, [])
            ),
            patch.object(review_validator, "gh_pr_review_post", return_value=["NEW"]),
            patch.object(review_validator, "gh_pr_resolve_thread") as resolve,
        ):
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )
        assert reopened == ["NEW"]
        assert is_clean is False
        resolved_ids = {c.args[0] for c in resolve.call_args_list}
        # Only the addressed thread (T2) is resolved; T1 (unaddressed) stays open.
        assert resolved_ids == {"T2"}
        assert resolve.call_args.kwargs == {"dry_run": False}

    def test_resolves_by_id_not_path_line(self, tmp_path: Path) -> None:
        """#1085 C2: two threads on the SAME (path, line) resolve independently.

        T1 and T3 both sit on a.py:3. The sub-agent flags only T1 as
        unaddressed; T3 must still be resolved (a (path,line) match would have
        wrongly kept both open).
        """
        threads = [
            {"id": "T1", "path": "a.py", "line": 3, "body": "first note"},
            {"id": "T3", "path": "a.py", "line": 3, "body": "second note, fixed"},
        ]
        unaddressed = [
            {
                "thread_id": "T1",
                "path": "a.py",
                "line": 3,
                "original_body": "first note",
                "detail": "x",
            }
        ]
        with (
            patch.object(
                review_validator, "_run_validation_session", return_value=(unaddressed, [])
            ),
            patch.object(review_validator, "gh_pr_review_post", return_value=["NEW"]),
            patch.object(review_validator, "gh_pr_resolve_thread") as resolve,
        ):
            review_validator.validate_prior_comments_addressed(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=threads,
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )
        resolved_ids = {c.args[0] for c in resolve.call_args_list}
        assert resolved_ids == {"T3"}

    def test_dry_run_resolves_nothing(self, tmp_path: Path) -> None:
        with (
            patch.object(review_validator, "_run_validation_session") as run,
            patch.object(review_validator, "gh_pr_resolve_thread") as resolve,
        ):
            review_validator.validate_prior_comments_addressed(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
                dry_run=True,
            )
        run.assert_not_called()
        resolve.assert_not_called()

    def test_unaddressed_reopens_new_inline_thread(self, tmp_path: Path) -> None:
        unaddressed = [
            {
                "path": "a.py",
                "line": 3,
                "original_body": "guard the null case",
                "detail": "still dereferences x without a check",
            }
        ]
        with (
            patch.object(
                review_validator, "_run_validation_session", return_value=(unaddressed, [])
            ),
            patch.object(
                review_validator, "gh_pr_review_post", return_value=["NEW_THREAD"]
            ) as post,
            # b.py thread is "addressed" → the validator resolves it; mock so no
            # real gh call (which would trip the github-api circuit breaker).
            patch.object(review_validator, "gh_pr_resolve_thread"),
        ):
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )

        assert reopened == ["NEW_THREAD"]
        assert is_clean is False
        post.assert_called_once()
        posted = post.call_args.kwargs["comments"]
        assert len(posted) == 1
        assert posted[0]["path"] == "a.py"
        assert posted[0]["line"] == 3
        assert posted[0]["side"] == "RIGHT"
        assert posted[0]["body"].startswith("Re-opening: prior review comment not addressed")
        # The original comment is quoted into the re-open body.
        assert "> guard the null case" in posted[0]["body"]

    def test_pr_level_unaddressed_without_path_is_skipped(self, tmp_path: Path) -> None:
        """An item with no path can't be an inline thread → skipped, stays clean."""
        unaddressed = [{"path": "", "line": None, "original_body": "x", "detail": "y"}]
        with (
            patch.object(
                review_validator, "_run_validation_session", return_value=(unaddressed, [])
            ),
            patch.object(review_validator, "gh_pr_review_post") as post,
            # Both real threads are "addressed" (the unaddressed item has no
            # path) → the validator resolves them; mock to avoid real gh calls.
            patch.object(review_validator, "gh_pr_resolve_thread"),
        ):
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )
        assert reopened == []
        assert is_clean is True
        post.assert_not_called()

    def test_wont_fix_dismisses_with_marker_reply_and_no_reopen(self, tmp_path: Path) -> None:
        """#1163: a won't-fix finding resolves with the marker reply, never re-opens.

        The validator partitions T1 as won't-fix (intentional design); it must be
        resolved with a ``WONT_FIX_MARKER`` reply and NOT posted as a re-open
        thread. T2 (addressed) resolves plainly. Nothing is re-opened, so the pass
        is clean.
        """
        from hephaestus.automation.protocol import WONT_FIX_MARKER

        wont_fix = [{"thread_id": "T1", "reason": "abstract method, NotImplementedError by design"}]
        with (
            patch.object(review_validator, "_run_validation_session", return_value=([], wont_fix)),
            patch.object(review_validator, "gh_pr_review_post") as post,
            patch.object(review_validator, "gh_pr_resolve_thread") as resolve,
        ):
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )

        assert reopened == []
        assert is_clean is True
        post.assert_not_called()  # won't-fix is NOT re-opened
        # T1 resolved WITH the won't-fix marker reply; T2 resolved plainly.
        calls = {c.args[0]: c.kwargs.get("reply_body") for c in resolve.call_args_list}
        assert set(calls) == {"T1", "T2"}
        assert calls["T1"] is not None and calls["T1"].startswith(WONT_FIX_MARKER)
        assert "NotImplementedError by design" in calls["T1"]
        assert calls["T2"] is None  # addressed → bare resolve


class TestDismissWontFixPriorThreads:
    """#1163: won't-fix dismissal resolves threads with the durable marker reply."""

    def test_resolves_only_wont_fix_ids_with_marker(self) -> None:
        from hephaestus.automation.protocol import WONT_FIX_MARKER

        threads = [
            {"id": "T1", "path": "a.py", "line": 1, "body": "x"},
            {"id": "T2", "path": "b.py", "line": 2, "body": "y"},
        ]
        wont_fix = [{"thread_id": "T1", "reason": "interface stub"}]
        with patch.object(review_validator, "gh_pr_resolve_thread") as resolve:
            dismissed = review_validator._dismiss_wont_fix_prior_threads(threads, wont_fix, {"T1"})
        assert dismissed == ["T1"]
        assert resolve.call_count == 1
        assert resolve.call_args.args[0] == "T1"
        assert resolve.call_args.kwargs["reply_body"].startswith(WONT_FIX_MARKER)
        assert "interface stub" in resolve.call_args.kwargs["reply_body"]

    def test_bare_marker_when_no_reason(self) -> None:
        from hephaestus.automation.protocol import WONT_FIX_MARKER

        threads = [{"id": "T1", "path": "a.py", "line": 1, "body": "x"}]
        with patch.object(review_validator, "gh_pr_resolve_thread") as resolve:
            review_validator._dismiss_wont_fix_prior_threads(threads, [{"thread_id": "T1"}], {"T1"})
        assert resolve.call_args.kwargs["reply_body"] == WONT_FIX_MARKER

    def test_continues_past_failure(self) -> None:
        import subprocess

        threads = [
            {"id": "T1", "path": "a.py", "line": 1, "body": "x"},
            {"id": "T2", "path": "b.py", "line": 2, "body": "y"},
        ]
        wont_fix = [{"thread_id": "T1"}, {"thread_id": "T2"}]
        with patch.object(
            review_validator,
            "gh_pr_resolve_thread",
            side_effect=[subprocess.CalledProcessError(1, "gh"), None],
        ):
            dismissed = review_validator._dismiss_wont_fix_prior_threads(
                threads, wont_fix, {"T1", "T2"}
            )
        assert dismissed == ["T2"]  # only the successful one


class TestResolveAddressedPriorThreads:
    """#1085 C4: the resolver continues past a failing resolve call."""

    def test_continues_after_one_resolve_failure(self) -> None:
        import subprocess

        threads = [
            {"id": "T1", "path": "a.py", "line": 1, "body": "x"},
            {"id": "T2", "path": "b.py", "line": 2, "body": "y"},
        ]
        # T1's resolve raises; T2 must still be attempted and resolved.
        with patch.object(
            review_validator,
            "gh_pr_resolve_thread",
            side_effect=[subprocess.CalledProcessError(1, "gh"), None],
        ) as resolve:
            resolved = review_validator._resolve_addressed_prior_threads(threads, set())
        assert [c.args[0] for c in resolve.call_args_list] == ["T1", "T2"]
        # Only the successful one is reported resolved.
        assert resolved == ["T2"]

    def test_skips_threads_without_id(self) -> None:
        threads = [{"path": "a.py", "line": 1, "body": "no id"}]
        with patch.object(review_validator, "gh_pr_resolve_thread") as resolve:
            resolved = review_validator._resolve_addressed_prior_threads(threads, set())
        resolve.assert_not_called()
        assert resolved == []
