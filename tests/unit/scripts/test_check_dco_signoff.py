"""Tests for scripts/check_dco_signoff.py."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from check_dco_signoff import _messages_from_args, main, validate_message

VALID_MESSAGE = "feat(x): add thing\n\nBody text.\n\nSigned-off-by: Jane Dev <jane@example.com>\n"
NO_SIGNOFF_MESSAGE = "feat(x): add thing\n\nBody text.\n"


class TestValidateMessage:
    """Tests for the ``validate_message`` DCO trailer validator."""

    def test_accepts_signoff_at_end(self) -> None:
        assert validate_message(VALID_MESSAGE) is None

    def test_accepts_signoff_in_body(self) -> None:
        msg = "feat(x): add thing\n\nSigned-off-by: Jane Dev <jane@example.com>\n\nMore text.\n"
        assert validate_message(msg) is None

    def test_accepts_multiple_signoffs(self) -> None:
        msg = (
            "feat(x): add thing\n\n"
            "Signed-off-by: Alice <alice@example.com>\n"
            "Signed-off-by: Bob <bob@example.com>\n"
        )
        assert validate_message(msg) is None

    def test_rejects_missing_signoff(self) -> None:
        err = validate_message(NO_SIGNOFF_MESSAGE)
        assert err is not None
        assert "Signed-off-by" in err

    def test_rejects_bare_signoff_keyword_only(self) -> None:
        msg = "feat(x): add thing\n\nSigned-off-by:\n"
        assert validate_message(msg) is not None

    def test_rejects_signoff_missing_email(self) -> None:
        msg = "feat(x): add thing\n\nSigned-off-by: Jane Dev\n"
        assert validate_message(msg) is not None

    def test_rejects_signoff_with_malformed_email_no_at(self) -> None:
        msg = "feat(x): add thing\n\nSigned-off-by: Jane Dev <janeexample.com>\n"
        assert validate_message(msg) is not None

    def test_rejects_signoff_missing_angle_brackets(self) -> None:
        msg = "feat(x): add thing\n\nSigned-off-by: Jane Dev jane@example.com\n"
        assert validate_message(msg) is not None

    def test_rejects_free_substring_in_description(self) -> None:
        # "Signed-off-by" in the description body must not be confused with a trailer.
        msg = "feat(x): Signed-off-by: Jane Dev <jane@example.com> in description\n"
        # The subject line contains "Signed-off-by" but it's not a standalone trailer line.
        # validate_message strips and checks each line; the subject line above starts
        # with "feat(x):" so it won't match the anchored regex.
        assert validate_message(msg) is not None

    def test_accepts_signoff_with_leading_whitespace_stripped(self) -> None:
        msg = "feat(x): add thing\n\n  Signed-off-by: Jane Dev <jane@example.com>\n"
        assert validate_message(msg) is None

    def test_rejects_empty_message(self) -> None:
        assert validate_message("") is not None

    def test_rejects_whitespace_only_message(self) -> None:
        assert validate_message("   \n\n   ") is not None

    def test_returns_subject_in_error(self) -> None:
        err = validate_message(NO_SIGNOFF_MESSAGE)
        assert err is not None
        assert "feat(x): add thing" in err


class TestMessagesFromArgs:
    """Tests for the ``_messages_from_args`` file/stdin entry-point split."""

    def test_reads_message_from_file(self, tmp_path: Path) -> None:
        f = tmp_path / "COMMIT_EDITMSG"
        f.write_text(VALID_MESSAGE)
        msgs = _messages_from_args([str(f)])
        assert len(msgs) == 1
        assert "Signed-off-by" in msgs[0]

    def test_strips_comment_lines_from_file(self, tmp_path: Path) -> None:
        f = tmp_path / "COMMIT_EDITMSG"
        f.write_text(
            "feat(x): add thing\n"
            "# This is a comment\n"
            "\n"
            "Signed-off-by: Jane Dev <jane@example.com>\n"
        )
        msgs = _messages_from_args([str(f)])
        assert len(msgs) == 1
        assert "# This is a comment" not in msgs[0]
        assert "Signed-off-by" in msgs[0]

    def test_empty_file_returns_empty_list(self, tmp_path: Path) -> None:
        f = tmp_path / "COMMIT_EDITMSG"
        f.write_text("")
        assert _messages_from_args([str(f)]) == []

    def test_comment_only_file_returns_empty_list(self, tmp_path: Path) -> None:
        f = tmp_path / "COMMIT_EDITMSG"
        f.write_text("# only comments\n# nothing here\n")
        assert _messages_from_args([str(f)]) == []

    def test_reads_nul_separated_messages_from_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        nul = "\x00"
        two_messages = VALID_MESSAGE + nul + VALID_MESSAGE + nul
        monkeypatch.setattr(sys, "stdin", io.StringIO(two_messages))
        msgs = _messages_from_args(["-"])
        assert len(msgs) == 2

    def test_reads_stdin_when_no_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        nul = "\x00"
        monkeypatch.setattr(sys, "stdin", io.StringIO(VALID_MESSAGE + nul))
        msgs = _messages_from_args([])
        assert len(msgs) == 1

    def test_empty_stdin_returns_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert _messages_from_args(["-"]) == []

    def test_drops_empty_nul_records(self, monkeypatch: pytest.MonkeyPatch) -> None:
        nul = "\x00"
        monkeypatch.setattr(sys, "stdin", io.StringIO(nul + nul + VALID_MESSAGE + nul))
        msgs = _messages_from_args(["-"])
        assert len(msgs) == 1


class TestMain:
    """End-to-end tests for ``main()`` including exit codes and output."""

    def test_passes_valid_message_file(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        f = tmp_path / "COMMIT_EDITMSG"
        f.write_text(VALID_MESSAGE)
        assert main([str(f)]) == 0
        assert "PASSED" in capsys.readouterr().out

    def test_fails_missing_signoff_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        f = tmp_path / "COMMIT_EDITMSG"
        f.write_text(NO_SIGNOFF_MESSAGE)
        assert main([str(f)]) == 1
        out = capsys.readouterr().out
        assert "FAILED" in out

    def test_fails_prints_remediation_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        f = tmp_path / "COMMIT_EDITMSG"
        f.write_text(NO_SIGNOFF_MESSAGE)
        main([str(f)])
        out = capsys.readouterr().out
        assert "git commit -s" in out

    def test_passes_valid_stdin(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        nul = "\x00"
        monkeypatch.setattr(sys, "stdin", io.StringIO(VALID_MESSAGE + nul))
        assert main(["-"]) == 0
        assert "PASSED" in capsys.readouterr().out

    def test_fails_invalid_stdin(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        nul = "\x00"
        monkeypatch.setattr(sys, "stdin", io.StringIO(NO_SIGNOFF_MESSAGE + nul))
        assert main(["-"]) == 1
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "git commit -s" in out

    def test_passes_multiple_valid_messages_stdin(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        nul = "\x00"
        two_valid = VALID_MESSAGE + nul + VALID_MESSAGE + nul
        monkeypatch.setattr(sys, "stdin", io.StringIO(two_valid))
        assert main(["-"]) == 0

    def test_fails_one_invalid_of_two_messages_stdin(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        nul = "\x00"
        mixed = VALID_MESSAGE + nul + NO_SIGNOFF_MESSAGE + nul
        monkeypatch.setattr(sys, "stdin", io.StringIO(mixed))
        assert main(["-"]) == 1

    def test_empty_stdin_passes(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert main(["-"]) == 0

    def test_help_flag_exits_zero(self, capsys: pytest.CaptureFixture) -> None:
        assert main(["--help"]) == 0

    def test_h_flag_exits_zero(self, capsys: pytest.CaptureFixture) -> None:
        assert main(["-h"]) == 0
