#!/usr/bin/env python3
"""Tests for git utilities."""

import pytest

from hephaestus.git.changelog import categorize_commits, parse_commit


class TestChangelogUtils:
    """Test changelog generation utilities."""

    def test_parse_commit_conventional(self):
        """Test parsing conventional commit messages."""
        # Standard format: hash|subject|author
        commit = "abc123|feat(api): Add new endpoint|John Doe"
        hash_val, commit_type, scope, message = parse_commit(commit)

        assert hash_val == "abc123"
        assert commit_type == "feat"
        assert scope == "api"
        assert message == "Add new endpoint"

    def test_parse_commit_without_scope(self):
        """Test parsing commit without scope."""
        commit = "def456|fix: Resolve bug|Jane Smith"
        hash_val, commit_type, scope, message = parse_commit(commit)

        assert hash_val == "def456"
        assert commit_type == "fix"
        assert scope == ""
        assert message == "Resolve bug"

    def test_parse_commit_non_conventional(self):
        """Test parsing non-conventional commit."""
        commit = "ghi789|Random commit message|Bob Johnson"
        hash_val, commit_type, scope, message = parse_commit(commit)

        assert hash_val == "ghi789"
        assert commit_type == "other"
        assert scope == ""
        assert message == "Random commit message"

    def test_categorize_commits(self):
        """Test categorizing commits by type."""
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

        # Check feature commits
        features = categories["Features"]
        assert ("abc123", "api", "Add endpoint") in features
        assert ("ghi789", "ui", "New button") in features

    def test_categorize_empty_commits(self):
        """Test categorizing empty commit list."""
        categories = categorize_commits([])
        assert categories == {}

    def test_categorize_with_blank_lines(self):
        """Test categorizing with blank lines in commits."""
        commits = [
            "abc123|feat: Feature|John",
            "",
            "def456|fix: Fix|Jane",
        ]

        categories = categorize_commits(commits)
        assert len(categories["Features"]) == 1
        assert len(categories["Bug Fixes"]) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
