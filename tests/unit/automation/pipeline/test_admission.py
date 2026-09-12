"""File-overlap serialization and admission control for the implementation queue.

Tests the within-round file-overlap guard that defers issues whose planned file
sets intersect an in-flight peer's, the topological-order gating for the
implementation queue, and the filtered-open-issues helper.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections.abc import Mapping
from typing import Any, ClassVar
from unittest.mock import patch

import pytest

from hephaestus.automation.comment_identity import CommentAliasConflictError
from hephaestus.automation.models import IssueInfo
from hephaestus.automation.pipeline.admission import (
    DependencyFact,
    _filter_open_issues,
    _parse_planned_files,
    dependency_block_reason,
    order_for_implementation,
    parse_publication_scope_files,
)
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.review_journal import render_current_plan


def _info(number: int, dependencies: list[int] | None = None) -> IssueInfo:
    """Build a minimal IssueInfo for dependency-ordering tests."""
    return IssueInfo(number=number, title=f"Issue {number}", dependencies=dependencies or [])


class TestParsePlannedFiles:
    """Plan-body parser: extract repo-relative paths from Files sections."""

    def test_parse_planned_files_modify_section(self) -> None:
        """A ``## Files to Modify`` body yields its backticked in-tree paths."""
        body = (
            "# Implementation Plan\n\n"
            "## Files to Modify\n\n"
            "### `hephaestus/automation/pipeline/stages/pr_review.py`\n"
            "Do a thing.\n"
            "- `hephaestus/automation/ci_driver.py`\n"
        )
        assert _parse_planned_files(body) == {
            "hephaestus/automation/pipeline/stages/pr_review.py",
            "hephaestus/automation/ci_driver.py",
        }

    def test_parse_planned_files_create_section(self) -> None:
        """A ``## Files to Create`` body is scanned too (both headings)."""
        body = (
            "# Implementation Plan\n\n## Files to Create\n\n"
            "### `tests/unit/automation/test_new.py`\n"
        )
        assert _parse_planned_files(body) == {"tests/unit/automation/test_new.py"}

    def test_parse_planned_files_no_section_returns_empty(self) -> None:
        """A plan with neither Files heading yields an empty set."""
        body = "# Implementation Plan\n\n## Objective\n\nJust do `x/y.py` inline."
        assert _parse_planned_files(body) == set()

    def test_parse_planned_files_stops_at_next_heading(self) -> None:
        """Backticked paths after the section's closing ``## `` heading are ignored."""
        body = (
            "# Implementation Plan\n\n"
            "## Files to Modify\n\n"
            "- `hephaestus/automation/ci_driver.py`\n\n"
            "## Verification\n\n"
            "- `hephaestus/automation/should_not_count.py`\n"
        )
        assert _parse_planned_files(body) == {"hephaestus/automation/ci_driver.py"}

    def test_parse_planned_files_bare_filenames_not_captured(self) -> None:
        """Bare filenames without directory (e.g., `pyproject.toml`) are NOT captured."""
        body = "# Implementation Plan\n\n## Files to Modify\n\n- `pyproject.toml`\n"
        assert _parse_planned_files(body) == set()

    def test_publication_scope_captures_top_level_files(self) -> None:
        """The publication allowlist includes top-level planned files."""
        body = "## Files to Modify\n\n- `pyproject.toml`\n- `uv.lock`\n"

        assert parse_publication_scope_files(body) == {"pyproject.toml", "uv.lock"}

    def test_parse_planned_files_case_insensitive_heading(self) -> None:
        """## Files to Modify/Create headings are case-insensitive."""
        body = "# Implementation Plan\n\n## FILES TO MODIFY\n\n- `hephaestus/automation/test.py`\n"
        assert _parse_planned_files(body) == {"hephaestus/automation/test.py"}


