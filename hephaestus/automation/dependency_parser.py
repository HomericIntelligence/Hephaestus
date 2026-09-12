"""Parse dependency declarations without GitHub or other external access."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

MAX_DEPENDENCY_FACTS = 100


@dataclass(frozen=True)
class DependencyFact:
    """Immutable lifecycle facts for one repository dependency node."""

    number: int
    typename: Literal["Issue", "PullRequest"]
    state: Literal["OPEN", "CLOSED"]
    merged: bool | None = None

    def __post_init__(self) -> None:
        """Reject a fact whose identity or lifecycle is contradictory."""
        if type(self.number) is not int or self.number <= 0:
            raise ValueError("dependency number must be a positive integer")
        if type(self.typename) is not str or self.typename not in {"Issue", "PullRequest"}:
            raise ValueError("dependency typename is invalid")
        if type(self.state) is not str or self.state not in {"OPEN", "CLOSED"}:
            raise ValueError("dependency state is invalid")
        if self.typename == "Issue":
            if self.merged is not None:
                raise ValueError("issue dependency cannot have merged state")
        elif type(self.merged) is not bool:
            raise ValueError("pull-request dependency merged state is invalid")
        if self.typename == "PullRequest" and self.state == "OPEN" and self.merged:
            raise ValueError("open pull request cannot be merged")

    @property
    def satisfied(self) -> bool:
        """Return whether this node proves a completed dependency."""
        return self.state == "CLOSED" and (self.typename == "Issue" or self.merged is True)


def canonical_dependency_numbers(dependencies: Sequence[int]) -> tuple[int, ...]:
    """Validate and return positive dependency numbers in canonical order."""
    numbers = tuple(dependencies)
    if len(numbers) > MAX_DEPENDENCY_FACTS:
        raise ValueError("dependency batch exceeds the bounded maximum")
    if any(type(number) is not int or number <= 0 for number in numbers):
        raise ValueError("dependency numbers must be positive integers")
    if len(set(numbers)) != len(numbers):
        raise ValueError("dependency numbers must be unique")
    if numbers != tuple(sorted(numbers)):
        raise ValueError("dependency numbers must be in canonical order")
    return numbers


def parse_issue_dependencies(issue_body: str) -> list[int]:
    """Return the unique issue numbers named as dependencies in *issue_body*.

    The parser reads references after dependency keywords and list entries in
    a ``Dependencies`` section. It does not read GitHub or make any other
    external call.
    """
    dependencies: list[int] = []

    # Read only references after the dependency keyword on each line. A
    # preceding epic reference is not a dependency declaration.
    dep_keywords = r"(?:depends on|blocked by|requires|dependencies?:?)"
    keyword_re = re.compile(dep_keywords, re.IGNORECASE)
    for line in issue_body.split("\n"):
        keyword_match = keyword_re.search(line)
        if keyword_match:
            for match in re.finditer(r"#(\d+)", line[keyword_match.start() :]):
                dependencies.append(int(match.group(1)))

    # Also read issue references in list items under a Dependencies heading.
    dep_section_match = re.search(
        r"##\s*Dependencies.*?\n(.*?)(?=##|\Z)", issue_body, re.IGNORECASE | re.DOTALL
    )
    if dep_section_match:
        list_pattern = r"^\s*[-*]\s*#(\d+)"
        for match in re.finditer(list_pattern, dep_section_match.group(1), re.MULTILINE):
            dependencies.append(int(match.group(1)))

    return list(dict.fromkeys(dependencies))


__all__ = [
    "MAX_DEPENDENCY_FACTS",
    "DependencyFact",
    "canonical_dependency_numbers",
    "parse_issue_dependencies",
]
