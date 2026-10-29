#!/usr/bin/env python3
"""Regenerate the Athena agent-contract mirror block for AGENTS.md.

This script is the consumer-side mechanical projection defined by the Athena
agent-contract v1 governance adoption (issue #2853). It reads the released
Athena principles catalog (``docs/principles/README.md`` at the protected
signed tag ``agent-contract-v1.0.0``) as data and renders the marked
P001-P091 block exactly once. It carries no copy of Athena's parser or
validator: the byte-equality arbiter is the released validator, executed by
the ``agent-contract`` reusable-workflow job in each in-scope workflow, plus
the pinned digests below.

Usage:
    python3 scripts/generate_agent_contract_block.py --catalog <path> --output <path>

Error handling: this console script is a module boundary. It reports each
projection defect once on stderr and exits nonzero.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import NamedTuple

CONTRACT_TAG = "agent-contract-v1.0.0"
# SHA-256 of docs/principles/README.md at the protected signed tag. Bound by
# the immutable GitHub release 384248657; verified at implementation start.
CATALOG_SHA256 = "c79a2824661546182b6fbc0541ca5bd08b666644e74590bf92c7a504abc022b3"
BEGIN_MARKER = f"<!-- BEGIN ATHENA DEVELOPMENT PRINCIPLES: {CONTRACT_TAG} -->"
END_MARKER = "<!-- END ATHENA DEVELOPMENT PRINCIPLES -->"
DETAIL_URL_PREFIX = (
    f"https://github.com/HomericIntelligence/Athena/blob/{CONTRACT_TAG}/docs/principles/"
)
EXPECTED_PRINCIPLE_IDENTIFIERS = tuple(f"P{number:03d}" for number in range(1, 92))
# Row contract of the released catalog: [name](details/pNNN-slug.md) — description
CATALOG_ENTRY = re.compile(
    r"\A\[(?P<name>[^\]\r\n]+)\]"
    r"(?P<detail>\(details/p[0-9]{3}-[a-z0-9-]+\.md\))"
    r" — (?P<description>.+)\Z"
)
MARKDOWN_HEADING = re.compile(r"^(?P<level>#{1,3}) (?P<title>[^\r\n]+)$")
PRINCIPLE_HEADING = re.compile(r"^P[0-9]{3}$")


class AgentContractProjectionError(ValueError):
    """The pinned released catalog violates the projection contract."""


class Principle(NamedTuple):
    """One canonical development principle from the released catalog."""

    identifier: str
    name: str
    description: str
    detail_path: str


def read_catalog_bytes(path: Path) -> bytes:
    """Return the released catalog bytes and require the pinned digest."""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise AgentContractProjectionError(f"The catalog is unreadable: {error}") from error
    digest = hashlib.sha256(data).hexdigest()
    if digest != CATALOG_SHA256:
        raise AgentContractProjectionError(
            f"The catalog digest {digest} does not match the pinned released digest "
            f"{CATALOG_SHA256} for {CONTRACT_TAG}. Stop: re-verify the provider tag, "
            "the immutable release, and the catalog bytes before you continue."
        )
    return data


def _outside_fence_mask(lines: list[str]) -> list[bool]:
    """Mark every line that does not sit inside a fenced code block."""
    outside: list[bool] = []
    fence: str | None = None
    for line in lines:
        stripped = line.strip()
        opens = stripped.startswith("```") or stripped.startswith("~~~")
        if fence is None:
            outside.append(True)
            if opens:
                fence = stripped[:3]
            continue
        outside.append(False)
        if stripped.startswith(fence):
            fence = None
    return outside


def parse_principles(catalog_text: str) -> tuple[Principle, ...]:
    """Parse the released catalog into ordered principles.

    The projection follows the released row contract only: level-3 P-headings
    carry one link row whose joined paragraph matches ``CATALOG_ENTRY``.
    """
    lines = catalog_text.split("\n")
    outside = _outside_fence_mask(lines)
    headings: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        if not outside[index]:
            continue
        match = MARKDOWN_HEADING.fullmatch(line)
        if match is not None:
            headings.append((index, match.group("level"), match.group("title")))

    principles: list[Principle] = []
    for position, (line_index, level, title) in enumerate(headings):
        if level != "###" or PRINCIPLE_HEADING.fullmatch(title) is None:
            continue
        end_index = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        content = [line for line in lines[line_index + 1 : end_index] if line]
        if not content or any(line != line.strip() for line in content):
            raise AgentContractProjectionError(
                f"The '{title}' entry does not match the required catalog structure."
            )
        entry = CATALOG_ENTRY.fullmatch(" ".join(content))
        if entry is None:
            raise AgentContractProjectionError(
                f"The '{title}' entry does not match the required catalog structure."
            )
        detail_path = entry.group("detail")[1:-1]
        if not detail_path.startswith(f"details/{title.casefold()}-"):
            raise AgentContractProjectionError(
                f"The '{title}' detail link does not match its identifier."
            )
        principles.append(
            Principle(title, entry.group("name"), entry.group("description"), detail_path)
        )

    identifiers = tuple(principle.identifier for principle in principles)
    if identifiers != EXPECTED_PRINCIPLE_IDENTIFIERS:
        raise AgentContractProjectionError(
            "The catalog projection must be exactly P001 through P091 in order."
        )
    return tuple(principles)


def render_principles_block(principles: tuple[Principle, ...]) -> str:
    """Render the deterministic AGENTS.md catalog mirror block."""
    rows = [
        f"- [{principle.identifier} — {principle.name}]"
        f"({DETAIL_URL_PREFIX}{principle.detail_path}) — {principle.description}"
        for principle in principles
    ]
    return "\n".join((BEGIN_MARKER, *rows, END_MARKER))


def main() -> int:
    """Parse arguments and write the rendered mirror block."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        required=True,
        help="Path to the released Athena docs/principles/README.md bytes.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination file for the rendered block (UTF-8, trailing newline).",
    )
    args = parser.parse_args()

    catalog_bytes = read_catalog_bytes(args.catalog)
    try:
        catalog_text = catalog_bytes.decode("utf-8")
    except UnicodeError as error:
        raise AgentContractProjectionError(
            f"The catalog must be readable UTF-8: {error}"
        ) from error
    block = render_principles_block(parse_principles(catalog_text))
    args.output.write_text(block + "\n", encoding="utf-8")
    block_sha256 = hashlib.sha256(block.encode("utf-8")).hexdigest()
    print(f"Wrote {args.output}: {CONTRACT_TAG}, block SHA-256 {block_sha256}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AgentContractProjectionError as error:
        print(f"agent-contract projection failed: {error}", file=sys.stderr)
        sys.exit(2)