class TestPublicationScopeFiles:
    """Read complete file declarations from the accepted plan."""

    @pytest.mark.parametrize("heading", ["Files to Modify", "Files to Create", "File Changes"])
    def test_complete_safe_paths(self, heading: str) -> None:
        paths = {
            "src/main.py",
            ".github/workflows/test.yml",
            ".pre-commit-config.yaml",
            "justfile",
            "docs/My Guide",
            "bin/run",
        }
        body = f"## {heading}\n" + "\n".join(f"- `{path}`" for path in paths)
        assert parse_publication_scope_files(body) == paths

    @pytest.mark.parametrize(
        "path", ["../outside", "/absolute", "./local", "a/../b", "bad\\path", "bad\x00path", ""]
    )
    def test_invalid_declaration_rejects_complete_manifest(self, path: str) -> None:
        body = f"## Files to Modify\n- `src/main.py`\n- `{path}`\n"
        assert parse_publication_scope_files(body) == set()

    def test_file_subheading_ignores_prose_and_other_sections(self) -> None:
        body = (
            "## Files to Modify\n### `src/main.py`\n"
            "Replace the call at `src/main.py:142` with `os.replace`.\n"
            "## Verification\n### Files to Modify\n- `unrelated/file.py`\n"
        )
        assert parse_publication_scope_files(body) == {"src/main.py"}

    @pytest.mark.parametrize("fence", ["```", "~~~~"])
    def test_fenced_examples_do_not_declare_files(self, fence: str) -> None:
        body = (
            f"{fence}markdown\n## Files to Modify\n- `example/file.py`\n{fence}\n"
            "## Files to Modify\n- `src/main.py`\n"
            f"{fence}\n- `other/example.py`\n{fence}\n"
        )
        assert parse_publication_scope_files(body) == {"src/main.py"}

    @pytest.mark.parametrize(
        "entry",
        [
            "|`.github/workflows/test.yml`|Update.|",
            "-\t`.github/workflows/test.yml`",
            "1.\t`.github/workflows/test.yml`",
        ],
    )
    def test_markdown_declaration_spacing(self, entry: str) -> None:
        body = f"## Files to Modify\n- `src/main.py`\n{entry}\n"
        assert parse_publication_scope_files(body) == {"src/main.py", ".github/workflows/test.yml"}

    def test_heading_suffix_does_not_grant_scope(self) -> None:
        body = "## Files to Modify Examples\n- `example/file.py`\n"
        assert parse_publication_scope_files(body) == set()


class TestFetchPlannedFiles:
    """Read the complete actor-owned plan through the repository accessor."""

    @staticmethod
    def read_files(
        issue: int, comments: list[dict[str, Any]], *, repo: tuple[str, str] = ("owner", "repo")
    ) -> set[str] | None:
        """Serve a paged journal to the real bounded accessor."""
        from hephaestus.automation.pipeline.admission import _fetch_planned_files

        def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            if argv == ["api", "user", "--jq", ".login"]:
                body = "bot"
            else:
                assert f"/repos/{repo[0]}/{repo[1]}/issues/{issue}/comments" in argv[1]
                page = int(argv[1].rsplit("page=", 1)[1])
                body = json.dumps(comments[(page - 1) * 100 : page * 100])
            return subprocess.CompletedProcess(argv, 0, stdout=body)

        github = PipelineGitHub(repo[0], repo=repo[1], command_runner=run)
        return _fetch_planned_files(
            issue, github=github, deadline_s=time.monotonic() + 10, shutdown=threading.Event()
        )

    def test_no_plan_comment_returns_none(self) -> None:
        comments = [
            {"body": "A normal comment.", "user": {"login": "other"}},
            {"body": "## Plan Review", "user": {"login": "bot"}},
        ]
        assert self.read_files(101, comments) is None

    def test_empty_comment_list_returns_none(self) -> None:
        assert self.read_files(102, []) is None

    def test_owned_plan_returns_file_set(self) -> None:
        comments = [
            {
                "body": render_current_plan("## Files to Modify\n- `src/worker.py`"),
                "user": {"login": "bot"},
            }
        ]
        assert self.read_files(103, comments) == {"src/worker.py"}

    @pytest.mark.parametrize("preceding_count", [0, 100])
    def test_foreign_marker_is_a_typed_conflict(self, preceding_count: int) -> None:
        comments = [
            {"body": f"Comment {index}.", "user": {"login": "other"}}
            for index in range(preceding_count)
        ]
        comments.append(
            {
                "body": render_current_plan("## Files to Modify\n- `src/worker.py`"),
                "user": {"login": "other"},
            }
        )
        with pytest.raises(CommentAliasConflictError, match="plan marker identity conflict"):
            self.read_files(104, comments)

    def test_selected_repository_owns_the_comment_read(self) -> None:
        assert self.read_files(188, [], repo=("HomericIntelligence", "Myrmidons")) is None


