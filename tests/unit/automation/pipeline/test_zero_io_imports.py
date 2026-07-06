"""Guard that pipeline modules have zero I/O imports (ast-based, not text-scan)."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import hephaestus.automation.pipeline as pkg

_PIPELINE_DIR = Path(pkg.__file__).parent

# Modules whose mere import implies (or enables) I/O / shelling out.
_FORBIDDEN_MODULES = {
    "subprocess",
    "os",
    "socket",
    "shutil",
    "urllib",
    "http",
    "requests",
    "httpx",
    "asyncio",
    "pty",
    "fcntl",
    "tempfile",
}
_FORBIDDEN_PREFIXES = (
    "hephaestus.automation.github_api",
    "hephaestus.automation.claude_invoke",
    "hephaestus.github",
    "hephaestus.automation.git_utils",
    # Both wrap subprocess execution; importing them from the pure-data layer
    # would smuggle shell-out capability past the stdlib forbid list.
    "hephaestus.utils",
    "hephaestus.resilience",
)

# Modules exempt from the zero-I/O guard entirely.
# worker_pool.py is the ONLY place that executes jobs (agent, git, build/test),
# so it must import I/O modules; all other workers offload to it.
_ALLOWLIST = frozenset({"worker_pool.py"})

# Capability-scoped exemptions: seeding.py and admission.py are the sanctioned
# "thin fetch over github_api" layer (epic #1809 PR-4): they READ GitHub facts
# for the classifier/serializer but perform no mutations (the AST mutator guard
# in test_pipeline_architecture.py still applies to them). Their exemption is
# NOT module-wide — only the read-seam prefixes below are permitted, so a
# direct `import subprocess` / `import os` in either file still trips the
# guard.
_THIN_FETCH_PREFIXES = (
    "hephaestus.automation.github_api",
    "hephaestus.automation._review_utils",
    "hephaestus.automation.state_labels",
    "hephaestus.automation.dependency_resolver",
)
# Each exemption maps a permitted prefix to an allowed-symbols set, or None
# for an unscoped (whole-prefix) exemption. A symbol-scoped prefix only
# permits `from <prefix...> import <allowed symbol>` — a bare
# `import <prefix>` or a from-import of any other symbol still trips.
_CAPABILITY_EXEMPT: dict[str, dict[str, frozenset[str] | None]] = {
    "seeding.py": dict.fromkeys(_THIN_FETCH_PREFIXES),
    "admission.py": dict.fromkeys(_THIN_FETCH_PREFIXES),
    # stages/plan_review.py may import ONLY the pure verdict parser pieces
    # (claude_invoke.parse_review_verdict — the ONLY allowed symbol) to attach as
    # AgentJob.parse — the architecture doc's plan_review contract says the
    # "verdict [is] parsed in-worker by claude_invoke.parse_review_verdict"
    # (#1814). The exemption is SYMBOL-scoped: importing any other
    # claude_invoke symbol (e.g. invoke_claude_with_session) or the module
    # itself still trips the guard, as does any other I/O-flavored import.
    "plan_review.py": {"hephaestus.automation.claude_invoke": frozenset({"parse_review_verdict"})},
    # stages/pr_review.py gets the SAME symbol-scoped exemption for the same
    # reason: the architecture doc's pr_review contract parses the reviewer
    # verdict in-worker (#1815), so the stage attaches parse_review_verdict
    # as AgentJob.parse. No other claude_invoke symbol is permitted.
    "pr_review.py": {"hephaestus.automation.claude_invoke": frozenset({"parse_review_verdict"})},
}


def _forbidden(name: str) -> bool:
    """Check if a module name is forbidden."""
    root = name.split(".")[0]
    return root in _FORBIDDEN_MODULES or name.startswith(_FORBIDDEN_PREFIXES)


def _matching_prefix(module: str, exempt: dict[str, frozenset[str] | None]) -> str | None:
    """Return the exempt prefix that covers *module*, or None."""
    for prefix in exempt:
        if module.startswith(prefix):
            return prefix
    return None


def _import_violations(
    node: ast.Import, filename: str, exempt: dict[str, frozenset[str] | None]
) -> list[str]:
    """Violations for a plain ``import X`` statement.

    Only an UNscoped exemption can permit a whole-module import: a bare
    import of a symbol-scoped prefix exposes the whole module surface.
    """
    violations = []
    for alias in node.names:
        if not _forbidden(alias.name):
            continue
        prefix = _matching_prefix(alias.name, exempt)
        if prefix is not None and exempt[prefix] is None:
            continue
        violations.append(f"{filename}:{node.lineno}: import {alias.name}")
    return violations


def _import_from_violations(
    node: ast.ImportFrom, filename: str, exempt: dict[str, frozenset[str] | None]
) -> list[str]:
    """Violations for a ``from X import Y`` statement (symbol scoping applies)."""
    mod = node.module or ""
    if not _forbidden(mod):
        return []
    prefix = _matching_prefix(mod, exempt)
    if prefix is None:
        return [f"{filename}:{node.lineno}: from {mod} import ..."]
    allowed = exempt[prefix]
    if allowed is None:
        return []  # unscoped exemption: whole prefix permitted
    return [
        f"{filename}:{node.lineno}: from {mod} import {alias.name} "
        f"(symbol not in allowed set {sorted(allowed)})"
        for alias in node.names
        if alias.name not in allowed
    ]


def _collect_violations(
    tree: ast.AST, filename: str, exempt: dict[str, frozenset[str] | None]
) -> list[str]:
    """Walk *tree* and return forbidden-import violations, honoring *exempt*.

    ``exempt`` maps permitted module prefixes to an allowed-symbols set
    (``None`` = whole prefix permitted). Symbol scoping applies to
    ``from X import Y`` only: each imported name must be in the allowed set.
    A plain ``import X`` of a symbol-scoped prefix exposes the whole module
    surface, so it is always a violation.
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(_import_violations(node, filename, exempt))
        elif isinstance(node, ast.ImportFrom):
            violations.extend(_import_from_violations(node, filename, exempt))
    return violations


