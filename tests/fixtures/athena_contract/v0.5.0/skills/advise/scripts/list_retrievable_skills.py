#!/usr/bin/env python3
"""List flat Mnemosyne main-skill files eligible for normal retrieval."""

from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from skills._cli import argument_parser

COMPANION_FILE = re.compile(
    r"(?:.*\.notes(?:-[A-Za-z0-9_-]+)?\.md|.*\.history(?:[-.].*)?)\Z"
)


def retrievable_skill_files(knowledge_root: Path) -> list[Path]:
    """Return sorted flat main-skill files from a Mnemosyne checkout."""
    skills_directory = knowledge_root / "skills"
    if not skills_directory.is_dir():
        raise RuntimeError(
            f"knowledge skills directory is unavailable: {skills_directory}"
        )
    return sorted(
        path
        for path in skills_directory.glob("*.md")
        if path.is_file() and COMPANION_FILE.fullmatch(path.name) is None
    )


def main() -> int:
    parser = argument_parser(description=__doc__)
    parser.add_argument("knowledge_root", type=Path)
    arguments = parser.parse_args()
    try:
        paths = retrievable_skill_files(arguments.knowledge_root)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    for path in paths:
        print(path.relative_to(arguments.knowledge_root).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
