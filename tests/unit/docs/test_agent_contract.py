"""Regression contract for the canonical repository agent guidance."""

from pathlib import Path

from hephaestus.validation.markdown import extract_markdown_links, validate_relative_link

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_claude_md_delegates_without_independent_policy() -> None:
    """The legacy file provides one resolvable link to the canonical contract."""
    claude_md = REPO_ROOT / "CLAUDE.md"
    content = claude_md.read_text(encoding="utf-8")
    links = extract_markdown_links(content)

    assert content.startswith(
        "# Claude Code guidance\n\n"
        "Follow [`AGENTS.md`](AGENTS.md). It is the sole authoritative agent "
        "contract for this repository.\n"
    )
    assert len(links) == 1
    target, _line = links[0]
    valid, error = validate_relative_link(target, claude_md, REPO_ROOT)
    assert valid, error
    assert (claude_md.parent / target).resolve() == (REPO_ROOT / "AGENTS.md").resolve()
