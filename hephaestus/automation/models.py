"""Store current issue identities and dependency graphs."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

DEFAULT_WORKER_COUNT = 3
DEFAULT_STATE_DIR = "build/.issue_implementer"


class IssueState(StrEnum):
    """Store the GitHub issue or PR state.

    An issue dependency can refer to a merged PR. MERGED is thus a valid
    dependency state.
    """

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"

    @property
    def is_done(self) -> bool:
        """Return True for a closed issue or merged PR."""
        return self in (IssueState.CLOSED, IssueState.MERGED)


class IssueInfo(BaseModel):
    """Information about a GitHub issue."""

    number: int
    title: str
    body: str = ""
    state: IssueState = IssueState.OPEN
    labels: list[str] = Field(default_factory=list)
    dependencies: list[int] = Field(default_factory=list)
    priority: int = 0
    authority_sanitized: bool = False

    def __hash__(self) -> int:
        """Make IssueInfo hashable for use in sets."""
        return hash(self.number)

    def __eq__(self, other: object) -> bool:
        """Compare issues by number."""
        if not isinstance(other, IssueInfo):
            return NotImplemented
        return self.number == other.number


class DependencyGraph(BaseModel):
    """Dependency graph for issues."""

    issues: dict[int, IssueInfo] = Field(default_factory=dict)
    edges: dict[int, list[int]] = Field(default_factory=dict)  # issue_number -> dependencies

    def add_issue(self, issue: IssueInfo) -> None:
        """Add an issue to the graph."""
        self.issues[issue.number] = issue
        if issue.number not in self.edges:
            self.edges[issue.number] = []

    def add_dependency(self, issue_number: int, depends_on: int) -> None:
        """Add an issue dependency.

        The dependency issue can be added to the graph later.

        Args:
            issue_number: Issue that requires the dependency.
            depends_on: Issue that must finish first.

        Raises:
            ValueError: The source issue is absent from the graph.

        """
        if issue_number not in self.issues:
            raise ValueError(f"Issue #{issue_number} not in graph")

        if issue_number not in self.edges:
            self.edges[issue_number] = []
        if depends_on not in self.edges[issue_number]:
            self.edges[issue_number].append(depends_on)

    def get_dependencies(self, issue_number: int) -> list[int]:
        """Get direct dependencies for an issue."""
        return self.edges.get(issue_number, [])

    def get_all_dependencies(self, issue_number: int) -> set[int]:
        """Get all transitive dependencies for an issue."""
        deps: set[int] = set()
        to_visit = [issue_number]
        visited: set[int] = set()

        while to_visit:
            current = to_visit.pop()
            if current in visited:
                continue
            visited.add(current)

            for dep in self.get_dependencies(current):
                deps.add(dep)
                to_visit.append(dep)

        return deps
