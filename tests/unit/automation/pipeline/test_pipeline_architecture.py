"""Architecture invariants for coordinator and closed GitHub worker access.

Models tests/unit/automation/test_ci_driver_architecture.py. Enforces via AST
walk that pipeline/* modules never import or call github_api mutator functions,
with an explicit allowlist for documented interim offenders awaiting refactor.
The generic worker modules remain implementation-neutral. GitHub worker I/O is
admitted only through frozen closed requests and the sole production runner,
which constructs a fresh repository-scoped accessor for every job.

Caveat: this is a static, import/attribute-level guard. Subprocess-level ``gh``
calls (e.g. building a ``["gh", ...]`` argv and running it directly) are OUT OF
SCOPE here and must be caught in code review.
Coordinator-neutral ``ctx.github`` mutator names are also out of scope here:
they are bound by the coordinator through ``StageGitHub`` and verified by the
PipelineGitHub mapping tests.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import hephaestus
import hephaestus.automation.github_api as github_api

_PIPELINE = Path(hephaestus.__file__).parent / "automation" / "pipeline"
_AUTOMATION = _PIPELINE.parent

# Public mutators from github_api.__all__ (avoids hardcoding drift).
# Scope note: this guard only tracks direct github_api mutator names. The
# coordinator-neutral ``ctx.github`` surface is intentionally covered by the
# coordinator adapter tests instead. The recursive
# ``_stage_auto_merge_callers`` guard covers the live auto-merge mutation
# names ``enable_auto_merge`` and ``disable_auto_merge``, and
# ``_PUBLIC_MUTATORS`` stays limited to ``github_api.__all__``. The test
# ``tests/unit/automation/pipeline/test_pipeline_architecture.py::
# test_pipeline_stages_have_no_auto_merge_mutation_capability`` enforces the
# auto-merge ban.
_PUBLIC_MUTATORS = frozenset(
    n
    for n in github_api.__all__
    if n.startswith(
        (
            "gh_issue_add",
            "gh_issue_remove",
            "gh_issue_comment",
            "gh_issue_create",
            "gh_issue_delete",
            "gh_issue_upsert",
            "gh_create_label",
            "gh_pr_create",
            "gh_pr_review_post",
            "gh_pr_update_review",
            "gh_pr_resolve",
        )
    )
)

# Private review-comment mutators that carry no public alias
_PRIVATE_MUTATORS = frozenset({"_post_shadow_review_comment", "_edit_or_keep_comments"})

_MUTATORS = _PUBLIC_MUTATORS | _PRIVATE_MUTATORS

# The empty allowlist is intentional: pipeline modules may not contain direct
# GitHub mutations; all writes pass through the coordinator adapter.
_ALLOWLIST: frozenset[str] = frozenset()

# Worker-side modules: code that executes ON worker threads. These may not
# import github_api or pr_manager at all (not even non-mutator helpers) so
# that gh_call/run/skip_epics-style mutators can never sneak in.
_WORKER_SIDE_MODULES = (
    "auxiliary_worker_pool.py",
    "git_cleanup.py",
    "jobs.py",
    "worker_pool.py",
)
_FORBIDDEN_WORKER_MODULES = ("github_api", "pr_manager")

# The clock-owner pin: exactly one stage module may ``import time``. The pin
# names the path components of the owner file relative to the pipeline package:
# ``stage_base.py``, the module split out of the old flat ``stages/base.py`` in
# #3579. A module that merely shares the stem is not the owner.
# Update this pin in the same change that moves the clock owner.
_CLOCK_OWNER = ("stage_base.py",)

# One package per stage (issue #3521). The stage lives in
# ``stages/<name>/<name>.py``; ``stages/<name>.py`` is only a flat re-export
# that keeps the historical import path working.
_STAGE_PACKAGES = (
    "finished",
    "implementation",
    "learning",
    "merge_wait",
    "plan_review",
    "planning",
    "pr_review",
    "repo",
)

# The one intra-stage-package edge that the architecture keeps: the plan review
# stage reads the planner's prompt builder.
_ALLOWED_STAGE_PACKAGE_EDGES = frozenset({("plan_review", "planning")})

# Shared modules (not stages) allowed to sit at the top of ``stages/``.
# ``durable_request`` holds the pending-request key and its two read
# predicates, which several stages import without owning a stage of their own.
_STAGE_FLAT_ALLOWED = frozenset({"__init__.py", "durable_request.py"})


def _stage_layout_offenders(root: Path) -> list[str]:
    """Return every stage module that is not in a stage package.

    The stage layout is ``stages/__init__.py`` plus the eight stage packages.
    A top-level ``stages/<name>.py`` is allowed only when it is one of the eight
    declared re-export modules. Any other flat module, and any module in a
    subdirectory that is not a declared stage package, is an offender.
    """
    stages = root / "stages"
    offenders: list[str] = []
    for py in sorted(stages.glob("*.py")):
        if py.name in _STAGE_FLAT_ALLOWED:
            continue
        if py.stem not in _STAGE_PACKAGES:
            offenders.append(_stage_relative(py, root))
    for py in sorted(stages.rglob("*.py")):
        if py.parent == stages:
            continue
        if py.relative_to(stages).parts[0] not in _STAGE_PACKAGES:
            offenders.append(_stage_relative(py, root))
    return offenders


def _stage_package_edges(root: Path) -> set[tuple[str, str]]:
    """Return ``(package, sibling package)`` edges of relative imports.

    A module at depth ``d`` below ``stages/`` reaches its own package root with
    a relative level of ``d``. A level of ``d + 1`` climbs out of the package
    and names a sibling package. An edge is counted only when the sibling is a
    declared stage package.
    """
    stages = root / "stages"
    edges: set[tuple[str, str]] = set()
    for py in sorted(stages.rglob("*.py")):
        parts = py.relative_to(stages).parts
        depth = len(parts) - 1
        if depth < 1:
            continue
        owner = parts[0]
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != depth + 1:
                continue
            sibling = (node.module or "").split(".")[0]
            if sibling in _STAGE_PACKAGES:
                edges.add((owner, sibling))
    return edges


def _flat_reexport_identity_offenders(stages: Path) -> list[str]:
    """Return flat re-export modules that do not forward the package object.

    A flat ``stages/<name>.py`` must import every public name from the package
    module, so ``stages.<name>.X is stages.<name>.<name>.X``. A copy, a second
    definition, or a missing name is an offender.
    """
    offenders: list[str] = []
    for name in _STAGE_PACKAGES:
        shim = stages / f"{name}.py"
        if not shim.is_file():
            offenders.append(f"{name}: missing flat re-export")
            continue
        tree = ast.parse(shim.read_text(encoding="utf-8"), filename=str(shim))
        forwarded = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == f"{name}.{name}"
            for alias in node.names
        }
        package_init = (
            ast.parse((stages / name / "__init__.py").read_text(encoding="utf-8"))
            if (stages / name / "__init__.py").is_file()
            else None
        )
        exported: set[str] = set()
        if package_init is not None:
            for node in package_init.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
                ):
                    exported = {
                        elt.value
                        for elt in getattr(node.value, "elts", [])
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    }
        missing = exported - forwarded
        if missing:
            offenders.append(f"{name}: flat re-export misses {sorted(missing)}")
    return offenders


def _stage_modules(root: Path = _PIPELINE) -> list[Path]:
    """Sorted recursive scan of the pipeline stage modules under ``root``.

    One shared pattern owns the glob both stage guards run. Every consumer
    (``_stage_auto_merge_callers``, the sleep guard, the clock-owner pin test)
    calls this helper and never re-globs or duplicates the non-empty assert
    inline. The non-empty assert keeps the guards from passing vacuously if
    the pattern ever stops matching the stage layout.

    The pattern covers the ``stages/`` package and the ``stage_*.py`` modules
    beside it, because stage code lives in both: a stage mixed into
    ``pipeline/`` as ``stage_<name>.py`` is still stage code and must obey the
    same bans.
    """
    modules = sorted(
        [*root.rglob("stages/**/*.py"), *root.glob("stage_*.py")],
        key=str,
    )
    assert modules, f"stage scan matched no files under {root}"
    return modules


def _stage_relative(py: Path, root: Path = _PIPELINE) -> str:
    """Path of a stage module relative to the scan ``root``.

    Nested modules can share a stem, so guard labels and detector members use
    this unambiguous relative path (for example
    ``stages/pr_review_details/synthetic_neg.py``) rather than ``py.stem``.
    A module directly under ``root`` keeps its bare name.
    """
    return py.relative_to(root).as_posix()


def _stage_auto_merge_callers(modules: list[Path], root: Path = _PIPELINE) -> set[str]:
    """Stage modules that call a banned auto-merge mutator (AST walk).

    Returns each offender's path relative to its ``stages/`` parent. The
    banned set holds the live mutation accessor names owned by
    ``pipeline_github_merge_mutations.py``. A stage must not enable or disable
    native auto-merge, and
    ``test_pipeline_stages_have_no_auto_merge_mutation_capability`` enforces
    that rule.
    """
    callers: set[str] = set()
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"enable_auto_merge", "disable_auto_merge"}
            ):
                callers.add(_stage_relative(path, root))
    return callers


def _is_clock_owner(py: Path) -> bool:
    """Return True only for the pinned clock-owner module ``stage_base.py``.

    Exact-path rule: the trailing path components equal ``_CLOCK_OWNER``. A
    module that shares the stem elsewhere is not the owner.
    """
    depth = len(_CLOCK_OWNER)
    return tuple(py.parts[-depth:]) == _CLOCK_OWNER


def _imported_mutators(path: Path) -> set[str]:
    """Find github_api mutators imported or called in a Python file (AST walk)."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()

    for node in ast.walk(tree):
        # Import statements: from github_api import X
        if isinstance(node, ast.ImportFrom) and node.module and "github_api" in node.module:
            found |= {a.name for a in node.names if a.name in _MUTATORS}
        # Attribute access: obj.X where X is a mutator
        if isinstance(node, ast.Attribute) and node.attr in _MUTATORS:
            found.add(node.attr)

    return found


