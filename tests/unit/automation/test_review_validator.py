"""Tests for hephaestus.automation.review_validator.

The validator runs a read-only sub-agent that compares prior review comments
against the current diff and re-opens (as NEW inline threads) the ones the diff
does not address.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

from hephaestus.automation import review_validator


def _threads() -> list[dict[str, object]]:
    return [
        {"id": "T1", "path": "a.py", "line": 3, "body": "guard the null case"},
        {"id": "T2", "path": "b.py", "line": 7, "body": "rename for clarity"},
    ]


class TestReviewValidatorStructure:
    """Structure regression tests for review_validator orchestration."""

    def test_validate_prior_comments_addressed_stays_under_line_cap(self) -> None:
        source = Path(review_validator.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        target = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "validate_prior_comments_addressed"
        )

        assert target.end_lineno is not None
        assert target.end_lineno - target.lineno + 1 <= 80


class TestValidatePriorCommentsAddressed:
    """Tests for validate_prior_comments_addressed."""

    def test_no_prior_threads_is_clean_noop(self, tmp_path: Path) -> None:
        with patch.object(review_validator, "gh_pr_review_post") as post:
            reopened, is_clean, _ = review_validator.validate_prior_comments_addressed(
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
            reopened, is_clean, _ = review_validator.validate_prior_comments_addressed(
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
            reopened, is_clean, _ = review_validator.validate_prior_comments_addressed(
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
            reopened, is_clean, _ = review_validator.validate_prior_comments_addressed(
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
            reopened, is_clean, _ = review_validator.validate_prior_comments_addressed(
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

    def test_pr_level_unaddressed_without_path_is_surfaced_not_dropped(
        self, tmp_path: Path
    ) -> None:
        """#1329: a pathless (PR-level) unaddressed item is surfaced, not dropped.

        Previously a pathless item was silently skipped and the pass stayed
        "clean". Now it is surfaced at PR level via a summary-only review (no
        inline comment), and the pass is reported NOT clean so the loop addresses
        it.
        """
        unaddressed = [{"path": "", "line": None, "original_body": "x", "detail": "y"}]
        with (
            patch.object(
                review_validator, "_run_validation_session", return_value=(unaddressed, [])
            ),
            patch.object(review_validator, "gh_pr_review_post", return_value=[]) as post,
            # Both real threads are "addressed" (the unaddressed item has no
            # path) → the validator resolves them; mock to avoid real gh calls.
            patch.object(review_validator, "gh_pr_resolve_thread"),
        ):
            reopened, is_clean, _ = review_validator.validate_prior_comments_addressed(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )
        # No inline thread id (summary-only review), but NOT clean — the loop
        # still treats the pass as having unaddressed work.
        assert reopened == []
        assert is_clean is False
        post.assert_called_once()
        # Posted with no inline comments; the PR-level finding rides the summary.
        assert post.call_args.kwargs["comments"] == []
        assert "PR-level" in post.call_args.kwargs["summary"]

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
            reopened, is_clean, _ = review_validator.validate_prior_comments_addressed(
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


class TestRecurringByDesignConvergence:
    """#1329: a recurring re-open documented as by-design in source is not re-added."""

    def _unaddressed(self) -> list[dict[str, object]]:
        return [
            {
                "thread_id": "T1",
                "path": "a.py",
                "line": 3,
                "original_body": "guard the null case",
                "detail": "still dereferences x",
            }
        ]

    def test_recurring_documented_in_source_is_not_readded(self, tmp_path: Path) -> None:
        """Round N+1: same finding, source now documents it by-design → no re-open.

        The finding's key was re-opened in a prior round (passed in via
        ``prior_reopened_keys``); the worktree source at a.py:3 carries a
        by-design comment. The validator must NOT re-add the comment and the pass
        is clean — converging the loop.
        """
        (tmp_path / "a.py").write_text(
            "def f(x):\n"
            "    # We intentionally skip the null guard here by design: callers\n"
            "    return x.value  # noqa — see contract in docstring\n"
        )
        prior_key = review_validator._thread_key(path="a.py", line=3, body="guard the null case")
        with (
            patch.object(
                review_validator,
                "_run_validation_session",
                return_value=(self._unaddressed(), []),
            ),
            patch.object(review_validator, "gh_pr_review_post") as post,
            patch.object(review_validator, "gh_pr_resolve_thread"),
        ):
            reopened, is_clean, keys = review_validator.validate_prior_comments_addressed(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=2,
                state_dir=tmp_path,
                prior_reopened_keys={prior_key},
            )
        assert reopened == []
        assert is_clean is True
        post.assert_not_called()  # documented by-design → not re-added
        # The key stays in the carried set so later rounds keep suppressing it.
        assert prior_key in keys

    def test_recurring_undocumented_still_reopens(self, tmp_path: Path) -> None:
        """Round N+1: same finding, but source does NOT document it → still re-opens."""
        (tmp_path / "a.py").write_text("def f(x):\n    return x.value\n")
        prior_key = review_validator._thread_key(path="a.py", line=3, body="guard the null case")
        with (
            patch.object(
                review_validator,
                "_run_validation_session",
                return_value=(self._unaddressed(), []),
            ),
            patch.object(review_validator, "gh_pr_review_post", return_value=["NEW"]) as post,
            patch.object(review_validator, "gh_pr_resolve_thread"),
        ):
            reopened, is_clean, keys = review_validator.validate_prior_comments_addressed(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=2,
                state_dir=tmp_path,
                prior_reopened_keys={prior_key},
            )
        assert reopened == ["NEW"]
        assert is_clean is False
        post.assert_called_once()
        # The recurring body is marked as such in the re-open.
        assert "recurring" in post.call_args.kwargs["comments"][0]["body"].lower()
        # The key is carried forward so a later documented round can converge.
        assert prior_key in keys

    def test_first_round_documented_source_still_reopens(self, tmp_path: Path) -> None:
        """A FIRST-round finding (not yet recurring) still re-opens even if documented.

        The by-design source-skip only applies to RECURRING re-opens — on the
        first occurrence the comment is posted so the implementer/reviewer gets a
        chance to engage. (Without a prior key the finding isn't recurring.)
        """
        (tmp_path / "a.py").write_text(
            "def f(x):\n    # intentional by design\n    return x.value\n"
        )
        with (
            patch.object(
                review_validator,
                "_run_validation_session",
                return_value=(self._unaddressed(), []),
            ),
            patch.object(review_validator, "gh_pr_review_post", return_value=["NEW"]) as post,
            patch.object(review_validator, "gh_pr_resolve_thread"),
        ):
            reopened, is_clean, keys = review_validator.validate_prior_comments_addressed(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=0,
                state_dir=tmp_path,
                prior_reopened_keys=set(),
            )
        assert reopened == ["NEW"]
        assert is_clean is False
        post.assert_called_once()
        # Its key is recorded so a NEXT round can recognise the recurrence.
        assert review_validator._thread_key(path="a.py", line=3, body="guard the null case") in keys


