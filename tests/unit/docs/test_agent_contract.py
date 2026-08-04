"""Regression contract for the canonical repository agent guidance."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

EXPECTED_POINTER = (
    "# Claude Code guidance\n\n"
    "Follow [`AGENTS.md`](AGENTS.md). It is the sole authoritative "
    "agent contract for this repository.\n"
)

REQUIRED_SECTIONS = {
    "## Project Overview": ("**Purpose**:", "Role in Ecosystem"),
    "## Repository Structure": ("automation/", "unit/"),
    "## Library vs product layer": (
        "automation → library",
        "Coverage omit-list invariant",
    ),
    "## Python Development Guidelines": (
        "Python 3.13",
        "83%+ test coverage enforced; target 90%",
    ),
    "## Key Development Principles": ("KISS", "YAGNI", "SOLID", "POLA"),
    "## Security Configuration Guidelines": (
        "Never hardcode secrets",
        "Validate input types and ranges",
    ),
    "## Documentation Rules": ("No CHANGELOG.md.",),
    "## Claude Code Optimization": (
        "athena:skill-advisor",
        "Agent Skills vs Sub-Agents Decision Tree",
    ),
    "## Working with GitHub": (
        "Closes #<issue-number>",
        "git commit -S",
        "Signed-off-by",
        "PR titles MUST",
    ),
    "## Environment Setup": ("just bootstrap", "uv sync"),
    "## Common Commands": ("--no-verify", "uv run mypy"),
    "## Troubleshooting": ("Import Errors", "Test Failures"),
    "## Key Files and Directories": ("hephaestus/utils/", "pyproject.toml"),
    "## Version Management": (
        'dynamic = ["version"]',
        "Make sure all temporary files are in the build/ directory.",
    ),
    "## AI-agent topology": ("seven in-memory stage queues",),
    "## Canonical architecture reference": ("docs/architecture.md",),
}

ALLOWED_CLAUDE_REFERENCE_LINES = {
    Path(".github/CODEOWNERS"): {"CLAUDE.md @mvillmow"},
    Path("docs/adr/0001-automation-library-boundary.md"): {
        "At decision time this guidance lived in `CLAUDE.md`; "
        "it is now consolidated in `AGENTS.md`."
    },
}

EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
}


def _section(text: str, heading: str) -> str:
    """Return the Markdown section beginning at *heading*."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def test_claude_md_is_exact_pointer() -> None:
    """The legacy entry point must contain only the compatibility pointer."""
    assert (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8") == EXPECTED_POINTER


def test_source_sections_and_directives_are_preserved() -> None:
    """The consolidated contract retains each mapped source section."""
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    positions = []
    for heading, markers in REQUIRED_SECTIONS.items():
        section = _section(text, heading)
        positions.append(text.index(heading))
        for marker in markers:
            assert marker in section, f"{heading} lost directive {marker!r}"
    assert positions == sorted(positions)


def test_only_explicit_compatibility_and_history_references_remain() -> None:
    """No live policy consumer may continue citing the legacy pointer."""
    test_file = Path(__file__).resolve()
    unexpected: list[str] = []

    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.resolve() == test_file:
            continue
        relative = path.relative_to(REPO_ROOT)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue

        allowed = ALLOWED_CLAUDE_REFERENCE_LINES.get(relative, set())
        for number, line in enumerate(lines, 1):
            if "CLAUDE.md" in line and line.strip() not in allowed:
                unexpected.append(f"{relative}:{number}: {line.strip()}")

    assert unexpected == []
