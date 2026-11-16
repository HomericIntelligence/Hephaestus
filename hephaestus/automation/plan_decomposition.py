"""Validated single-pull-request work trees for plan decomposition.

The plan-review stage converts one stalled plan into an epic of independently
implementable issues. The decomposition agent proposes a work tree; this module
is the host-owned domain that parses, normalizes, and validates that proposal.
Model output is untrusted data: every field passes a closed schema, every path
passes the shared safe-path guard, and every verification command passes a
closed command grammar. Validation returns the complete error list so the
decomposition agent can replace one rejected draft.

Tree rules:

- every node maps to one or more parent requirement or finding identifiers;
- the complete tree covers every requirement and finding identifier;
- prerequisite edges form an acyclic graph with no self-dependency;
- two nodes that share a repository path must be related by a dependency
  path, so overlapping scope is always explained by the tree order;
- no configured child-count or tree-depth limit exists; text and payload
  limits are the single-pull-request leaf condition.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Final

from hephaestus.automation.pipeline.scope_retraction import (
    is_safe_scope_retraction_path,
)

SCHEMA_VERSION: Final[int] = 1

TITLE_MAX_CHARS: Final[int] = 120
OWNED_BEHAVIOR_MAX_CHARS: Final[int] = 4_000
FAILURE_BEHAVIOR_MAX_CHARS: Final[int] = 2_000
EVIDENCE_MAX_CHARS: Final[int] = 1_000
CRITERION_MAX_CHARS: Final[int] = 500
MAX_ACCEPTANCE_CRITERIA: Final[int] = 16
MAX_ACCEPTANCE_CHARS: Final[int] = 4_000
PATH_MAX_CHARS: Final[int] = 240
MAX_PATHS_PER_UNIT: Final[int] = 32
COMMAND_MAX_CHARS: Final[int] = 500
MAX_COMMANDS_PER_UNIT: Final[int] = 16
PREREQUISITE_MAX_CHARS: Final[int] = 64

NODE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^(?:req|find)-[0-9a-f]{12}$")

REQUIREMENT_ID_PREFIX: Final[str] = "req-"
FINDING_ID_PREFIX: Final[str] = "find-"

_CONTROL_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")

# Reserved control tokens keep model prose from forging verdicts, state
# labels, or comment markers inside a durable record.
_RESERVED_CONTROL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)"
    r"<!--|--!?>|"
    r"\bstate\s*:\s*[a-z-]+\b|"
    r"\bverdict\s*:\s*[^\r\n]*|"
    r"\bdecision\s*:\s*[^\r\n]*|"
    r"\bapproval\s*:\s*[^\r\n]*|"
    r"\bimplementation[\s_-]+(?:approval|rejection)\b[^\r\n]*|"
    r"\bcloses\s+#\d+\b|"
    r"\bfixes\s+#\d+\b|"
    r"\bresolves\s+#\d+\b",
)

# Closed command grammar: repository validation commands only.
_UV_RUN_TOOLS: Final[frozenset[str]] = frozenset({"pytest", "ruff", "mypy", "pre-commit"})
_SAFE_ARG_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_./@=,:+-]+$")
_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_JUST_RECIPE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_COMMAND_FORBIDDEN_CHARS: Final[frozenset[str]] = frozenset(set(";&|<>()$`{}[]*?~!#\\\n\r\t\"'"))

WORK_UNIT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "node_id",
        "title",
        "owned_behavior",
        "repository_paths",
        "acceptance_criteria",
        "failure_behavior",
        "verification_commands",
        "expected_evidence",
        "prerequisites",
        "requirement_ids",
        "finding_ids",
        "single_pr",
    }
)


def normalize_decomposition_text(value: object, *, max_chars: int) -> str | None:
    """Normalize one model-supplied text field and reject control tokens."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value).strip()
    if not text or len(text) > max_chars:
        return None
    if _CONTROL_RE.search(text) or _RESERVED_CONTROL_RE.search(text):
        return None
    return text