def _forbidden_module_imports(path: Path) -> set[str]:
    """Find any-form imports of forbidden modules (github_api, pr_manager)."""

    def _is_forbidden(dotted: str) -> bool:
        return any(part in _FORBIDDEN_WORKER_MODULES for part in dotted.split("."))

    tree = ast.parse(path.read_text())
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits |= {a.name for a in node.names if _is_forbidden(a.name)}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_forbidden(module):
                hits.add(module)
            else:
                # e.g. `from hephaestus.automation import github_api`
                hits |= {f"{module}.{a.name}" for a in node.names if _is_forbidden(a.name)}
    return hits


def test_pipeline_modules_do_not_import_github_api_mutators_directly() -> None:
    """Ensure pipeline modules (recursively) do not import/call github_api mutators.

    Stage code uses the neutral StageGitHub contract; worker GitHub operations
    live in the repository-scoped production dispatcher outside this package.
    Subprocess-level ``gh`` invocations are out of scope for this AST guard.
    """
    violations = []
    for py in sorted(_PIPELINE.rglob("*.py")):
        if py.stem == "__init__":
            continue
        offenders = _imported_mutators(py) - _ALLOWLIST
        if offenders:
            violations.append(f"{py.name}: {sorted(offenders)}")

    assert not violations, "pipeline code must not call github_api mutators:\n" + "\n".join(
        violations
    )


