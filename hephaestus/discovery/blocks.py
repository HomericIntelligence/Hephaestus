"""CLAUDE.md block extraction utilities.

Provides generic block-extraction helpers for splitting a markdown file into
named sections.  Block definitions (line ranges + filenames) are supplied by
the caller — no project-specific defaults are baked in.

Usage::

    from hephaestus.discovery.blocks import extract_blocks

    blocks = [
        ("B01", 1, 11, "B01-overview.md"),
        ("B02", 13, 67, "B02-rules.md"),
    ]
    created = extract_blocks(Path("CLAUDE.md"), Path("out/"), blocks)
"""

from __future__ import annotations

from pathlib import Path

# A block definition: (block_id, start_line, end_line, output_filename)
# Lines are 1-indexed (same convention as most editors and grep output).
BlockDef = tuple[str, int, int, str]


def discover_blocks(
    claude_md_path: Path,
    block_defs: list[BlockDef] | None = None,
) -> list[BlockDef]:
    """Return block definitions for a CLAUDE.md file.

    If *block_defs* is provided it is returned as-is after validating that
    *claude_md_path* exists.  When no definitions are supplied the function
    raises :exc:`ValueError` — automatic section detection is intentionally
    not implemented because markdown headings vary too much across projects.

    Args:
        claude_md_path: Path to the CLAUDE.md file.
        block_defs: Explicit block definitions ``(id, start, end, filename)``.
            Lines are 1-indexed.

    Returns:
        The resolved list of block definitions.

    Raises:
        FileNotFoundError: If *claude_md_path* does not exist.
        ValueError: If *block_defs* is ``None`` (no automatic detection).

    """
    if not claude_md_path.exists():
        raise FileNotFoundError(f"CLAUDE.md not found: {claude_md_path}")
    if block_defs is None:
        raise ValueError(
            "block_defs must be provided; automatic section detection is not implemented. "
            "Pass an explicit list of (id, start_line, end_line, filename) tuples."
        )
    return block_defs


def extract_blocks(
    source_file: Path,
    output_dir: Path,
    block_defs: list[BlockDef] | None = None,
) -> list[Path]:
    """Extract sections of *source_file* into separate files.

    Args:
        source_file: Markdown file to split (typically ``CLAUDE.md``).
        output_dir: Directory where extracted block files are written.
            Created if it does not exist.
        block_defs: Block definitions ``(id, start, end, filename)``.
            Lines are 1-indexed.  Passed through to :func:`discover_blocks`.

    Returns:
        List of paths to the created block files.

    Raises:
        FileNotFoundError: If *source_file* does not exist.
        ValueError: If *block_defs* is ``None``.

    """
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved = discover_blocks(source_file, block_defs)
    lines = source_file.read_text(encoding="utf-8").splitlines(keepends=True)

    created: list[Path] = []
    for _block_id, start, end, filename in resolved:
        block_lines = lines[start - 1 : end]
        output_path = output_dir / filename
        output_path.write_text("".join(block_lines), encoding="utf-8")
        created.append(output_path)
    return created