def normalize_decomposition_paths(value: object) -> tuple[str, ...] | None:
    """Validate one non-empty safe path manifest and return a stable tuple."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    normalized: list[str] = []
    for item in value:
        if not is_safe_scope_retraction_path(item) or len(item) > PATH_MAX_CHARS:
            return None
        path = unicodedata.normalize("NFC", item).strip()
        if path != item or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            return None
        normalized.append(path)
    unique = sorted(set(normalized))
    if len(unique) != len(normalized) or len(unique) > MAX_PATHS_PER_UNIT:
        return None
    return tuple(unique)


def _safe_command_argument(token: str) -> bool:
    """Return whether one command token is a bounded plain argument."""
    if not token or len(token) > COMMAND_MAX_CHARS:
        return False
    if _ENV_ASSIGNMENT_RE.match(token):
        return False
    if token.startswith("/") or ".." in token:
        return False
    return _SAFE_ARG_RE.fullmatch(token) is not None


def normalize_verification_command(value: object) -> str | None:
    """Validate one repository verification command with a closed grammar.

    Permit only focused ``uv run pytest``, ``uv run ruff check``,
    ``uv run ruff format --check``, ``uv run mypy``, ``uv run pre-commit run``,
    and named ``just`` recipes. Reject shell operators, redirections,
    substitutions, environment assignments, control characters, absolute
    paths, parent traversal, network tools, and inline interpreter code.
    """
    if not isinstance(value, str):
        return None
    command = unicodedata.normalize("NFC", value).strip()
    if not command or len(command) > COMMAND_MAX_CHARS:
        return None
    if _CONTROL_RE.search(command):
        return None
    if _COMMAND_FORBIDDEN_CHARS.intersection(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    executable = tokens[0]
    if executable == "uv":
        return _normalize_uv_command(tokens)
    if executable == "just":
        if len(tokens) < 2 or not _JUST_RECIPE_RE.fullmatch(tokens[1]):
            return None
        if not all(_safe_command_argument(token) for token in tokens[2:]):
            return None
        return command
    return None


def _normalize_uv_command(tokens: list[str]) -> str | None:
    """Validate one ``uv run`` tool invocation."""
    if len(tokens) < 3 or tokens[1] != "run":
        return None
    tool = tokens[2]
    if tool not in _UV_RUN_TOOLS:
        return None
    rest = tokens[3:]
    if tool == "ruff":
        if not rest or rest[0] not in {"check", "format"}:
            return None
        if rest[0] == "format" and "--check" not in rest:
            return None
        rest = rest[1:]
    if not all(_safe_command_argument(token) for token in rest):
        return None
    return " ".join(tokens)


def normalize_verification_commands(value: object) -> tuple[str, ...] | None:
    """Validate a non-empty bounded command manifest."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    normalized: list[str] = []
    for item in value:
        command = normalize_verification_command(item)
        if command is None:
            return None
        normalized.append(command)
    unique = tuple(dict.fromkeys(normalized))
    if len(unique) > MAX_COMMANDS_PER_UNIT:
        return None
    return unique


def requirement_identifier(text: str) -> str:
    """Derive the stable parent-requirement identifier for one criterion."""
    digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]
    return f"{REQUIREMENT_ID_PREFIX}{digest}"


def finding_identifier(text: str) -> str:
    """Derive the stable unresolved-finding identifier for one finding."""
    digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]
    return f"{FINDING_ID_PREFIX}{digest}"


@dataclass(frozen=True, slots=True)
class RequirementRef:
    """One parent requirement or finding and its stable identifier."""

    identifier: str
    digest: str

    def as_dict(self) -> dict[str, str]:
        """Return the canonical JSON-safe snapshot."""
        return {"identifier": self.identifier, "digest": self.digest}


def build_requirement_manifest(texts: Sequence[str]) -> tuple[RequirementRef, ...]:
    """Build the deduplicated requirement manifest for parent criteria."""
    refs: dict[str, RequirementRef] = {}
    for text in texts:
        identifier = requirement_identifier(text)
        digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
        refs.setdefault(identifier, RequirementRef(identifier, digest))
    return tuple(refs[identifier] for identifier in sorted(refs))


def build_finding_manifest(texts: Sequence[str]) -> tuple[RequirementRef, ...]:
    """Build the deduplicated finding manifest for unresolved review findings."""
    refs: dict[str, RequirementRef] = {}
    for text in texts:
        identifier = finding_identifier(text)
        digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
        refs.setdefault(identifier, RequirementRef(identifier, digest))
    return tuple(refs[identifier] for identifier in sorted(refs))


