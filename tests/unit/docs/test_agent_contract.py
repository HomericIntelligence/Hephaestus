"""Regression contract for the canonical repository agent guidance."""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

EXPECTED_POINTER = (
    "# Claude Code guidance\n\n"
    "Follow [`AGENTS.md`](AGENTS.md). It is the sole authoritative "
    "agent contract for this repository.\n"
)

ALLOWED_CLAUDE_REFERENCE_PATHS = {
    Path(".github/CODEOWNERS"),
    Path("docs/adr/0001-automation-library-boundary.md"),
    Path("docs/adr/0013-backup-and-disaster-recovery-policy.md"),
}


def _tracked_paths() -> list[Path]:
    """Return repository paths tracked by Git, excluding local artifacts."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [Path(path) for path in result.stdout.splitlines()]


def test_claude_md_is_exact_pointer() -> None:
    """The legacy file remains only as the documented compatibility pointer."""
    assert (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8") == EXPECTED_POINTER


def test_only_explicit_compatibility_and_history_references_remain() -> None:
    """Tracked consumers use AGENTS.md, except compatibility and ADR history."""
    test_file = Path(__file__).resolve()
    unexpected: list[str] = []

    for relative in _tracked_paths():
        path = REPO_ROOT / relative
        if path.resolve() == test_file:
            continue
        if relative in ALLOWED_CLAUDE_REFERENCE_PATHS:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue

        for number, line in enumerate(lines, 1):
            if "CLAUDE.md" in line:
                unexpected.append(f"{relative}:{number}: {line.strip()}")

    assert unexpected == []
