"""Guard the automation namespace exports.

The two hub modules keep their ``__all__`` lists to module-local and
re-exported names: no entry may be bound by a standard-library or
``__future__`` import, and every private entry must be listed here as
explicitly intended before it may stay exported.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AUTOMATION_DIR = REPO_ROOT / "hephaestus" / "automation"

PR_REVIEW_THREADS_HUB = (
    AUTOMATION_DIR / "pipeline" / "stages" / "pr_review" / "pr_review_threads.py"
)
PIPELINE_GITHUB_TRANSPORT_HUB = AUTOMATION_DIR / "pipeline_github_transport.py"

STDLIB_MODULES = frozenset(sys.stdlib_module_names) | {"__future__"}

# Private names the pipeline collaborators import from hub to hub. A hub
# ``__all__`` may export a private name only when it is listed here.
PR_REVIEW_THREADS_INTENDED_PRIVATES = frozenset(
    {
        "_COMMENT_VALIDATION_ONLY",
        "_ROUND_PAYLOAD_KEYS",
        "_STEP_HANDLER_NAMES",
        "_THREAD_READ_RETRIES",
        "_ParsedReviewResponse",
        "_PrReviewHost",
        "_clear_round_review_state",
        "_durable_thread_id",
        "_issue_number",
        "_normalize_remediation_threads",
        "_parse_review_response",
        "_parse_validation_result",
        "_review_context_kind",
        "_scope_retraction_paths",
        "_thread_snapshots",
        "_validation_decisions",
        "_worktree_path",
    }
)

PIPELINE_GITHUB_TRANSPORT_INTENDED_PRIVATES = frozenset(
    {
        "_CLOSES_ISSUE_LINE_RE",
        "_FULL_COMMIT_SHA_RE",
        "_HTTP_STATUS_RE",
        "_IMPLEMENTATION_REPLY_BODY_RE",
        "_STANDALONE_VERDICT_LINE_RE",
        "_parse_included_http_response",
    }
)


def _top_level_statements(tree: ast.Module | ast.If) -> Iterator[ast.stmt]:
    """Yield the module statements, recursing into top-level branch blocks."""
    for stmt in tree.body:
        yield stmt
        if isinstance(stmt, ast.If):
            yield from _top_level_statements(stmt)


def _stdlib_bound_names(tree: ast.Module) -> set[str]:
    """Return names bound by a standard-library or future import."""
    bound: set[str] = set()
    for stmt in _top_level_statements(tree):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name.split(".", maxsplit=1)[0] in STDLIB_MODULES:
                    bound.add(alias.asname or alias.name.split(".", maxsplit=1)[0])
        elif isinstance(stmt, ast.ImportFrom):
            root = stmt.module.split(".", maxsplit=1)[0] if stmt.module else ""
            if root in STDLIB_MODULES:
                for alias in stmt.names:
                    bound.add(alias.asname or alias.name)
    return bound


def _all_entries(tree: ast.Module) -> list[str]:
    """Return the string entries of the module-level ``__all__``."""
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if getattr(stmt.targets[0], "id", None) != "__all__":
            continue
        if not isinstance(stmt.value, (ast.List, ast.Tuple)):
            continue
        constants = [elt.value for elt in stmt.value.elts if isinstance(elt, ast.Constant)]
        return [value for value in constants if isinstance(value, str)]
    raise AssertionError("no module-level __all__ assignment")


def _hub_violations(source: str, intended_privates: frozenset[str]) -> list[str]:
    """List the hygiene violations in one hub ``__all__``."""
    tree = ast.parse(source)
    stdlib_bound = _stdlib_bound_names(tree)
    violations: list[str] = []
    for entry in _all_entries(tree):
        if entry in stdlib_bound:
            violations.append(f"stdlib-bound entry {entry!r} in __all__")
        elif entry.startswith("_") and entry not in intended_privates:
            violations.append(f"private entry {entry!r} not explicitly intended")
    return violations


def _violations_for(path: Path, intended_privates: frozenset[str]) -> list[str]:
    """Syntax-check one hub source file for the export hygiene rules."""
    return _hub_violations(path.read_text(), intended_privates)


def test_pr_review_threads_hub_has_no_stdlib_exports() -> None:
    """The pr_review_threads hub exports nothing bound by a stdlib import."""
    names = _all_entries(ast.parse(PR_REVIEW_THREADS_HUB.read_text()))
    assert names
    violations = _violations_for(PR_REVIEW_THREADS_HUB, PR_REVIEW_THREADS_INTENDED_PRIVATES)
    assert not [v for v in violations if "stdlib" in v]


def test_pr_review_threads_hub_privates_are_intended() -> None:
    """Each private pr_review_threads export is explicitly intended."""
    violations = _violations_for(PR_REVIEW_THREADS_HUB, PR_REVIEW_THREADS_INTENDED_PRIVATES)
    assert not [v for v in violations if "private" in v]


def test_pipeline_github_transport_hub_has_no_stdlib_exports() -> None:
    """The pipeline_github_transport hub exports nothing bound by a stdlib import."""
    names = _all_entries(ast.parse(PIPELINE_GITHUB_TRANSPORT_HUB.read_text()))
    assert names
    violations = _violations_for(
        PIPELINE_GITHUB_TRANSPORT_HUB, PIPELINE_GITHUB_TRANSPORT_INTENDED_PRIVATES
    )
    assert not [v for v in violations if "stdlib" in v]


def test_pipeline_github_transport_hub_privates_are_intended() -> None:
    """Each private pipeline_github_transport export is explicitly intended."""
    violations = _violations_for(
        PIPELINE_GITHUB_TRANSPORT_HUB, PIPELINE_GITHUB_TRANSPORT_INTENDED_PRIVATES
    )
    assert not [v for v in violations if "private" in v]


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(
            'import json\n__all__ = ["json"]\n',
            ["stdlib-bound entry 'json' in __all__"],
            id="stdlib-module-entry",
        ),
        pytest.param(
            "from __future__ import annotations\n__all__ = ['annotations']\n",
            ["stdlib-bound entry 'annotations' in __all__"],
            id="future-entry",
        ),
        pytest.param(
            "from typing import Any\n__all__ = ['Any']\n",
            ["stdlib-bound entry 'Any' in __all__"],
            id="typing-alias-entry",
        ),
        pytest.param(
            "_helper = 1\n__all__ = ['_helper']\n",
            ["private entry '_helper' not explicitly intended"],
            id="unintended-private",
        ),
        pytest.param(
            "def helper():\n    pass\n__all__ = ['helper']\n",
            [],
            id="clean-local-export",
        ),
        pytest.param(
            "import json\nimport os\n__all__ = ['json', 'os']\n",
            ["stdlib-bound entry 'json' in __all__", "stdlib-bound entry 'os' in __all__"],
            id="multiple-stdlib-entries",
        ),
    ],
)
def test_hub_export_hygiene(source: str, expected: list[str], tmp_path: Path) -> None:
    """The hygiene switch flags synthetic fixture violations, and accepts clean ones."""
    fixture = tmp_path / "synthetic_hub.py"
    fixture.write_text(source)
    assert _violations_for(fixture, PR_REVIEW_THREADS_INTENDED_PRIVATES) == expected


def test_no_star_imports_or_f403_noqa_in_automation() -> None:
    """No module under automation re-imports a hub with import star or hides F403."""
    violations: list[str] = []
    for path in sorted(AUTOMATION_DIR.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("from ") and stripped.endswith(" import *"):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno} star import")
            if "F403" in stripped and "noqa" in stripped.lower():
                violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno} F403 noqa")
    assert violations == []
