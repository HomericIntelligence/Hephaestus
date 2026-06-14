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


def _run_with_mock_gh(json_body: str, repo: str = "owner/repo") -> subprocess.CompletedProcess[str]:
    """Run choose_merge_flag with a mock gh function returning json_body."""
    script = f"""
gh() {{
    case "$1" in
        api) printf '%s\\n' '{json_body}' ;;
        *) command gh "$@" ;;
    esac
}}
export -f gh
. {SNIPPET}
choose_merge_flag {repo}
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )


def test_shell_helper_selects_rebase_when_all_allowed() -> None:
    """Preference order: rebase wins when all three methods are allowed."""
    body = '{"allow_rebase_merge":true,"allow_squash_merge":true,"allow_merge_commit":true}'
    result = _run_with_mock_gh(body)
    assert result.returncode == 0
    assert result.stdout.strip() == "--rebase"


def test_shell_helper_selects_squash_when_rebase_disallowed() -> None:
    """Falls back to squash when rebase is not permitted."""
    body = '{"allow_rebase_merge":false,"allow_squash_merge":true,"allow_merge_commit":false}'
    result = _run_with_mock_gh(body)
    assert result.returncode == 0
    assert result.stdout.strip() == "--squash"


def test_shell_helper_exits_1_when_no_methods_allowed() -> None:
    """Returns exit 1 and diagnostic message when repo permits no merge methods."""
    body = '{"allow_rebase_merge":false,"allow_squash_merge":false,"allow_merge_commit":false}'
    result = _run_with_mock_gh(body)
    assert result.returncode == 1
    assert "allows no merge methods" in result.stderr


def test_shell_helper_selects_merge_when_rebase_and_squash_disallowed() -> None:
    """Falls back to merge commit when both rebase and squash are not permitted."""
    body = '{"allow_rebase_merge":false,"allow_squash_merge":false,"allow_merge_commit":true}'
    result = _run_with_mock_gh(body)
    assert result.returncode == 0
    assert result.stdout.strip() == "--merge"


def test_shell_helper_handles_gh_stderr_noise() -> None:
    """Stderr from gh (warnings, banners) must not corrupt jq input.

    Regression guard for the safety fix: fails against the unfixed 2>&1 version.
    """
    merge_settings = (
        '{"allow_rebase_merge":true,"allow_squash_merge":true,"allow_merge_commit":true}'
    )
    script = f"""
gh() {{
    case "$1" in
        api)
            printf '%s\\n' 'warning: new gh release available' >&2
            printf '%s\\n' '{merge_settings}'
            ;;
        *) command gh "$@" ;;
    esac
}}
export -f gh
. {SNIPPET}
choose_merge_flag owner/repo
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, f"stderr noise broke jq parse: stderr={result.stderr!r}"
    assert result.stdout.strip() == "--rebase"
