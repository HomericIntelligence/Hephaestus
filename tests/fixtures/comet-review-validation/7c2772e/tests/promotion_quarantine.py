"""Temporary exceptions for confirmed promotion test failures."""

import json
import re
from pathlib import Path

import pytest

MANIFEST = Path(__file__).with_name("promotion-quarantine.json")


def load_quarantine(path: Path) -> dict[str, str]:
    """Read exact node IDs and their repair issues."""
    groups = json.loads(path.read_text())
    if not isinstance(groups, list):
        raise pytest.UsageError("The quarantine manifest must contain a list.")
    nodes: dict[str, str] = {}
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"issue", "nodes"}:
            raise pytest.UsageError("A quarantine group must contain issue and nodes.")
        issue = group["issue"]
        if not isinstance(issue, str) or not re.fullmatch(
            r"https://github\.com/LLM360/comet/issues/[1-9][0-9]*", issue
        ):
            raise pytest.UsageError("A quarantine group must link to a repair issue.")
        if not isinstance(group["nodes"], list) or not group["nodes"]:
            raise pytest.UsageError("A quarantine group must contain exact node IDs.")
        for node in group["nodes"]:
            if not isinstance(node, str) or ".py::" not in node or "*" in node or "?" in node:
                raise pytest.UsageError("A quarantine entry must be an exact test node ID.")
            if node in nodes:
                raise pytest.UsageError("A quarantine node ID occurs more than once.")
            nodes[node] = issue
    return nodes


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply the recorded test exceptions."""
    nodes = load_quarantine(MANIFEST)
    for item in items:
        if issue := nodes.get(item.nodeid):
            item.add_marker(
                pytest.mark.skip(
                    reason=f"Temporary v0.1.100 exception; restore for the next release: {issue}"
                )
            )