def test_pipeline_modules_have_zero_io_imports() -> None:
    """Verify pipeline modules do not import I/O-related modules.

    Uses AST parsing to detect imports anywhere in the module (including
    inside function bodies), not just at the top level. This catches
    conditional and lazy imports that a text-scan would miss.

    Exceptions: worker_pool.py is the only place that executes jobs
    (agent, git, build/test), so it must import I/O modules; seeding.py and
    admission.py get a capability-scoped exemption for their sanctioned
    read seams only (see ``_CAPABILITY_EXEMPT``).
    """
    violations: list[str] = []
    # rglob so future pipeline/ subpackages (e.g. stages/) stay guarded.
    for py in sorted(_PIPELINE_DIR.rglob("*.py")):
        if py.name in _ALLOWLIST:
            continue
        exempt = _CAPABILITY_EXEMPT.get(py.name, {})
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        violations.extend(_collect_violations(tree, py.name, exempt))
    assert not violations, "pipeline modules must do zero I/O imports:\n" + "\n".join(violations)


def test_pipeline_package_import_stays_lazy() -> None:
    """Importing the package must not eagerly load coordinator I/O dependencies."""
    probe = (
        "import json, sys\n"
        "import hephaestus.automation.pipeline\n"
        "watched = [\n"
        "  'hephaestus.automation.pipeline.coordinator',\n"
        "  'hephaestus.automation.github_api',\n"
        "  'hephaestus.automation.claude_invoke',\n"
        "]\n"
        "print(json.dumps([name for name in watched if name in sys.modules]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(result.stdout) == []


def test_capability_scope_still_blocks_io_in_seeding() -> None:
    """The seeding/admission exemption is capability-scoped, not module-wide.

    A synthetic seeding.py that imports subprocess/os alongside its sanctioned
    read seams must still be flagged for the I/O modules — only imports under
    the ``_THIN_FETCH_PREFIXES`` capability prefixes escape the guard.
    """
    synthetic_source = (
        "import subprocess\n"
        "import os\n"
        "from hephaestus.automation.github_api import fetch_issue_info\n"
        "from hephaestus.automation._review_utils import find_pr_for_issue\n"
        "from hephaestus.automation.state_labels import is_epic\n"
        "from hephaestus.automation.dependency_resolver import DependencyResolver\n"
        "import hephaestus.utils.helpers\n"  # NOT a sanctioned seam — must trip
    )
    tree = ast.parse(synthetic_source, filename="<synthetic-seeding>")
    violations = _collect_violations(tree, "seeding.py", _CAPABILITY_EXEMPT["seeding.py"])

    assert any("import subprocess" in v for v in violations)
    assert any("import os" in v for v in violations)
    assert any("hephaestus.utils.helpers" in v for v in violations)
    assert not any("github_api" in v for v in violations)
    assert not any("_review_utils" in v for v in violations)
    assert not any("state_labels" in v for v in violations)
    assert not any("dependency_resolver" in v for v in violations)


def test_plan_review_exemption_is_symbol_scoped() -> None:
    """The plan_review.py claude_invoke exemption permits ONLY parse_review_verdict.

    A synthetic plan_review.py importing ReviewVerdict, an execution symbol
    (invoke_claude_with_session), or the whole claude_invoke module must
    all be flagged. Only the sanctioned parse_review_verdict from-import passes.
    """
    synthetic_source = (
        "from hephaestus.automation.claude_invoke import parse_review_verdict\n"
        "from hephaestus.automation.claude_invoke import ReviewVerdict\n"
        "from hephaestus.automation.claude_invoke import invoke_claude_with_session\n"
        "import hephaestus.automation.claude_invoke\n"
    )
    tree = ast.parse(synthetic_source, filename="<synthetic-plan-review>")
    violations = _collect_violations(tree, "plan_review.py", _CAPABILITY_EXEMPT["plan_review.py"])

    # Lines 2, 3, and 4 violate the tightened exemption (only parse_review_verdict
    # is allowed). Line 1 (parse_review_verdict) produces no violation.
    assert len(violations) == 3, violations
    assert any("import ReviewVerdict" in v for v in violations)
    assert any("import invoke_claude_with_session" in v for v in violations)
    # A bare module import exposes the whole surface: always a violation
    # under a symbol-scoped exemption.
    assert any(
        v.startswith("plan_review.py:4: import hephaestus.automation.claude_invoke")
        for v in violations
    )
    assert not any(":1:" in v for v in violations)


def test_forbidden_detects_synthetic_forbidden_import() -> None:
    """Negative test: the guard must actually flag forbidden imports.

    Without this test, a broken `_forbidden()` that always returns False
    would let `test_pipeline_modules_have_zero_io_imports` pass vacuously
    with an empty `violations` list. Here we parse synthetic source
    containing known-forbidden imports (stdlib module, forbidden prefix,
    and a from-import) through the same collector (no exemptions) and
    assert each is caught, plus that an allowed import is not.
    """
    synthetic_source = (
        "import subprocess\n"
        "import hephaestus.automation.git_utils\n"
        "from os import path\n"
        "import json\n"  # allowed stdlib module; must NOT be flagged
    )
    tree = ast.parse(synthetic_source, filename="<synthetic>")
    violations = _collect_violations(tree, "<synthetic>", {})

    assert any("import subprocess" in v for v in violations)
    assert any("import hephaestus.automation.git_utils" in v for v in violations)
    assert any("from os import ..." in v for v in violations)
    assert not any("json" in v for v in violations)


def test_forbidden_direct_cases() -> None:
    """Directly exercise `_forbidden()` for both branches of its predicate.

    Covers: a bare forbidden stdlib module, a submodule of a forbidden
    stdlib module (root-splitting), a forbidden dotted prefix, and an
    allowed module that must return False.
    """
    assert _forbidden("subprocess") is True
    assert _forbidden("os.path") is True
    assert _forbidden("hephaestus.automation.claude_invoke") is True
    assert _forbidden("hephaestus.automation.claude_invoke.helpers") is True
    assert _forbidden("json") is False
    assert _forbidden("hephaestus.automation.pipeline") is False
