#!/usr/bin/env python3
"""Tests for git utilities."""

from unittest.mock import patch

from hephaestus.git.changelog import (
    categorize_commits,
    generate_changelog,
    get_commits_between,
    parse_commit,
)


class TestParseCommit:
    """Tests for parse_commit."""

    def test_parse_conventional_commit(self):
        """Parse a standard conventional commit."""
        commit = "abc123|feat(api): Add new endpoint|John Doe"
        hash_val, commit_type, scope, message = parse_commit(commit)
        assert hash_val == "abc123"
        assert commit_type == "feat"
        assert scope == "api"
        assert message == "Add new endpoint"

    def test_parse_commit_without_scope(self):
        """Parse commit without scope."""
        commit = "def456|fix: Resolve bug|Jane Smith"
        hash_val, commit_type, scope, message = parse_commit(commit)
        assert hash_val == "def456"
        assert commit_type == "fix"
        assert scope == ""
        assert message == "Resolve bug"

    def test_parse_non_conventional_commit(self):
        """Parse a non-conventional commit."""
        commit = "ghi789|Random commit message|Bob Johnson"
        hash_val, commit_type, scope, message = parse_commit(commit)
        assert hash_val == "ghi789"
        assert commit_type == "other"
        assert scope == ""
        assert message == "Random commit message"

    def test_parse_commit_missing_parts(self):
        """Parse malformed commit line."""
        commit = "malformed line"
        hash_val, commit_type, _scope, message = parse_commit(commit)
        assert hash_val == ""
        assert commit_type == "other"
        assert message == "malformed line"

    def test_parse_commit_type_lowercased(self):
        """Commit type is always lowercased."""
        commit = "abc123|FEAT(api): Something|Author"
        _, commit_type, _, _ = parse_commit(commit)
        assert commit_type == "feat"

    def test_parse_refactor_commit(self):
        """Parse a refactor commit."""
        commit = "zzz999|refactor(core): Simplify logic|Dev"
        _, commit_type, scope, _ = parse_commit(commit)
        assert commit_type == "refactor"
        assert scope == "core"

    def test_parse_docs_commit(self):
        """Parse a docs commit."""
        commit = "aaa111|docs: Update README|Dev"
        _, commit_type, scope, message = parse_commit(commit)
        assert commit_type == "docs"
        assert scope == ""
        assert message == "Update README"

    def test_parse_commit_with_colon_in_message(self):
        """Handles message that contains a colon after the prefix."""
        commit = "abc123|feat(scope): Add feature: with colon|Author"
        _, _, _, message = parse_commit(commit)
        assert "with colon" in message


class TestCategorizeCommits:
    """Tests for categorize_commits."""

    def test_categorize_multiple_types(self):
        """Categorizes different commit types correctly."""
        commits = [
            "abc123|feat(api): Add endpoint|John",
            "def456|fix: Bug fix|Jane",
            "ghi789|feat(ui): New button|Bob",
            "jkl012|docs: Update README|Alice",
        ]
        categories = categorize_commits(commits)
        assert "Features" in categories
        assert "Bug Fixes" in categories
        assert "Documentation" in categories
        assert len(categories["Features"]) == 2
        assert len(categories["Bug Fixes"]) == 1
        assert len(categories["Documentation"]) == 1

    def test_categorize_empty_commits(self):
        """Empty commit list returns empty dict."""
        assert categorize_commits([]) == {}

    def test_categorize_with_blank_lines(self):
        """Blank lines are skipped."""
        commits = [
            "abc123|feat: Feature|John",
            "",
            "def456|fix: Fix|Jane",
        ]
        categories = categorize_commits(commits)
        assert len(categories["Features"]) == 1
        assert len(categories["Bug Fixes"]) == 1

    def test_categorize_unknown_type_goes_to_other(self):
        """Unknown commit type goes to 'Other' category."""
        commits = ["abc123|unknown: Something|Author"]
        categories = categorize_commits(commits)
        assert "Other" in categories

    def test_categorize_all_known_types(self):
        """All standard conventional commit types are categorized."""
        commits = [
            "a|feat: f|a",
            "b|fix: f|a",
            "c|perf: f|a",
            "d|docs: f|a",
            "e|refactor: f|a",
            "f|test: f|a",
            "g|ci: f|a",
            "h|chore: f|a",
            "i|build: f|a",
            "j|style: f|a",
        ]
        categories = categorize_commits(commits)
        assert "Features" in categories
        assert "Bug Fixes" in categories
        assert "Performance" in categories
        assert "Documentation" in categories
        assert "Refactoring" in categories
        assert "Testing" in categories
        assert "CI/CD" in categories
        assert "Maintenance" in categories
        assert "Build" in categories
        assert "Style" in categories

    def test_categorize_returns_tuples(self):
        """Each category entry is a (hash, scope, message) tuple."""
        commits = ["abc123|feat(api): Add endpoint|Author"]
        categories = categorize_commits(commits)
        entry = categories["Features"][0]
        assert len(entry) == 3
        assert entry[0] == "abc123"
        assert entry[1] == "api"
        assert entry[2] == "Add endpoint"