class TestThreadKey:
    """#1329: stable cross-round identity for prior review threads."""

    def test_normalizes_whitespace_and_case(self) -> None:
        a = review_validator._thread_key(path="a.py", line=3, body="Guard  the\nnull case")
        b = review_validator._thread_key(path="a.py", line=3, body="guard the null case")
        assert a == b

    def test_distinguishes_path_and_line(self) -> None:
        a = review_validator._thread_key(path="a.py", line=3, body="x")
        b = review_validator._thread_key(path="b.py", line=3, body="x")
        c = review_validator._thread_key(path="a.py", line=4, body="x")
        assert a != b
        assert a != c

    def test_none_line_is_stable(self) -> None:
        a = review_validator._thread_key(path="", line=None, body="pr level")
        b = review_validator._thread_key(path="", line=None, body="pr level")
        assert a == b


class TestSourceDocumentsDecision:
    """#1329: the 'documented in source' heuristic reads source near the line."""

    def test_finds_by_design_marker_near_line(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text(
            "line1\nline2\n# this is intentional by design\nline4\nline5\n"
        )
        assert review_validator._source_documents_decision(tmp_path, "a.py", 4) is True

    def test_marker_far_from_line_does_not_count(self, tmp_path: Path) -> None:
        body = "\n".join(["# by design"] + [f"code{i}" for i in range(30)])
        (tmp_path / "a.py").write_text(body + "\n")
        # The marker is at line 1, the cited line is 25 — outside the window.
        assert review_validator._source_documents_decision(tmp_path, "a.py", 25) is False

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        assert review_validator._source_documents_decision(tmp_path, "missing.py", 1) is False

    def test_pathless_or_bad_line_returns_false(self, tmp_path: Path) -> None:
        assert review_validator._source_documents_decision(tmp_path, "", 1) is False
        assert review_validator._source_documents_decision(tmp_path, "a.py", None) is False

    def test_path_traversal_returns_false(self, tmp_path: Path) -> None:
        assert review_validator._source_documents_decision(tmp_path, "../escape.py", 1) is False


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


class TestRunValidationAndReconcile:
    """Tests for _run_validation_and_reconcile."""

    def test_returns_unaddressed_and_reconciles_threads(self, tmp_path: Path) -> None:
        threads = [{"id": "T1", "path": "a.py", "line": 3, "body": "x"}]
        unaddr = [{"thread_id": "T2", "path": "a.py", "line": 3, "detail": "d"}]
        with (
            patch.object(review_validator, "_run_validation_session", return_value=(unaddr, [])),
            patch.object(review_validator, "_dismiss_wont_fix_prior_threads") as dismiss,
            patch.object(review_validator, "_resolve_addressed_prior_threads") as resolve,
        ):
            out = review_validator._run_validation_and_reconcile(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=threads,
                diff_text="diff",
                agent="claude",
                iteration=0,
                state_dir=tmp_path,
                timeout=60,
            )
        assert out == unaddr
        dismiss.assert_called_once()
        resolve.assert_called_once()

    def test_empty_unaddressed_still_resolves(self, tmp_path: Path) -> None:
        threads = [{"id": "T1", "path": "a.py", "line": 3, "body": "x"}]
        with (
            patch.object(review_validator, "_run_validation_session", return_value=([], [])),
            patch.object(review_validator, "_dismiss_wont_fix_prior_threads"),
            patch.object(review_validator, "_resolve_addressed_prior_threads") as resolve,
        ):
            out = review_validator._run_validation_and_reconcile(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=threads,
                diff_text="diff",
                agent="claude",
                iteration=0,
                state_dir=tmp_path,
                timeout=60,
            )
        assert out == []
        resolve.assert_called_once()


class TestClassifyUnaddressedFindings:
    """Tests for _classify_unaddressed_findings."""

    def test_recurring_documented_finding_is_dropped(self, tmp_path: Path) -> None:
        unaddressed = [
            {
                "thread_id": "T1",
                "path": "a.py",
                "line": 3,
                "detail": "x",
                "original_body": "x",
            }
        ]
        key = review_validator._thread_key(path="a.py", line=3, body="x")
        with patch.object(review_validator, "_source_documents_decision", return_value=True):
            comments, pathless, new_keys = review_validator._classify_unaddressed_findings(
                unaddressed=unaddressed,
                seen_keys={key},
                worktree_path=tmp_path,
                pr_number=1,
                iteration=2,
            )
        assert comments == [] and pathless == [] and new_keys == set()

    def test_pathless_finding_goes_to_pr_level(self, tmp_path: Path) -> None:
        unaddressed = [{"thread_id": "T1", "path": "", "line": None, "detail": "global note"}]
        comments, pathless, new_keys = review_validator._classify_unaddressed_findings(
            unaddressed=unaddressed,
            seen_keys=set(),
            worktree_path=tmp_path,
            pr_number=1,
            iteration=0,
        )
        assert comments == [] and len(pathless) == 1 and len(new_keys) == 1

    def test_inline_finding_builds_comment_with_line(self, tmp_path: Path) -> None:
        unaddressed = [
            {
                "thread_id": "T1",
                "path": "a.py",
                "line": 7,
                "detail": "guard",
                "original_body": "orig",
            }
        ]
        comments, pathless, _ = review_validator._classify_unaddressed_findings(
            unaddressed=unaddressed,
            seen_keys=set(),
            worktree_path=tmp_path,
            pr_number=1,
            iteration=0,
        )
        assert pathless == []
        assert (
            comments[0]["path"] == "a.py"
            and comments[0]["line"] == 7
            and comments[0]["side"] == "RIGHT"
        )


class TestPostReopenedFindings:
    """Tests for _post_reopened_findings."""

    def test_posts_comments_and_appends_pathless_to_summary(self) -> None:
        with patch.object(review_validator, "gh_pr_review_post", return_value=["NEW"]) as post:
            ids = review_validator._post_reopened_findings(
                pr_number=1,
                iteration=0,
                comments=[{"path": "a.py", "body": "b", "side": "RIGHT", "line": 3}],
                pathless=[{"body": "pr-level note"}],
            )
        assert ids == ["NEW"]
        kwargs = post.call_args.kwargs
        assert kwargs["dedupe_existing"] is True
        assert "pr-level note" in kwargs["summary"]
