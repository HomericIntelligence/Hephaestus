"""Tests for hephaestus.validation.complexity."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.utils.helpers import NETWORK_TIMEOUT
from hephaestus.validation.complexity import (
    RuffComplexityError,
    check_max_complexity,
    main,
    run_ruff_complexity_check,
)


class TestRunRuffComplexityCheck:
    """Tests for run_ruff_complexity_check()."""

    def test_no_violations(self, tmp_path: Path) -> None:
        """Simple code returns no violations."""
        py_file = tmp_path / "simple.py"
        py_file.write_text("def hello():\n    return 1\n")
        violations = run_ruff_complexity_check(str(py_file), 10, tmp_path)
        assert violations == []

    def test_complex_function_flagged(self, tmp_path: Path) -> None:
        """Function exceeding threshold is detected."""
        branches = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(15))
        py_file = tmp_path / "complex.py"
        py_file.write_text(f"def complex_func(x):\n{branches}\n    return -1\n")
        violations = run_ruff_complexity_check(str(py_file), 5, tmp_path)
        assert len(violations) >= 1
        assert violations[0]["code"] == "C901"

    def test_threshold_respected(self, tmp_path: Path) -> None:
        """Higher threshold allows more complex functions."""
        branches = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(8))
        py_file = tmp_path / "moderate.py"
        py_file.write_text(f"def moderate_func(x):\n{branches}\n    return -1\n")
        violations_low = run_ruff_complexity_check(str(py_file), 5, tmp_path)
        violations_high = run_ruff_complexity_check(str(py_file), 20, tmp_path)
        assert len(violations_low) >= 1
        assert len(violations_high) == 0

    def test_missing_target_is_tool_failure(self, tmp_path: Path) -> None:
        """A missing target fails before Ruff is invoked."""
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            with pytest.raises(RuffComplexityError, match="does not exist"):
                run_ruff_complexity_check("missing.py", 10, tmp_path)
        mock_run.assert_not_called()

    def test_nonzero_exit_is_tool_failure(self, tmp_path: Path) -> None:
        """A nonzero Ruff exit preserves its stderr and status."""
        target = tmp_path / "target.py"
        target.write_text("def f():\n    return 1\n")
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="",
                stderr="ruff failed to inspect target",
                returncode=2,
            )
            with pytest.raises(RuffComplexityError, match="exit code 2") as exc_info:
                run_ruff_complexity_check(str(target), 10, tmp_path)

        assert exc_info.value.stderr == "ruff failed to inspect target"
        assert exc_info.value.returncode == 2

    def test_timeout_is_tool_failure(self, tmp_path: Path) -> None:
        """A hung Ruff process is translated into the normal tool-failure type."""
        target = tmp_path / "target.py"
        target.write_text("def f():\n    return 1\n")
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd="ruff",
                timeout=NETWORK_TIMEOUT,
                stderr=b"partial timeout diagnostic",
            )
            with pytest.raises(RuffComplexityError, match="timed out") as exc_info:
                run_ruff_complexity_check(str(target), 10, tmp_path)

        assert exc_info.value.stderr == "partial timeout diagnostic"
        assert exc_info.value.returncode is None

    def test_process_launch_failure_is_tool_failure(self, tmp_path: Path) -> None:
        """A subprocess launch failure is translated into the normal tool-failure type."""
        target = tmp_path / "target.py"
        target.write_text("def f():\n    return 1\n")
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            mock_run.side_effect = OSError(2, "No such file or directory")
            with pytest.raises(RuffComplexityError, match="Failed to launch Ruff") as exc_info:
                run_ruff_complexity_check(str(target), 10, tmp_path)

        assert "No such file or directory" in exc_info.value.stderr
        assert exc_info.value.returncode is None

    def test_empty_output_is_tool_failure(self, tmp_path: Path) -> None:
        """A successful process without Ruff JSON cannot pass validation."""
        target = tmp_path / "target.py"
        target.write_text("def f():\n    return 1\n")
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            with pytest.raises(RuffComplexityError, match="empty JSON"):
                run_ruff_complexity_check(str(target), 10, tmp_path)

    def test_invalid_json_is_tool_failure(self, tmp_path: Path) -> None:
        """Malformed Ruff output cannot be interpreted as zero findings."""
        target = tmp_path / "target.py"
        target.write_text("def f():\n    return 1\n")
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="not-json",
                stderr="parser diagnostic",
                returncode=0,
            )
            with pytest.raises(RuffComplexityError, match="invalid JSON"):
                run_ruff_complexity_check(str(target), 10, tmp_path)

    @pytest.mark.parametrize("output", ["{}", '[{"location": []}]'])
    def test_invalid_json_shape_is_tool_failure(self, tmp_path: Path, output: str) -> None:
        """Malformed JSON structures cannot be interpreted as findings."""
        target = tmp_path / "target.py"
        target.write_text("def f():\n    return 1\n")
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=output, stderr="", returncode=0)
            with pytest.raises(RuffComplexityError, match="invalid JSON"):
                run_ruff_complexity_check(str(target), 10, tmp_path)


class TestCheckMaxComplexity:
    """Tests for check_max_complexity()."""

    def test_clean_code_passes(self, tmp_path: Path) -> None:
        """Simple code passes complexity check."""
        py_file = tmp_path / "clean.py"
        py_file.write_text("def clean():\n    return True\n")
        result = check_max_complexity(str(py_file), 10, repo_root=tmp_path)
        assert result is True

    def test_complex_code_fails(self, tmp_path: Path) -> None:
        """Complex code fails complexity check."""
        branches = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(15))
        py_file = tmp_path / "bad.py"
        py_file.write_text(f"def bad_func(x):\n{branches}\n    return -1\n")
        result = check_max_complexity(str(py_file), 5, repo_root=tmp_path)
        assert result is False

    def test_verbose_output(self, tmp_path: Path) -> None:
        """Verbose mode prints details."""
        py_file = tmp_path / "clean.py"
        py_file.write_text("def clean():\n    return True\n")
        result = check_max_complexity(str(py_file), 10, repo_root=tmp_path, verbose=True)
        assert result is True

    def test_tool_failure_surfaces_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Human output includes Ruff's diagnostic and fails the check."""
        target = tmp_path / "target.py"
        target.write_text("def f():\n    return 1\n")
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="",
                stderr="ruff crashed",
                returncode=2,
            )
            assert check_max_complexity(str(target), 10, repo_root=tmp_path) is False

        assert "ruff crashed" in capsys.readouterr().err

    def test_timeout_returns_false(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Human output treats a Ruff timeout as a failed check, not a traceback."""
        target = tmp_path / "target.py"
        target.write_text("def f():\n    return 1\n")
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd="ruff",
                timeout=NETWORK_TIMEOUT,
                stderr="partial timeout diagnostic",
            )
            assert check_max_complexity(str(target), 10, repo_root=tmp_path) is False

        err = capsys.readouterr().err
        assert "timed out" in err
        assert "partial timeout diagnostic" in err


class TestMain:
    """Tests for main() CLI entry point."""

    def test_clean_returns_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clean code exits 0."""
        py_file = tmp_path / "clean.py"
        py_file.write_text("def f():\n    return 1\n")
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-complexity",
                "--path",
                str(py_file),
                "--repo-root",
                str(tmp_path),
            ],
        )
        assert main() == 0

    def test_complex_returns_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Complex code exits 1."""
        branches = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(15))
        py_file = tmp_path / "bad.py"
        py_file.write_text(f"def f(x):\n{branches}\n    return -1\n")
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-complexity",
                "--path",
                str(py_file),
                "--threshold",
                "5",
                "--repo-root",
                str(tmp_path),
            ],
        )
        assert main() == 1

    def test_json_tool_failure_is_structured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """JSON mode emits machine-readable Ruff failure details."""
        target = tmp_path / "target.py"
        target.write_text("def f():\n    return 1\n")
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-complexity",
                "--path",
                str(target),
                "--repo-root",
                str(tmp_path),
                "--json",
            ],
        )
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="",
                stderr="ruff crashed",
                returncode=2,
            )
            assert main() == 1

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["exit_code"] == 1
        assert payload["ruff_exit_code"] == 2
        assert payload["stderr"] == "ruff crashed"

    def test_json_timeout_failure_is_structured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """JSON mode emits the standard tool-failure envelope for Ruff timeouts."""
        target = tmp_path / "target.py"
        target.write_text("def f():\n    return 1\n")
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-complexity",
                "--path",
                str(target),
                "--repo-root",
                str(tmp_path),
                "--json",
            ],
        )
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd="ruff",
                timeout=NETWORK_TIMEOUT,
                stderr="partial timeout diagnostic",
            )
            assert main() == 1

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["exit_code"] == 1
        assert "timed out" in payload["message"]
        assert payload["ruff_exit_code"] is None
        assert payload["stderr"] == "partial timeout diagnostic"


class TestComplexitySubprocessTimeout:
    """``run_ruff_complexity_check`` must bound the external ruff call (#684)."""

    def test_ruff_invocation_passes_timeout(self, tmp_path: Path) -> None:
        """The ruff subprocess is bounded so a hung tool cannot stall the check."""
        target = tmp_path / "target.py"
        target.write_text("def f():\n    return 1\n")
        with patch("hephaestus.validation.complexity.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="[]", stderr="", returncode=0)
            run_ruff_complexity_check(str(target), 10, tmp_path)
        assert mock_run.call_args.kwargs["timeout"] == NETWORK_TIMEOUT
        assert "--exit-zero" in mock_run.call_args.args[0]
