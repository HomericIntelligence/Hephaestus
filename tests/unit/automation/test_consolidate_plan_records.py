"""CLI behavior of scripts/consolidate_plan_records.py (#3307).

The command is dry-run by default and mutates nothing. ``--apply`` runs the
ordered consolidation against real ``github_api`` callables, so these tests
replace the ``github_api`` surface with one in-memory GitHub and assert the
mutation order, the drift abort, the latch abort, and exact replay.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import github_api
from hephaestus.automation.comment_identity import marker_aliases_in_body
from hephaestus.automation.issue_timeline import issue_comments_from_metadata
from hephaestus.automation.plan_consolidation import planning_role_inventory
from hephaestus.automation.protocol import comment_marker_aliases
from hephaestus.automation.review_journal import render_current_plan, render_current_review

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "consolidate_plan_records.py"
_ACTOR = "mvillmow"
_ISSUE = 705
_REPO = ("LLM360", "comet")

_ATHENA_PLAN_BODY = (
    "<!-- athena:plan-issue -->\n# Implementation Plan\n\nAthena-era plan requirements\n"
)
_ATHENA_REVIEW_BODY = "<!-- athena:issue-review -->\n## Plan Review\n\nAthena-era review findings\n"
_SHARED_PLAN_BODY = render_current_plan("Hephaestus plan requirements", revision=3)
_SHARED_REVIEW_BODY = render_current_review("Hephaestus review findings", revision=3)
_MERGED_PLAN = "One merged plan that preserves both plans' requirements.\n"
_MERGED_REVIEW = "Merged review findings without a verdict token.\n"


def _load_script() -> Any:
    """Load the script module without importing ``scripts`` as a package."""
    spec = importlib.util.spec_from_file_location("consolidate_plan_records", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MemoryGitHub:
    """One in-memory issue with the ``github_api`` surface the script uses."""

    def __init__(
        self,
        comments: list[tuple[int, str]] | None = None,
        labels: tuple[str, ...] = ("state:plan-no-go",),
        issue_body: str = "Issue requirements body\n",
    ) -> None:
        self._next_id = 5446240000
        self._comments: list[tuple[int, str, str, bool]] = []
        for comment_id, body in comments or []:
            self._comments.append((comment_id, body, _ACTOR, True))
        self.labels = set(labels)
        self.issue_body = issue_body
        self.mutations: list[str] = []

    def fetch_issue_comments_metadata(
        self, issue_number: int, repo: tuple[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """Return the journal as raw REST metadata."""
        return [
            {
                "databaseId": comment_id,
                "body": body,
                "user": {"login": login},
                "author_association": "MEMBER",
            }
            for comment_id, body, login, _owned in self._comments
        ]

    def gh_issue_json(
        self, issue_number: int, repo: tuple[str, str] | None = None
    ) -> dict[str, Any]:
        """Return the issue payload with its body and labels."""
        return {
            "number": issue_number,
            "state": "OPEN",
            "body": self.issue_body,
            "labels": [{"name": label} for label in sorted(self.labels)],
        }

    def gh_issue_comment(
        self, issue_number: int, body: str, repo: tuple[str, str] | None = None
    ) -> None:
        """Append one comment owned by the actor."""
        self.mutations.append("append")
        self._comments.append((self._next_id, body, _ACTOR, True))
        self._next_id += 1

    def gh_issue_delete_comment(
        self,
        comment_id: int,
        repo: tuple[str, str] | None = None,
        *,
        missing_ok: bool = False,
    ) -> None:
        """Delete one comment by id."""
        self.mutations.append(f"delete:{comment_id}")
        self._comments = [comment for comment in self._comments if comment[0] != comment_id]

    def gh_issue_upsert_owned_comment(
        self,
        issue_number: int,
        marker_prefix: str,
        body: str,
        repo: tuple[str, str] | None = None,
    ) -> int | None:
        """Patch the actor's newest comment for one role marker, or create it."""
        self.mutations.append(f"upsert:{marker_prefix}")
        aliases = comment_marker_aliases(marker_prefix)
        matches = [
            (index, comment)
            for index, comment in enumerate(self._comments)
            if comment[3] and marker_aliases_in_body(comment[1], aliases)
        ]
        if matches:
            index, comment = matches[-1]
            _comment_id, _body, login, owned = comment
            self._comments[index] = (comment[0], body, login, owned)
            return comment[0]
        self.gh_issue_comment(issue_number, body, repo=repo)
        return self._next_id - 1

    def gh_issue_edit_labels(
        self,
        issue_number: int,
        *,
        add: list[str],
        remove: list[str],
        repo: tuple[str, str] | None = None,
    ) -> None:
        """Apply one label add/remove transition."""
        self.mutations.append(f"labels:{sorted(add)}/{sorted(remove)}")
        self.labels -= set(remove)
        self.labels |= set(add)

    def gh_current_login(self, *args: Any, **kwargs: Any) -> str:
        """Return the authenticated login."""
        return _ACTOR

    def get_repo_info(self) -> tuple[str, str]:
        """Return the repository the issue lives in."""
        return _REPO

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bind this fake to the ``github_api`` surface."""
        for name in (
            "fetch_issue_comments_metadata",
            "gh_issue_json",
            "gh_issue_comment",
            "gh_issue_delete_comment",
            "gh_issue_upsert_owned_comment",
            "gh_issue_edit_labels",
            "gh_current_login",
            "get_repo_info",
        ):
            monkeypatch.setattr(github_api, name, getattr(self, name))

    def owned_claims(self) -> list[tuple[str, int]]:
        """Return the current owned plan/review claims."""
        metadata = self.fetch_issue_comments_metadata(_ISSUE)
        inventory = planning_role_inventory(
            issue_comments_from_metadata(metadata, viewer_login=_ACTOR)
        )
        return [(claim.role, claim.comment_id) for claim in inventory if claim.owned]


@pytest.fixture()
def journal_fake() -> MemoryGitHub:
    """Build the #705 shape: two same-actor pairs across two alias families."""
    return MemoryGitHub(
        [
            (5444284559, _ATHENA_PLAN_BODY),
            (5444310901, _ATHENA_REVIEW_BODY),
            (5446247620, _SHARED_PLAN_BODY),
            (5446247883, _SHARED_REVIEW_BODY),
        ]
    )


