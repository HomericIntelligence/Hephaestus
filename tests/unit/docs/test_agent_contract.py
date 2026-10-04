"""Regression contract for the canonical repository agent guidance."""

import ast
import re
from pathlib import Path

from hephaestus.validation.markdown import extract_markdown_links, validate_relative_link

REPO_ROOT = Path(__file__).resolve().parents[3]


def _active_documentation_paths() -> list[Path]:
    """Return current documentation, excluding immutable ADR history."""
    root_docs = REPO_ROOT.glob("*.md")
    docs_tree = (
        path
        for path in (REPO_ROOT / "docs").rglob("*.md")
        if (REPO_ROOT / "docs" / "adr") not in path.parents
    )
    return sorted(path for path in (*root_docs, *docs_tree) if path.name != "CLAUDE.md")


def test_claude_md_is_compatibility_pointer() -> None:
    """The legacy file contains only a heading and pointer to the contract."""
    claude_md = REPO_ROOT / "CLAUDE.md"
    content = claude_md.read_text(encoding="utf-8")

    blocks = re.split(r"\n\s*\n", content.strip())
    assert len(blocks) == 2
    heading, pointer = blocks
    assert re.fullmatch(r"#\s+\S.*", heading)
    assert not any(line.lstrip().startswith("#") for line in pointer.splitlines())

    links = extract_markdown_links(content)
    assert len(links) == 1

    contract_links = [link for link in links if link[0] == "AGENTS.md"]
    assert len(contract_links) == 1
    target, _line = contract_links[0]
    valid, error = validate_relative_link(target, claude_md, REPO_ROOT)
    assert valid, error
    assert (claude_md.parent / target).resolve() == (REPO_ROOT / "AGENTS.md").resolve()


def test_active_documentation_directs_legacy_references_to_agents_md() -> None:
    """Active documentation must direct legacy-contract references to AGENTS.md."""
    stale_references = []
    for path in _active_documentation_paths():
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "CLAUDE.md" not in line:
                continue
            targets = {target for target, _line in extract_markdown_links(line)}
            if "AGENTS.md" not in targets:
                stale_references.append(f"{path.relative_to(REPO_ROOT)}:{line_number}")

    assert not stale_references, (
        "Active documentation references CLAUDE.md without directing readers "
        f"to AGENTS.md: {stale_references}"
    )


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