def test_worker_side_modules_never_import_github_api_or_pr_manager() -> None:
    """Worker-side modules must not import github_api or pr_manager AT ALL.

    The mutator-name scan above cannot enumerate every mutating entry point
    (``skip_epics``, ``gh_call``, ``run``, ...), so for the modules that run on
    worker threads we forbid the whole modules in any import form.
    """
    violations = []
    for name in _WORKER_SIDE_MODULES:
        path = _PIPELINE / name
        assert path.exists(), f"expected worker-side module missing: {path}"
        offenders = _forbidden_module_imports(path)
        if offenders:
            violations.append(f"{name}: {sorted(offenders)}")

    assert not violations, (
        "worker-side modules must not import github_api/pr_manager:\n" + "\n".join(violations)
    )


def test_admission_uses_the_pipeline_dependency_facts_boundary() -> None:
    """Keep dependency admission off the broad GitHub implementation package."""
    path = _PIPELINE / "admission.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                imports.add(module)
            imports.update(f"{module}.{alias.name}" for alias in node.names)

    assert not any(imported == "hephaestus.automation.github_api" for imported in imports)


def test_worker_pool_does_not_import_the_github_implementation() -> None:
    """The generic dispatcher depends only on the typed runner protocol."""
    tree = ast.parse((_PIPELINE / "worker_pool.py").read_text(encoding="utf-8"))
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(
        module.endswith("pipeline_github") or module.endswith("pipeline_github_jobs")
        for module in imported
    )


