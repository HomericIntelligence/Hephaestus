"""Epic task-list conventions (ADR-013 §6).

The epic body format is a GitHub task list, one child issue per line::

    - [ ] #123 (depends on: #456, #789)
    - [x] #124

Parsing/rendering round-trips through this module so Telemachy (which
creates epics), the planner role (which reads them), and tests all agree.
Epic *detection* reuses :func:`hephaestus.automation.state_labels.is_epic`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from hephaestus.automation.mesh.config import slugify
from hephaestus.automation.state_labels import is_epic, partition_epics

__all__ = [
    "AGAMEMNON_EPIC_LABEL",
    "EpicChild",
    "epic_key",
    "is_epic",
    "parse_task_list",
    "partition_epics",
    "render_task_list",
]

#: Label Telemachy applies to epics it registers (ADR-013 §6).
AGAMEMNON_EPIC_LABEL = "agamemnon-epic"

_TASK_LINE_RE = re.compile(
    r"^\s*-\s*\[(?P<checked>[ xX])\]\s*#(?P<number>\d+)"
    r"(?:\s*\(depends on:\s*(?P<deps>[^)]*)\))?\s*$"
)
_DEP_RE = re.compile(r"#(\d+)")


@dataclass
class EpicChild:
    """One child-issue entry in an epic's task list."""

    number: int
    depends_on: list[int] = field(default_factory=list)
    checked: bool = False


def parse_task_list(body: str) -> list[EpicChild]:
    """Parse an epic body's task-list lines into :class:`EpicChild` entries.

    Non-matching lines (prose, headings) are ignored, so the task list can
    live inside a larger epic description.
    """
    children: list[EpicChild] = []
    for line in body.splitlines():
        match = _TASK_LINE_RE.match(line)
        if match is None:
            continue
        deps = [int(n) for n in _DEP_RE.findall(match.group("deps") or "")]
        children.append(
            EpicChild(
                number=int(match.group("number")),
                depends_on=deps,
                checked=match.group("checked").lower() == "x",
            )
        )
    return children


def render_task_list(children: list[EpicChild]) -> str:
    """Render :class:`EpicChild` entries back into task-list markdown."""
    lines = []
    for child in children:
        box = "x" if child.checked else " "
        line = f"- [{box}] #{child.number}"
        if child.depends_on:
            deps = ", ".join(f"#{n}" for n in child.depends_on)
            line += f" (depends on: {deps})"
        lines.append(line)
    return "\n".join(lines)


def epic_key(repo_slug: str, issue_number: int) -> str:
    """Build the ADR-013 §6 epic key: ``{repo_slug}-{issue_number}``.

    The repo slug is slugified into a single NATS token
    (``Owner/Repo`` → ``owner-repo``).
    """
    return f"{slugify(repo_slug)}-{issue_number}"