class TestOrderForImplementation:
    """Topological-order gating: dependencies dispatch before dependents."""

    def test_dependency_ordered_before_dependent(self) -> None:
        """#A depends on #B → B precedes A regardless of input order."""
        order = order_for_implementation([_info(10, dependencies=[20]), _info(20)])
        assert order.index(20) < order.index(10)

    def test_uses_public_dependency_api(self) -> None:
        """The admission layer routes dependency edges through the resolver API."""

        class FakeDependencyResolver:
            """Resolver double that fails if callers reach into ``graph`` directly."""

            instances: ClassVar[list[FakeDependencyResolver]] = []

            class Graph:
                def add_dependency(self, issue_number: int, depends_on: int) -> None:
                    pytest.fail(
                        "order_for_implementation must call DependencyResolver.add_dependency()"
                    )

            def __init__(self, skip_closed: bool = True) -> None:
                self.skip_closed = skip_closed
                self.graph = self.Graph()
                self.add_issue_calls: list[int] = []
                self.add_dependency_calls: list[tuple[int, int]] = []
                self.topological_sort_result = [20, 10]
                type(self).instances.append(self)

            def add_issue(self, issue: IssueInfo) -> None:
                self.add_issue_calls.append(issue.number)

            def add_dependency(self, issue_number: int, depends_on: int) -> None:
                self.add_dependency_calls.append((issue_number, depends_on))

            def topological_sort(self) -> list[int]:
                return self.topological_sort_result

        FakeDependencyResolver.instances = []

        with patch(
            "hephaestus.automation.pipeline.admission.DependencyResolver",
            FakeDependencyResolver,
        ):
            order = order_for_implementation([_info(10, dependencies=[20]), _info(20)])

        assert order == [20, 10]
        assert FakeDependencyResolver.instances[0].add_dependency_calls == [(10, 20)]

    def test_no_dependencies_preserves_input_order(self) -> None:
        """Independent issues keep their dispatch-priority (input) order."""
        assert order_for_implementation([_info(3), _info(1), _info(2)]) == [3, 1, 2]

    def test_out_of_set_dependency_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        """An absent dependency cannot create a false cycle or reorder queued work."""
        # #5 depends on #999 which is not admitted — #5 must still be ordered.
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.pipeline.admission"):
            order = order_for_implementation([_info(5, dependencies=[999]), _info(6)])
        assert order == [5, 6]
        assert not any("dependency cycle" in record.message for record in caplog.records)

    def test_chain_fully_ordered(self) -> None:
        """A → B → C chain sorts leaf-dependency first."""
        order = order_for_implementation(
            [_info(1, dependencies=[2]), _info(2, dependencies=[3]), _info(3)]
        )
        assert order == [3, 2, 1]

    def test_newly_ready_input_priority_precedes_lower_priority_peer(self) -> None:
        """A newly ready high-priority dependent is not stranded behind a peer."""
        order = order_for_implementation([_info(1, dependencies=[2]), _info(2), _info(3)])
        assert order == [2, 1, 3]

    def test_cycle_falls_open_to_input_order(self, caplog: pytest.LogCaptureFixture) -> None:
        """A dependency cycle keeps input order and warns (never wedges the queue)."""
        infos = [_info(1, dependencies=[2]), _info(2, dependencies=[1]), _info(3)]
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.pipeline.admission"):
            order = order_for_implementation(infos)
        assert order == [1, 2, 3]
        assert any("dependency cycle" in record.message for record in caplog.records)


