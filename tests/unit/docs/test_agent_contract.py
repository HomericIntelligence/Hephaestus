"""Regression contract for the canonical repository agent guidance."""

import re
import subprocess
from pathlib import Path

from hephaestus.validation.markdown import extract_markdown_links, validate_relative_link

REPO_ROOT = Path(__file__).resolve().parents[3]

ALLOWED_CLAUDE_REFERENCES = frozenset(
    {
        (Path(".github/CODEOWNERS"), 6, "CLAUDE.md @mvillmow"),
        (
            Path("docs/adr/0001-automation-library-boundary.md"),
            70,
            '- README and CLAUDE.md gain a "Library vs product layer" section pointing',
        ),
        (
            Path("docs/adr/0013-backup-and-disaster-recovery-policy.md"),
            50,
            "upholds the CLAUDE.md secrets policy (no secrets in artifacts).",
        ),
        (
            Path("docs/adr/0014-agent-contract-canonical-location.md"),
            11,
            "`CLAUDE.md`, which was the agent-contract location when those decisions were",
        ),
        (
            Path("docs/adr/0014-agent-contract-canonical-location.md"),
            13,
            "`AGENTS.md`; `CLAUDE.md` is only a compatibility pointer.",
        ),
        (
            Path("docs/adr/0014-agent-contract-canonical-location.md"),
            22,
            "2. `CLAUDE.md` remains an exact compatibility pointer to `AGENTS.md` and does",
        ),
        (
            Path("docs/adr/0014-agent-contract-canonical-location.md"),
            25,
            "ADR-0001 and ADR-0013 retain their original `CLAUDE.md` wording as immutable",
        ),
        (
            Path("docs/adr/0014-agent-contract-canonical-location.md"),
            35,
            "- **Remove `CLAUDE.md` outright.** Rejected: existing integrations may still",
        ),
    }
)


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


def test_claude_md_delegates_without_independent_policy() -> None:
    """The legacy file provides one resolvable link and no standalone content."""
    claude_md = REPO_ROOT / "CLAUDE.md"
    content = claude_md.read_text(encoding="utf-8")
    links = extract_markdown_links(content)

    assert len(links) == 1
    target, _line = links[0]
    valid, error = validate_relative_link(target, claude_md, REPO_ROOT)
    assert valid, error
    assert (claude_md.parent / target).resolve() == (REPO_ROOT / "AGENTS.md").resolve()
    assert not re.sub(r"\[[^\]]+\]\([^\)]+\)", "", content).strip()


def test_only_explicit_compatibility_and_history_references_remain() -> None:
    """Tracked consumers use AGENTS.md, except compatibility and ADR history."""
    test_file = Path(__file__).resolve()
    unexpected: list[str] = []

    for relative in _tracked_paths():
        path = REPO_ROOT / relative
        if path.resolve() == test_file:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue

        for number, line in enumerate(lines, 1):
            if "CLAUDE.md" in line:
                reference = (relative, number, line.strip())
                if reference not in ALLOWED_CLAUDE_REFERENCES:
                    unexpected.append(f"{relative}:{number}: {line.strip()}")

    assert unexpected == []