def test_worker_runner_is_the_only_worker_module_constructing_pipeline_github() -> None:
    """Every closed job gets its accessor at the sole production gateway."""
    runner = _AUTOMATION / "pipeline_github_jobs.py"
    tree = ast.parse(runner.read_text(encoding="utf-8"), filename=str(runner))
    constructors = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PipelineGitHub"
    ]
    assert len(constructors) == 1


def test_mutator_set_is_non_empty() -> None:
    """Guard against github_api.__all__ rename silently emptying _MUTATORS.

    If _MUTATORS becomes empty due to a rename or refactor, the invariant
    passes vacuously. This test ensures the mutator set always has content.
    """
    assert len(_MUTATORS) >= 5, f"_MUTATORS is suspiciously small: {_MUTATORS}"


def test_public_mutators_from_all() -> None:
    """Verify _PUBLIC_MUTATORS is derived from github_api.__all__."""
    # At least the major mutators should be present
    expected = {
        "gh_issue_add_labels",
        "gh_issue_remove_labels",
        "gh_issue_comment",
        "gh_pr_review_post",
        "gh_create_label",
    }
    assert expected <= _PUBLIC_MUTATORS, f"expected mutators missing: {expected - _PUBLIC_MUTATORS}"
    assert "gh_pr_create" not in github_api.__all__


def test_guard_scope_excludes_coordinator_neutral_stage_github_calls(
    tmp_path: Path,
) -> None:
    """Document the guard scope: only direct github_api mutator names are tracked.

    The coordinator-neutral ``ctx.github`` mutator surface is bound by the
    coordinator's ``StageGitHub`` adapter and covered by the mapping tests in
    ``tests/unit/automation/test_pipeline_github.py``. Auto-merge mutations
    are the exception: ``_stage_auto_merge_callers`` covers the live names
    ``enable_auto_merge`` and ``disable_auto_merge``.
    """
    synthetic = (
        "from hephaestus.automation import github_api\n"
        "def f(ctx):\n"
        "    ctx.github.add_labels(1, ['state:x'])\n"
        "    ctx.github.create_pr(1, 'branch', 'title', 'body')\n"
        "    ctx.github.enable_auto_merge(2)\n"
        "    github_api.gh_issue_add_labels(3, ['state:y'])\n"
    )
    path = tmp_path / "synthetic_stage.py"
    path.write_text(synthetic, encoding="utf-8")

    assert _imported_mutators(path) == {"gh_issue_add_labels"}


@pytest.mark.parametrize(
    "module_name",
    [
        "_followup_phase.py",
        "auto_merge_coordinator.py",
        "ci_fix_contract.py",
        "ci_fix_flow.py",
        "ci_fix_orchestrator.py",
        "ci_fix_push_guard.py",
        "ci_fix_sessions.py",
        "ci_run_coordinator.py",
        "drive_green_state.py",
    ],
)
def test_retired_pre_pipeline_coordinators_are_removed(module_name: str) -> None:
    """Retired orchestration must not inflate coverage or revive obsolete behavior."""
    assert not (_AUTOMATION / module_name).exists()


def test_pipeline_stages_have_no_auto_merge_mutation_capability() -> None:
    """The reviewed-head interlock leaves persistent auto-merge to operators."""
    assert _stage_auto_merge_callers(_stage_modules()) == set()


