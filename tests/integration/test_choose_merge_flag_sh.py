import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SNIPPET = REPO / "scripts" / "choose_merge_flag.sh"


def test_shell_helper_defines_function() -> None:
    """Verify that choose_merge_flag is defined as a function."""
    out = subprocess.run(
        ["bash", "-c", f". {SNIPPET} && type choose_merge_flag"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "choose_merge_flag is a function" in out.stdout


def test_shell_helper_errors_on_missing_arg() -> None:
    """Verify that choose_merge_flag returns error when called without arguments."""
    out = subprocess.run(
        ["bash", "-c", f". {SNIPPET} && choose_merge_flag"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 2
    assert "missing required argument" in out.stderr