class TestDependencyReadiness:
    """Live dependency facts decide implementation readiness."""

    class GitHub:
        """Return one scripted batch and record the exact request."""

        def __init__(
            self,
            facts: tuple[DependencyFact, ...] | Exception,
        ) -> None:
            self.facts = facts
            self.requests: list[tuple[int, ...]] = []

        def batch_dependency_facts(
            self,
            issue_numbers: tuple[int, ...],
            *,
            deadline_s: float,
            shutdown: threading.Event | None = None,
        ) -> tuple[DependencyFact, ...]:
            """Return the configured complete result or raise its error."""
            assert deadline_s > 0
            assert shutdown is None or isinstance(shutdown, threading.Event)
            self.requests.append(issue_numbers)
            if isinstance(self.facts, Exception):
                raise self.facts
            return self.facts

    @pytest.mark.parametrize(
        ("fact", "expected"),
        [
            (DependencyFact(10, "Issue", "CLOSED"), None),
            (DependencyFact(10, "PullRequest", "MERGED", True), None),
            (DependencyFact(10, "Issue", "OPEN"), "dependency #10 is still open"),
            (
                DependencyFact(10, "PullRequest", "OPEN", False),
                "dependency #10 has an open pull request",
            ),
            (
                DependencyFact(10, "PullRequest", "CLOSED", False),
                "dependency #10 has a closed unmerged pull request",
            ),
        ],
    )
    def test_issue_and_pr_lifecycle(self, fact: DependencyFact, expected: str | None) -> None:
        """Only a closed issue or a merged PR satisfies a dependency."""
        github = self.GitHub((fact,))

        assert dependency_block_reason((10,), github, deadline_s=10.0) == expected
        assert github.requests == [(10,)]

    def test_multiple_pending_reasons_have_canonical_order(self) -> None:
        """One result lists each pending dependency in number order."""
        github = self.GitHub(
            (
                DependencyFact(10, "PullRequest", "CLOSED", False),
                DependencyFact(20, "Issue", "OPEN"),
            )
        )

        assert dependency_block_reason((10, 20), github, deadline_s=10.0) == (
            "dependency #10 has a closed unmerged pull request; dependency #20 is still open"
        )
        assert github.requests == [(10, 20)]

    @pytest.mark.parametrize(
        "facts",
        [
            (),
            (DependencyFact(20, "Issue", "CLOSED"),),
            RuntimeError("GraphQL failed"),
        ],
        ids=["partial", "mismatched", "read-failure"],
    )
    def test_incomplete_or_unverifiable_facts_fail_closed(
        self,
        facts: tuple[DependencyFact, ...] | Exception,
    ) -> None:
        """Incomplete external evidence cannot authorize an agent turn."""
        github = self.GitHub(facts)

        assert dependency_block_reason((10,), github, deadline_s=10.0) == (
            "dependency #10 state could not be verified"
        )

    def test_external_error_text_is_not_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """An external diagnostic cannot put its content in the host log."""
        github = self.GitHub(RuntimeError("credential-bearing diagnostic"))

        with caplog.at_level(logging.WARNING):
            reason = dependency_block_reason((10,), github, deadline_s=10.0)

        assert reason == "dependency #10 state could not be verified"
        assert "RuntimeError" in caplog.text
        assert "credential-bearing diagnostic" not in caplog.text

    def test_duplicate_inputs_fail_closed_before_external_read(self) -> None:
        """A duplicate declaration cannot create an ambiguous batch."""
        github = self.GitHub(())

        assert dependency_block_reason((10, 10), github, deadline_s=10.0) == (
            "dependencies could not be verified"
        )
        assert github.requests == []


class TestFilterOpenIssues:
    """Exclude confirmed closed rows and retain unknown repository state."""

    @staticmethod
    def select(
        issues: list[int],
        states: Mapping[int, str | Exception],
        *,
        repo: tuple[str, str] = ("owner", "repo"),
    ) -> list[int]:
        """Serve explicit issue state through the real repository accessor."""

        def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert argv[-2:] == ["--repo", f"{repo[0]}/{repo[1]}"]
            number = int(argv[2])
            state = states[number]
            if isinstance(state, Exception):
                raise state
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"number": number, "state": state})
            )

        github = PipelineGitHub(repo[0], repo=repo[1], command_runner=run)
        return _filter_open_issues(
            repo,
            issues,
            github=github,
            deadline_s=time.monotonic() + 10,
            shutdown=threading.Event(),
        )

    def test_keeps_open_issues(self) -> None:
        assert self.select([1, 2, 3], dict.fromkeys([1, 2, 3], "OPEN")) == [1, 2, 3]

    def test_excludes_confirmed_closed_issues(self) -> None:
        assert self.select([1, 2, 3], {1: "OPEN", 2: "CLOSED", 3: "OPEN"}) == [1, 3]

    def test_keeps_rows_on_transport_error(self) -> None:
        states = dict.fromkeys([1, 2, 3], RuntimeError("API unavailable"))
        assert self.select([1, 2, 3], states) == [1, 2, 3]

    def test_preserves_source_order(self) -> None:
        states = {1: "OPEN", 2: "CLOSED", 3: "OPEN", 4: "CLOSED", 5: "OPEN"}
        assert self.select([1, 2, 3, 4, 5], states) == [1, 3, 5]

    def test_reads_the_selected_repository(self) -> None:
        assert self.select([1, 2], {1: "OPEN", 2: "CLOSED"}, repo=("target", "project")) == [1]

    def test_keeps_unverified_rows_after_a_closed_row(self) -> None:
        assert self.select([1, 2], {1: "CLOSED", 2: RuntimeError("NOT_FOUND")}) == [2]