@dataclass(frozen=True, slots=True)
class WorkUnit:
    """One closed single-pull-request work item of a decomposition tree."""

    node_id: str
    title: str
    owned_behavior: str
    repository_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    failure_behavior: str
    verification_commands: tuple[str, ...]
    expected_evidence: str
    prerequisites: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    finding_ids: tuple[str, ...]
    single_pr: bool

    def as_dict(self) -> dict[str, object]:
        """Return the canonical JSON-safe snapshot."""
        return asdict(self)


def normalize_work_unit(value: object) -> WorkUnit | None:
    """Validate one mapping against the closed work-unit schema."""
    if not isinstance(value, Mapping):
        return None
    if set(value) != WORK_UNIT_FIELDS:
        return None
    node_id = value.get("node_id")
    if not isinstance(node_id, str) or NODE_ID_RE.fullmatch(node_id) is None:
        return None
    title = normalize_decomposition_text(value.get("title"), max_chars=TITLE_MAX_CHARS)
    behavior = normalize_decomposition_text(
        value.get("owned_behavior"), max_chars=OWNED_BEHAVIOR_MAX_CHARS
    )
    paths = normalize_decomposition_paths(value.get("repository_paths"))
    criteria = _normalize_criteria(value.get("acceptance_criteria"))
    failure = normalize_decomposition_text(
        value.get("failure_behavior"), max_chars=FAILURE_BEHAVIOR_MAX_CHARS
    )
    commands = normalize_verification_commands(value.get("verification_commands"))
    evidence = normalize_decomposition_text(
        value.get("expected_evidence"), max_chars=EVIDENCE_MAX_CHARS
    )
    prerequisites = _normalize_node_references(value.get("prerequisites"))
    requirement_ids = _normalize_identifiers(value.get("requirement_ids"))
    finding_ids = _normalize_identifiers(value.get("finding_ids"))
    single_pr = value.get("single_pr")
    if (
        title is None
        or behavior is None
        or paths is None
        or criteria is None
        or failure is None
        or commands is None
        or evidence is None
        or prerequisites is None
        or requirement_ids is None
        or finding_ids is None
        or single_pr is not True
    ):
        return None
    if not requirement_ids and not finding_ids:
        return None
    if node_id in prerequisites:
        return None
    return WorkUnit(
        node_id=node_id,
        title=title,
        owned_behavior=behavior,
        repository_paths=paths,
        acceptance_criteria=criteria,
        failure_behavior=failure,
        verification_commands=commands,
        expected_evidence=evidence,
        prerequisites=prerequisites,
        requirement_ids=requirement_ids,
        finding_ids=finding_ids,
        single_pr=True,
    )


def _normalize_criteria(value: object) -> tuple[str, ...] | None:
    """Validate a bounded non-empty acceptance-criteria manifest."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        criterion = normalize_decomposition_text(item, max_chars=CRITERION_MAX_CHARS)
        if criterion is None or criterion in seen:
            return None
        seen.add(criterion)
        normalized.append(criterion)
    if len(normalized) > MAX_ACCEPTANCE_CRITERIA:
        return None
    if sum(len(item) for item in normalized) > MAX_ACCEPTANCE_CHARS:
        return None
    return tuple(sorted(normalized))


def _normalize_node_references(value: object) -> tuple[str, ...] | None:
    """Validate a bounded node-reference manifest."""
    if not isinstance(value, (list, tuple)):
        return None
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or NODE_ID_RE.fullmatch(item) is None:
            return None
        normalized.append(item)
    unique = tuple(sorted(set(normalized)))
    if len(unique) != len(normalized) or len(unique) > 128:
        return None
    return unique


def _normalize_identifiers(value: object) -> tuple[str, ...] | None:
    """Validate a bounded stable-identifier manifest."""
    if not isinstance(value, (list, tuple)):
        return None
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or IDENTIFIER_RE.fullmatch(item) is None:
            return None
        normalized.append(item)
    unique = tuple(sorted(set(normalized)))
    if len(unique) != len(normalized):
        return None
    return unique


def normalize_work_tree(value: object) -> tuple[WorkUnit, ...] | None:
    """Parse and normalize a complete model-supplied work tree."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    units: list[WorkUnit] = []
    for item in value:
        unit = normalize_work_unit(item)
        if unit is None:
            return None
        units.append(unit)
    return tuple(units)