def test_auto_merge_mutations_have_one_production_call_site_each() -> None:
    """Only the closed merge-wait job may enable or disable native auto-merge.

    The approval label is the whole merge contract, so the enablement must stay
    inside one audited call site rather than being spread across stages. A lost
    approval must be disarmed from the closed merge-wait job or the merge
    mutation module, so the ban covers both live mutation names.

    The scan pins the call sites, not their offsets: the assertions use a
    relative file path and a count only. A line above a call site can move
    without a change to the guarded contract.
    """
    enabled: list[str] = []
    disabled: list[str] = []
    for path in sorted(_AUTOMATION.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "enable_auto_merge":
                    enabled.append(str(path.relative_to(_AUTOMATION)))
                elif node.func.attr == "disable_auto_merge":
                    disabled.append(str(path.relative_to(_AUTOMATION)))

    assert enabled == ["pipeline_github_jobs.py"], f"unexpected auto-merge enable sites: {enabled}"
    assert len(disabled) == 2, f"unexpected auto-merge disarm sites: {disabled}"
    assert sorted(disabled) == ["pipeline_github_jobs.py", "pipeline_github_mutations.py"]


def _sleep_violations(tree: ast.AST, filename: str, *, allow_time_import: bool) -> list[str]:
    """AST-collect time.sleep usage/imports (issue #1816, AC1).

    Flags, anywhere in the module (function bodies included):

    - ``time.sleep(...)`` attribute access (any ``<Name time>.sleep``);
    - ``from time import sleep`` (aliased or not);
    - any other ``import time`` / ``from time import ...`` form, unless
      ``allow_time_import``. The exemption belongs to exactly the pinned
      ``_CLOCK_OWNER`` module (stage_base.py legitimately uses ``time.time``
      for its injectable-clock default — it still may never sleep).
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "sleep"
            and isinstance(node.value, ast.Name)
            and node.value.id == "time"
        ):
            violations.append(f"{filename}:{node.lineno}: time.sleep reference")
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            names = {a.name for a in node.names}
            if "sleep" in names:
                violations.append(f"{filename}:{node.lineno}: from time import sleep")
            elif not allow_time_import:
                violations.append(f"{filename}:{node.lineno}: from time import ...")
        elif isinstance(node, ast.Import) and not allow_time_import:
            violations.extend(
                f"{filename}:{node.lineno}: import time"
                for a in node.names
                if a.name == "time" or a.name.startswith("time.")
            )
    return violations


def test_no_time_sleep_in_pipeline_stages() -> None:
    """AC1: zero time.sleep in any pipeline stage module (issue #1816).

    AST-based (matching this file's own guards), so conditional/lazy
    imports and aliased ``from time import sleep`` are caught too — not
    just top-level text matches. The scan is recursive, so stages moved
    into subdirectories stay covered. base.py is NOT exempt from the
    sleep ban: it keeps its ``import time`` only through the pinned
    ``_CLOCK_OWNER`` exemption (the exact-path pin moves with the clock
    owner) and is asserted to contain no ``time.sleep`` in any form.
    """
    violations: list[str] = []
    for py in _stage_modules():
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        violations.extend(
            _sleep_violations(tree, _stage_relative(py), allow_time_import=_is_clock_owner(py))
        )
    assert not violations, "stages must not sleep — timer heap owns waits:\n" + "\n".join(
        violations
    )


def test_sleep_guard_detects_synthetic_violations() -> None:
    """Negative test: the AST sleep guard actually flags every forbidden form.

    Without this, a broken ``_sleep_violations`` returning [] would let the
    guard above pass vacuously.
    """
    synthetic = (
        "import time\n"
        "from time import sleep\n"
        "from time import sleep as zzz\n"
        "def f():\n"
        "    import time\n"
        "    time.sleep(1)\n"
    )
    tree = ast.parse(synthetic, filename="<synthetic>")

    strict = _sleep_violations(tree, "stage.py", allow_time_import=False)
    assert any("time.sleep reference" in v for v in strict)
    assert sum("from time import sleep" in v for v in strict) == 2  # plain + aliased
    assert sum(": import time" in v for v in strict) == 2  # top-level + in-function

    # stage_base.py mode: the bare imports are allowed, sleep never is.
    relaxed = _sleep_violations(tree, "stage_base.py", allow_time_import=True)
    assert any("time.sleep reference" in v for v in relaxed)
    assert not any(": import time" in v for v in relaxed)
    assert sum("from time import sleep" in v for v in relaxed) == 2


def test_auto_merge_guard_detects_nested_violation(tmp_path: Path) -> None:
    """Negative: the recursive stage scan must flag a nested auto-merge caller.

    The benign top-level module keeps the scan non-empty, so the failure of
    this test is a missed detector hit for the nested offender, not the
    vacuity assert.
    """
    stages = tmp_path / "stages"
    nested = stages / "pr_review_details"
    nested.mkdir(parents=True)
    (stages / "clean.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (nested / "synthetic_neg.py").write_text(
        "def g(obj):\n    obj.disable_auto_merge(2)\n", encoding="utf-8"
    )

    assert "stages/pr_review_details/synthetic_neg.py" in _stage_auto_merge_callers(
        _stage_modules(tmp_path), root=tmp_path
    )


def test_sleep_guard_detects_nested_violation(tmp_path: Path) -> None:
    """Negative: the recursive stage scan must flag a nested ``time.sleep``.

    Runs the guard's exact loop over the tmp stage tree; the benign top-level
    module keeps the scan non-empty.
    """
    stages = tmp_path / "stages"
    nested = stages / "pr_review_details"
    nested.mkdir(parents=True)
    (stages / "clean.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (nested / "synth_sleep.py").write_text("import time\ntime.sleep(0)\n", encoding="utf-8")

    violations: list[str] = []
    for py in _stage_modules(tmp_path):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        violations.extend(
            _sleep_violations(
                tree, _stage_relative(py, tmp_path), allow_time_import=_is_clock_owner(py)
            )
        )
    assert any("synth_sleep.py" in v for v in violations)


def test_clock_owner_exemption_is_pinned() -> None:
    """The time-import exemption resolves to exactly ``stage_base.py``.

    (a) The real owner resolves inside the scan. (b) The pin is
    load-bearing: ``import time`` is a violation off-pin, and the owner
    itself is never exempt from the sleep ban. (c) A nested
    ``stages/<sub>/base.py`` shares the stem but is not the owner, so its
    ``import time`` is flagged under the guard's exemption rule.
    """
    # (a) the real clock owner resolves inside the scan
    owners = [py for py in _stage_modules() if _is_clock_owner(py)]
    assert len(owners) == 1, f"clock-owner pin resolved to: {owners}"

    # (b) the pin is load-bearing: the same ``import time`` is a violation
    # off-pin, and the owner's content holds no sleep in any form
    owner = owners[0]
    tree = ast.parse(owner.read_text(encoding="utf-8"), filename=str(owner))
    label = _stage_relative(owner)
    flagged = _sleep_violations(tree, label, allow_time_import=False)
    assert any(": import time" in v for v in flagged)
    allowed = _sleep_violations(tree, label, allow_time_import=True)
    assert not any("sleep" in v for v in allowed)

    # (c) nested-base exactness: a nested ``stages/<sub>/base.py`` is not the
    # owner, so its ``import time`` must be flagged
    nested = _PIPELINE / "stages" / "nested_neg" / "base.py"
    assert not _is_clock_owner(nested)
    nested_tree = ast.parse("import time\n", filename="<nested>")
    nested_flagged = _sleep_violations(
        nested_tree, _stage_relative(nested), allow_time_import=_is_clock_owner(nested)
    )
    assert any(": import time" in v for v in nested_flagged)


def test_stage_layout_is_one_package_per_stage() -> None:
    """AC1: every stage module lives in its own stage package.

    ``stages/`` holds ``__init__.py``, the eight stage packages, and the eight
    flat re-export modules. The base protocol modules stay beside ``stages/``.
    """
    stages = _PIPELINE / "stages"
    flat = sorted(py.name for py in stages.glob("*.py"))
    packages = sorted(p.name for p in stages.iterdir() if p.is_dir() and p.name != "__pycache__")

    assert flat == sorted({*_STAGE_FLAT_ALLOWED, *{f"{name}.py" for name in _STAGE_PACKAGES}}), flat
    assert packages == sorted(_STAGE_PACKAGES), packages
    for name in _STAGE_PACKAGES:
        assert (stages / name / "__init__.py").is_file(), name
        assert (stages / name / f"{name}.py").is_file(), name
    assert _stage_layout_offenders(_PIPELINE) == []
    # The base protocol modules stay flat beside stages/.
    assert (_PIPELINE / "stage_base.py").is_file()
    assert not (stages / "base.py").exists()


def test_stage_layout_guard_detects_a_flat_offender(tmp_path: Path) -> None:
    """AC3: a stage module left at the top of ``stages/`` is an offender."""
    stages = tmp_path / "stages"
    stages.mkdir()
    (stages / "__init__.py").write_text("", encoding="utf-8")
    (stages / "repo.py").write_text("", encoding="utf-8")  # declared re-export
    (stages / "stray.py").write_text("", encoding="utf-8")

    assert _stage_layout_offenders(tmp_path) == ["stages/stray.py"]


def test_stage_layout_guard_detects_a_nested_offender(tmp_path: Path) -> None:
    """AC3: a module in an undeclared subdirectory is an offender."""
    stages = tmp_path / "stages"
    stray = stages / "not_a_stage"
    stray.mkdir(parents=True)
    (stages / "__init__.py").write_text("", encoding="utf-8")
    (stray / "helper.py").write_text("", encoding="utf-8")

    assert _stage_layout_offenders(tmp_path) == ["stages/not_a_stage/helper.py"]


def test_no_stage_package_imports_a_sibling_helper() -> None:
    """AC4: only ``plan_review`` may import the ``planning`` package.

    The set is exact, so a new cross-stage edge fails here.
    """
    assert _stage_package_edges(_PIPELINE) == _ALLOWED_STAGE_PACKAGE_EDGES


def test_stage_package_edge_guard_detects_a_new_edge(tmp_path: Path) -> None:
    """AC4 negative: a sibling-package import from a stage is detected."""
    stages = tmp_path / "stages"
    for name in ("finished", "planning"):
        (stages / name).mkdir(parents=True)
        (stages / name / "__init__.py").write_text("", encoding="utf-8")
        (stages / name / f"{name}.py").write_text("", encoding="utf-8")
    (stages / "__init__.py").write_text("", encoding="utf-8")
    (stages / "finished" / "finished.py").write_text(
        "from ..planning import build_plan_prompt\nfrom . import helper\n",
        encoding="utf-8",
    )
    (stages / "finished" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert _stage_package_edges(tmp_path) == {("finished", "planning")}


def test_flat_reexport_modules_forward_the_package_objects() -> None:
    """AC5: a flat import path and the package path yield the same objects."""
    import importlib

    stages = _PIPELINE / "stages"
    assert _flat_reexport_identity_offenders(stages) == []

    for name in _STAGE_PACKAGES:
        flat = importlib.import_module(f"hephaestus.automation.pipeline.stages.{name}")
        package = importlib.import_module(f"hephaestus.automation.pipeline.stages.{name}.{name}")
        assert flat.__all__, name
        for public in flat.__all__:
            assert getattr(flat, public) is getattr(package, public), (name, public)


def test_flat_reexport_guard_detects_a_missing_name(tmp_path: Path) -> None:
    """AC5 negative: a flat module that omits a package name is an offender."""
    stages = tmp_path / "stages"
    for name in _STAGE_PACKAGES:
        (stages / name).mkdir(parents=True)
        (stages / name / "__init__.py").write_text(
            f'from .{name} import VALUE\n\n__all__ = ["VALUE"]\n', encoding="utf-8"
        )
        (stages / name / f"{name}.py").write_text("VALUE = 1\n", encoding="utf-8")
        (stages / f"{name}.py").write_text(f"from .{name}.{name} import VALUE\n", encoding="utf-8")
    (stages / "repo.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert _flat_reexport_identity_offenders(stages) == ["repo: flat re-export misses ['VALUE']"]