@pytest.fixture()
def content_files(tmp_path: Path) -> tuple[Path, Path]:
    """Write the operator-merged plan and review content files."""
    plan_file = tmp_path / "merged-plan.md"
    review_file = tmp_path / "merged-review.md"
    plan_file.write_text(_MERGED_PLAN, encoding="utf-8")
    review_file.write_text(_MERGED_REVIEW, encoding="utf-8")
    return plan_file, review_file


def _argv(plan_file: Path, review_file: Path, *, apply: bool = False) -> list[str]:
    """Build the CLI arguments for the #705 request."""
    argv = [
        "--repo",
        f"{_REPO[0]}/{_REPO[1]}",
        "--issue",
        str(_ISSUE),
        "--keep-plan-comment",
        "5446247620",
        "--keep-review-comment",
        "5446247883",
        "--supersede-plan-comment",
        "5444284559",
        "--supersede-review-comment",
        "5444310901",
        "--plan-body-file",
        str(plan_file),
        "--review-body-file",
        str(review_file),
    ]
    if apply:
        argv.append("--apply")
    return argv


def test_dry_run_performs_zero_mutations(
    journal_fake: MemoryGitHub,
    content_files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The dry run prints the plan and performs no mutation."""
    journal_fake.install(monkeypatch)
    script = _load_script()

    exit_code = script.main(_argv(*content_files))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "dry run: nothing written" in out
    assert journal_fake.mutations == []
    assert ("plan", 5446247620) in journal_fake.owned_claims()


def test_apply_runs_archives_deletes_upserts_in_order(
    journal_fake: MemoryGitHub,
    content_files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Apply archives both superseded records, then deletes, then upserts."""
    journal_fake.install(monkeypatch)
    script = _load_script()

    exit_code = script.main(_argv(*content_files, apply=True))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "consolidated at epoch 4" in out
    mutations = journal_fake.mutations
    first_delete = next(index for index, m in enumerate(mutations) if m.startswith("delete"))
    first_upsert = next(index for index, m in enumerate(mutations) if m.startswith("upsert"))
    assert mutations[:first_delete].count("append") == 2
    assert first_delete < first_upsert
    assert not any(mutation.startswith("labels:") for mutation in mutations)
    assert journal_fake.owned_claims() == [("plan", 5446247620), ("review", 5446247883)]


def test_second_identical_apply_is_a_noop(
    journal_fake: MemoryGitHub,
    content_files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A completed replay reports already consolidated without mutations."""
    journal_fake.install(monkeypatch)
    script = _load_script()

    assert script.main(_argv(*content_files, apply=True)) == 0
    capsys.readouterr()
    mutations = len(journal_fake.mutations)
    exit_code = script.main(_argv(*content_files, apply=True))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "already consolidated" in out
    assert len(journal_fake.mutations) == mutations
    assert journal_fake.owned_claims() == [("plan", 5446247620), ("review", 5446247883)]


def test_apply_transitions_a_historical_go(
    content_files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A historical GO label moves to plan-no-go and is never transferred."""
    fake = MemoryGitHub(
        [
            (5444284559, _ATHENA_PLAN_BODY),
            (5444310901, _ATHENA_REVIEW_BODY),
            (5446247620, _SHARED_PLAN_BODY),
            (5446247883, _SHARED_REVIEW_BODY),
        ],
        labels=("state:plan-go",),
    )
    fake.install(monkeypatch)
    script = _load_script()

    exit_code = script.main(_argv(*content_files, apply=True))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "labels_edited=True" in out
    assert fake.labels == {"state:plan-no-go"}


def test_drift_abort_names_the_changed_comment(
    journal_fake: MemoryGitHub,
    content_files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A changed keep comment aborts before any mutation and names its id."""
    journal_fake.install(monkeypatch)
    script = _load_script()

    original_fetch = journal_fake.fetch_issue_comments_metadata
    calls = {"count": 0}

    def drifting_fetch(issue_number: int, repo: Any = None) -> list[dict[str, Any]]:
        """Drift one keep comment after the plan was computed."""
        calls["count"] += 1
        metadata = original_fetch(issue_number, repo)
        if calls["count"] >= 3:
            metadata = [
                dict(comment, body=render_current_plan("Tampered plan", revision=3))
                if comment["databaseId"] == 5446247620
                else comment
                for comment in metadata
            ]
        return metadata

    monkeypatch.setattr(github_api, "fetch_issue_comments_metadata", drifting_fetch)

    exit_code = script.main(_argv(*content_files, apply=True))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "5446247620" in captured.err
    assert all(not mutation.startswith("delete") for mutation in journal_fake.mutations)


def test_latch_label_aborts(
    content_files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An operator latch label refuses the consolidation outright."""
    fake = MemoryGitHub(
        [
            (5444284559, _ATHENA_PLAN_BODY),
            (5444310901, _ATHENA_REVIEW_BODY),
            (5446247620, _SHARED_PLAN_BODY),
            (5446247883, _SHARED_REVIEW_BODY),
        ],
        labels=("state:skip",),
    )
    fake.install(monkeypatch)
    script = _load_script()

    exit_code = script.main(_argv(*content_files, apply=True))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "state:skip" in captured.err
    assert fake.mutations == []


def test_missing_content_file_fails_without_mutations(
    journal_fake: MemoryGitHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unreadable merged-content file fails closed."""
    journal_fake.install(monkeypatch)
    script = _load_script()

    argv = _argv(tmp_path / "absent-plan.md", tmp_path / "absent-review.md", apply=True)
    exit_code = script.main(argv)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "cannot read" in captured.err
    assert journal_fake.mutations == []


def test_help_works_without_github(capsys: pytest.CaptureFixture[str]) -> None:
    """The parser is module-level, so --help needs no GitHub access."""
    script = _load_script()

    with pytest.raises(SystemExit) as raised:
        script.main(["--help"])

    assert raised.value.code == 0
    assert "consolidate" in capsys.readouterr().out.lower()