def work_tree_errors(
    units: Sequence[WorkUnit],
    *,
    requirement_ids: Sequence[str],
    finding_ids: Sequence[str],
) -> list[str]:
    """Return every tree-validation error for one normalized proposal."""
    errors: list[str] = []
    known_ids = {unit.node_id for unit in units}
    if len(known_ids) != len(units):
        errors.append("duplicate node_id values in the work tree")
    behaviors: dict[str, str] = {}
    for unit in units:
        if unit.owned_behavior in behaviors:
            errors.append(
                f"nodes {behaviors[unit.owned_behavior]} and {unit.node_id} "
                "declare duplicate owned behavior"
            )
        else:
            behaviors[unit.owned_behavior] = unit.node_id
    known_requirements = set(requirement_ids)
    known_findings = set(finding_ids)
    for unit in units:
        for prerequisite in unit.prerequisites:
            if prerequisite not in known_ids:
                errors.append(f"node {unit.node_id} names unknown prerequisite {prerequisite}")
        unknown = set(unit.requirement_ids) - known_requirements
        if unknown:
            errors.append(
                f"node {unit.node_id} maps unknown requirement identifiers {sorted(unknown)}"
            )
        unknown_findings = set(unit.finding_ids) - known_findings
        if unknown_findings:
            errors.append(
                f"node {unit.node_id} maps unknown finding identifiers {sorted(unknown_findings)}"
            )
    if not any(unit.requirement_ids or unit.finding_ids for unit in units):
        errors.append("no node maps to a parent requirement or finding identifier")
    covered = {
        identifier for unit in units for identifier in (*unit.requirement_ids, *unit.finding_ids)
    }
    missing = (known_requirements | known_findings) - covered
    if missing:
        errors.append(f"incomplete coverage; uncovered identifiers {sorted(missing)}")
    errors.extend(_overlap_errors(units))
    errors.extend(_cycle_errors(units))
    return errors


def _overlap_errors(units: Sequence[WorkUnit]) -> list[str]:
    """Reject shared paths between nodes that no dependency path relates."""
    errors: list[str] = []
    reachable = _reachability(units)
    for index, unit in enumerate(units):
        for other in units[index + 1 :]:
            shared = set(unit.repository_paths) & set(other.repository_paths)
            if not shared:
                continue
            forward = other.node_id in reachable[unit.node_id]
            backward = unit.node_id in reachable[other.node_id]
            if not forward and not backward:
                errors.append(
                    f"nodes {unit.node_id} and {other.node_id} share unexplained "
                    f"overlapping scope {sorted(shared)}"
                )
    return errors


def _reachability(units: Sequence[WorkUnit]) -> dict[str, set[str]]:
    """Return the transitive dependency descendants of every node."""
    edges: dict[str, list[str]] = {unit.node_id: [] for unit in units}
    dependents: dict[str, list[str]] = {unit.node_id: [] for unit in units}
    for unit in units:
        for prerequisite in unit.prerequisites:
            if prerequisite in edges:
                edges[prerequisite].append(unit.node_id)
                dependents[unit.node_id].append(prerequisite)
    reachable: dict[str, set[str]] = {}
    for start in edges:
        seen: set[str] = set()
        stack = [*edges[start]]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(edges[current])
        reachable[start] = seen
    return reachable


def _cycle_errors(units: Sequence[WorkUnit]) -> list[str]:
    """Return cycle errors found by an iterative Kahn topological sort."""
    layers = topological_layers(units)
    if layers is not None:
        return []
    return ["dependency graph contains a cycle"]


