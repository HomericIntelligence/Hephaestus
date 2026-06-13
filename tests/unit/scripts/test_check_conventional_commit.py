"""Tests for scripts/check_conventional_commit.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from check_conventional_commit import (
    _subjects_from_args,
    validate_subject,
)


class TestValidateSubject:
    """Tests for the ``validate_subject`` Conventional Commit parser."""

    def test_accepts_simple_type(self) -> None:
        assert validate_subject("fix: handle EOF") is None

    def test_accepts_type_with_scope(self) -> None:
        assert validate_subject("feat(io): add atomic write") is None

    def test_accepts_nested_paren_scope(self) -> None:
        assert validate_subject("feat(core(sub)): wire it") is None

    def test_accepts_breaking_marker_with_scope(self) -> None:
        assert validate_subject("feat(api)!: drop v1") is None

    def test_accepts_breaking_marker_no_scope(self) -> None:
        assert validate_subject("feat!: drop v1") is None

    def test_accepts_genuine_multi_colon_description(self) -> None:
        # Advise-named case: a colon INSIDE the description must not break parse.
        assert validate_subject("fix: url: handle https://example.com") is None

    def test_rejects_bracketed_prefix(self) -> None:  # the cited 67079cc3 form
        assert validate_subject("[FIX] Replace bash -c") is not None

    def test_rejects_unknown_type(self) -> None:
        assert validate_subject("wip: stuff") is not None

    def test_rejects_missing_colon(self) -> None:
        assert validate_subject("add a thing") is not None

    def test_rejects_empty_description(self) -> None:
        assert validate_subject("fix: ") is not None

    def test_rejects_empty_scope(self) -> None:
        assert validate_subject("fix(): x") is not None

    def test_rejects_empty_subject(self) -> None:
        assert validate_subject("") is not None

    def test_ignores_merge_and_revert_and_fixup(self) -> None:
        assert validate_subject("Merge branch 'main'") is None
        assert validate_subject('Revert "feat: x"') is None
        assert validate_subject("fixup! fix: x") is None


class TestSubjectsFromArgs:
    """Tests for the ``_subjects_from_args`` file/stdin entry-point split."""

    def test_reads_first_noncomment_line_from_msg_file(self, tmp_path: Path) -> None:
        f = tmp_path / "COMMIT_EDITMSG"
        f.write_text("feat(io): add thing\n\n# comment line\nbody text\n")
        assert _subjects_from_args([str(f)]) == ["feat(io): add thing"]

    def test_skips_leading_comment_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "COMMIT_EDITMSG"
        f.write_text("# pre-filled comment\nfix: real subject\n")
        assert _subjects_from_args([str(f)]) == ["fix: real subject"]
