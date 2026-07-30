"""Regression contract for the canonical repository agent guidance."""

import ast
from pathlib import Path

from hephaestus.validation.markdown import extract_markdown_links, validate_relative_link

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_CLAUDE_MD = (
    "# Claude Code guidance\n"
    "\n"
    "Follow [`AGENTS.md`](AGENTS.md). "
    "It is the sole authoritative agent contract for this repository.\n"
)


def test_claude_md_is_exact_compatibility_pointer() -> None:
    """The legacy file is the exact required pointer to the canonical contract."""
    claude_md = REPO_ROOT / "CLAUDE.md"
    content = claude_md.read_text(encoding="utf-8")
    assert content == EXPECTED_CLAUDE_MD

    links = extract_markdown_links(content)

    contract_links = [link for link in links if link[0] == "AGENTS.md"]
    assert len(contract_links) == 1
    target, _line = contract_links[0]
    valid, error = validate_relative_link(target, claude_md, REPO_ROOT)
    assert valid, error
    assert (claude_md.parent / target).resolve() == (REPO_ROOT / "AGENTS.md").resolve()


def test_live_consumers_do_not_use_legacy_contract_path() -> None:
    """Production code must consume the canonical contract, not its compatibility pointer."""
    stale_consumers = []
    for path in (REPO_ROOT / "hephaestus").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Constant) and node.value == "CLAUDE.md" for node in ast.walk(tree)
        ):
            stale_consumers.append(path.relative_to(REPO_ROOT))

    assert not stale_consumers, f"Live consumers still use CLAUDE.md: {stale_consumers}"