def topological_layers(
    units: Sequence[WorkUnit],
) -> tuple[tuple[str, ...], ...] | None:
    """Return node IDs grouped into dependency layers, or None on a cycle.

    The sort is iterative, so a deep tree cannot exhaust the Python call
    stack. Root nodes (no prerequisites inside the tree) form the first
    layer; each later layer contains only nodes whose direct prerequisites
    all appear in earlier layers.
    """
    ids = {unit.node_id for unit in units}
    if len(ids) != len(units):
        return None
    pending = {unit.node_id: {p for p in unit.prerequisites if p in ids} for unit in units}
    dependents: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for node_id, prerequisites in pending.items():
        for prerequisite in prerequisites:
            dependents[prerequisite].append(node_id)
    layers: list[tuple[str, ...]] = []
    ready = sorted(node_id for node_id, prerequisites in pending.items() if not prerequisites)
    resolved = 0
    while ready:
        layers.append(tuple(ready))
        resolved += len(ready)
        next_ready: list[str] = []
        for node_id in ready:
            for dependent in dependents[node_id]:
                pending[dependent].discard(node_id)
                if not pending[dependent]:
                    next_ready.append(dependent)
        ready = sorted(set(next_ready))
    if resolved != len(ids):
        return None
    return tuple(layers)


def tree_digest(units: Sequence[WorkUnit]) -> str:
    """Return the stable digest of one complete normalized work tree."""
    payload = json.dumps(
        sorted((unit.as_dict() for unit in units), key=lambda unit: unit["node_id"]),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render_child_body(
    *,
    parent_issue: int,
    unit: WorkUnit,
    prerequisite_issues: Mapping[str, int],
    plan_revision: int,
    requirements_epoch: str,
    marker: str,
) -> str:
    """Render the complete child issue body from sealed normalized data."""
    lines = [
        f"Part of epic #{parent_issue}",
        "",
        "## Owned behavior",
        "",
        unit.owned_behavior,
        "",
        "## Repository paths",
        "",
    ]
    lines.extend(f"- `{path}`" for path in unit.repository_paths)
    lines.extend(
        [
            "",
            "## Acceptance criteria",
            "",
        ]
    )
    lines.extend(f"- {criterion}" for criterion in unit.acceptance_criteria)
    lines.extend(
        [
            "",
            "## Failure behavior",
            "",
            unit.failure_behavior,
            "",
            "## Verification commands",
            "",
        ]
    )
    lines.extend(f"- `{command}`" for command in unit.verification_commands)
    lines.extend(
        [
            "",
            "## Expected evidence",
            "",
            unit.expected_evidence,
            "",
            "## Dependencies",
            "",
        ]
    )
    for prerequisite in unit.prerequisites:
        issue = prerequisite_issues.get(prerequisite)
        if issue is not None:
            lines.append(f"- Depends on #{issue}")
    lines.extend(
        [
            "",
            "## Provenance",
            "",
            f"- Source plan revision: {plan_revision}",
            f"- Requirements epoch: {requirements_epoch}",
            f"- Requirement identifiers: {', '.join(unit.requirement_ids) or 'none'}",
            f"- Finding identifiers: {', '.join(unit.finding_ids) or 'none'}",
            "",
            marker,
            "",
        ]
    )
    return "\n".join(lines)


def child_recovery_marker(
    *,
    repository: str,
    parent_issue: int,
    requirements_epoch: str,
    generation: int,
    node_id: str,
) -> str:
    """Build the deterministic host-owned recovery marker for one child."""
    return (
        f"<!-- hephaestus-decomposition-child:{repository}:{parent_issue}"
        f":{requirements_epoch}:{generation}:{node_id} -->"
    )


def parse_child_marker(marker: str) -> dict[str, str] | None:
    """Parse one child recovery marker back into its identity fields."""
    prefix = "<!-- hephaestus-decomposition-child:"
    if not marker.startswith(prefix) or not marker.rstrip().endswith("-->"):
        return None
    payload = marker.strip()
    payload = payload[len(prefix) : -3].strip()
    parts = payload.split(":")
    if len(parts) != 5:
        return None
    repository, parent, epoch, generation, node_id = parts
    if not repository or not parent.isdigit() or not epoch or not generation.isdigit():
        return None
    if NODE_ID_RE.fullmatch(node_id) is None:
        return None
    return {
        "repository": repository,
        "parent_issue": parent,
        "requirements_epoch": epoch,
        "generation": generation,
        "node_id": node_id,
    }