class TestGenerateChangelog:
    """Tests for generate_changelog."""

    @patch("hephaestus.git.changelog.get_commits_between")
    @patch("hephaestus.git.changelog.get_previous_tag")
    def test_generates_changelog_with_commits(self, mock_prev_tag, mock_commits):
        """Generates changelog content with commit data."""
        mock_prev_tag.return_value = "v0.2.0"
        mock_commits.return_value = [
            "abc123|feat(core): New feature|Author",
            "def456|fix: Important bugfix|Author",
        ]

        result = generate_changelog("v0.3.0")
        assert "v0.3.0" in result
        assert "Features" in result
        assert "New feature" in result
        assert "Bug Fixes" in result
        assert "Important bugfix" in result

    @patch("hephaestus.git.changelog.get_commits_between")
    @patch("hephaestus.git.changelog.get_previous_tag")
    def test_generates_changelog_no_commits(self, mock_prev_tag, mock_commits):
        """Changelog with no commits says 'No changes recorded'."""
        mock_prev_tag.return_value = None
        mock_commits.return_value = []

        result = generate_changelog("v0.3.0")
        assert "No changes recorded" in result

    @patch("hephaestus.git.changelog.get_commits_between")
    def test_uses_provided_from_ref(self, mock_commits):
        """Uses provided from_ref directly."""
        mock_commits.return_value = ["abc123|feat: Feature|Author"]
        generate_changelog("v0.3.0", from_ref="v0.2.0")
        mock_commits.assert_called_once_with("v0.2.0", "HEAD")

    @patch("hephaestus.git.changelog.get_commits_between")
    @patch("hephaestus.git.changelog.get_previous_tag")
    def test_scope_formatted_in_output(self, mock_prev_tag, mock_commits):
        """Scoped commits show scope in bold."""
        mock_prev_tag.return_value = None
        mock_commits.return_value = ["abc123|feat(api): My feature|Author"]
        result = generate_changelog("v0.3.0")
        assert "**api**:" in result


class TestGetCommitsBetween:
    """Tests for get_commits_between."""

    @patch("subprocess.run")
    def test_with_from_ref(self, mock_run):
        """Builds range spec with from_ref..to_ref."""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "abc|feat: thing|Author\n"
        result = get_commits_between("v0.1.0", "HEAD")
        assert len(result) == 1
        cmd_args = mock_run.call_args[0][0]
        assert "v0.1.0..HEAD" in cmd_args

    @patch("subprocess.run")
    def test_without_from_ref(self, mock_run):
        """Uses just to_ref when from_ref is None."""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        result = get_commits_between(None, "HEAD")
        assert result == []
        cmd_args = mock_run.call_args[0][0]
        assert "HEAD" in cmd_args

    @patch("subprocess.run")
    def test_empty_output_returns_empty_list(self, mock_run):
        """Empty git output returns empty list."""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        result = get_commits_between("v0.1.0")
        assert result == []
