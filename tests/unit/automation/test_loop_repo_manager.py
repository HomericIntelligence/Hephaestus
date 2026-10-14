"""Test bounded repository and issue discovery."""

from __future__ import annotations

import json
import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import loop_repo_manager
from hephaestus.automation.loop_repo_manager import _detect_cwd_repo
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT


class TestOrgRepoSource:
    """Bounded organization-repository source contracts."""

    def test_pages_are_fetched_only_when_the_prior_page_is_consumed(self) -> None:
        first_page = [
            {"name": f"repo-{number:03d}", "fork": False, "archived": False}
            for number in range(100)
        ]
        second_page = [{"name": "repo-100", "fork": False, "archived": False}]
        with patch(
            "hephaestus.automation.loop_repo_manager.gh_call",
            side_effect=[
                MagicMock(stdout=json.dumps(first_page)),
                MagicMock(stdout=json.dumps(second_page)),
            ],
        ) as mock_gh:
            source = loop_repo_manager._iter_gh_repos("acme", shutdown=threading.Event())
            first = [next(source) for _ in range(100)]
            assert mock_gh.call_count == 1
            tail = list(source)

        assert first + tail == [f"repo-{number:03d}" for number in range(100)] + ["repo-100"]
        assert mock_gh.call_args_list[1].args[0][-1].endswith("page=2")

    @pytest.mark.parametrize(
        "entry",
        [
            {"name": "missing-flags"},
            {"name": "not-a-bool", "fork": "false", "archived": False},
            {"name": "not-a-bool", "fork": False, "archived": 0},
        ],
    )
    def test_rejects_malformed_rest_repository_flags(self, entry: dict[str, object]) -> None:
        with (
            patch(
                "hephaestus.automation.loop_repo_manager.gh_call",
                return_value=MagicMock(stdout=json.dumps([entry])),
            ),
            pytest.raises(RuntimeError, match="malformed flags"),
        ):
            list(loop_repo_manager._iter_gh_repos("acme", shutdown=threading.Event()))


class TestListOpenIssueMeta:
    """Tests for uncapped open-issue metadata discovery."""

    def test_yields_page_at_a_time_without_paginate_or_slurp(self) -> None:
        """The cursor pulls the next page only after the current page is consumed."""
        pages = [
            [
                {"number": number, "labels": [{"name": "bug"}], "title": f"Issue {number}"}
                for number in range(1, 101)
            ],
            [{"number": 101, "labels": [], "title": "Issue 101"}],
        ]
        with patch(
            "hephaestus.automation.loop_repo_manager.gh_call",
            side_effect=[MagicMock(stdout=json.dumps(page)) for page in pages],
        ) as mock_gh:
            result = loop_repo_manager._iter_open_issue_meta(
                "acme", "widget", shutdown=threading.Event()
            )
            first = [next(result) for _ in range(100)]
            assert mock_gh.call_count == 1
            tail = list(result)

        assert [entry["number"] for entry in first + tail] == list(range(1, 102))
        assert first[0] == {"number": 1, "labels": ["bug"], "title": "Issue 1"}
        assert tail[-1] == {"number": 101, "labels": [], "title": "Issue 101"}
        assert mock_gh.call_args_list[0].args[0] == [
            "api",
            "/repos/acme/widget/issues?state=open&per_page=100&sort=created&direction=asc&page=1",
        ]
        assert mock_gh.call_args_list[1].args[0] == [
            "api",
            "/repos/acme/widget/issues?state=open&per_page=100&sort=created&direction=asc&page=2",
        ]
        assert all(call.kwargs["timeout"] == NETWORK_TIMEOUT for call in mock_gh.call_args_list)

    @pytest.mark.parametrize(
        "page",
        [
            {"not": "a list"},
            [{"number": "not-an-int", "labels": [], "title": "broken"}],
            [{"number": 1, "labels": ["not-an-object"], "title": "broken"}],
        ],
    )
    def test_fails_closed_on_malformed_page(self, page: object) -> None:
        """A malformed page cannot masquerade as an empty repository."""
        with patch(
            "hephaestus.automation.loop_repo_manager.gh_call",
            return_value=MagicMock(stdout=json.dumps(page)),
        ):
            with pytest.raises(RuntimeError, match="failed to list open issues"):
                list(
                    loop_repo_manager._iter_open_issue_meta(
                        "acme", "widget", shutdown=threading.Event()
                    )
                )


class TestDetectCwdRepo:
    """Tests for _detect_cwd_repo URL parsing logic."""

    def test_returns_none_tuple_when_not_in_git_repo(self) -> None:
        with patch(
            "hephaestus.automation.loop_repo_manager.subprocess.run",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            result = _detect_cwd_repo()
        assert result == (None, None)

    def test_parses_https_url(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/MyRepo\n"
            else:
                m.stdout = "https://github.com/MyOrg/MyRepo.git\n"
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org == "MyOrg"
        assert repo == "MyRepo"

    def test_git_probes_use_metadata_timeout(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/MyRepo\n"
            else:
                m.stdout = "https://github.com/MyOrg/MyRepo.git\n"
            return m

        with patch(
            "hephaestus.automation.loop_repo_manager.subprocess.run",
            side_effect=fake_run,
        ) as mock_run:
            _detect_cwd_repo()

        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            assert call.kwargs["timeout"] == METADATA_TIMEOUT

    def test_parses_repo_name_from_https_remote_not_worktree_dir(self) -> None:
        """GitHub remote path is authoritative when worktree dir is issue-named."""

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/Hephaestus/build/.worktrees/issue-1442\n"
            else:
                m.stdout = "https://github.com/HomericIntelligence/Hephaestus.git\n"
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org == "HomericIntelligence"
        assert repo == "Hephaestus"

    def test_parses_ssh_scp_url(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/ProjectFoo\n"
            else:
                m.stdout = "git@github.com:MyOrg/ProjectFoo.git\n"
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org == "MyOrg"
        assert repo == "ProjectFoo"

    def test_parses_repo_name_from_ssh_remote_not_worktree_dir(self) -> None:
        """SCP-style GitHub remotes also override local worktree basenames."""

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/Hephaestus/build/.worktrees/issue-1442\n"
            else:
                m.stdout = "git@github.com:HomericIntelligence/Hephaestus.git\n"
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org == "HomericIntelligence"
        assert repo == "Hephaestus"

    def test_returns_none_org_for_non_github_remote(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            m = MagicMock()
            if "rev-parse" in cmd:
                m.stdout = "/home/user/repos/SomeRepo\n"
            else:
                m.stdout = "https://gitlab.com/org/repo.git\n"
            return m

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org is None
        assert repo == "SomeRepo"

    def test_returns_none_org_when_remote_url_fetch_fails(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            if "rev-parse" in cmd:
                m = MagicMock()
                m.stdout = "/home/user/repos/SomeRepo\n"
                return m
            raise subprocess.CalledProcessError(128, cmd)

        with patch("hephaestus.automation.loop_repo_manager.subprocess.run", side_effect=fake_run):
            org, repo = _detect_cwd_repo()
        assert org is None
        assert repo == "SomeRepo"
