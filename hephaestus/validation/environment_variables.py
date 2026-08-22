"""Enforce an exact, documented ambient environment-variable policy.

The validator scans only Python source in ``hephaestus/``. Named accesses must
match the ambient-reader manifest at ``docs/environment-variables.toml`` by variable name,
source path, enclosing qualified reader, and access kind. Dynamic names, bulk
reads, and escapes of the ambient mapping always fail closed.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from hephaestus.cli.utils import create_validation_parser, emit_json_status, resolve_repo_root
from hephaestus.config.environment_registry import RETIRED_ENV_NAMES

SCANNED_ROOT = "hephaestus"
EXCLUDED_PATHS = frozenset({"hephaestus/_version.py"})
RETIRED_REGISTRY_PATH = "hephaestus/config/environment_registry.py"
REGISTRY_PATH = "docs/environment-variables.toml"
MARKDOWN_PATH = "docs/environment-variables.md"
MARKDOWN_START = "<!-- BEGIN GENERATED ENVIRONMENT VARIABLE INVENTORY -->"
MARKDOWN_END = "<!-- END GENERATED ENVIRONMENT VARIABLE INVENTORY -->"

_NAMED_ACCESSES = frozenset({"read", "write", "delete", "membership", "read-write"})
_CATEGORIES = frozenset(
    {"operator-config", "platform", "workflow-input", "child-process", "internal"}
)
_SENSITIVITIES = frozenset({"public", "sensitive", "secret"})
_VALIDATIONS = frozenset(
    {"none", "string", "non-empty", "integer", "number", "boolean", "path", "url", "json"}
)
_DIRECTIONS = frozenset({"input", "output", "bidirectional"})
_WILDCARD_CHARS = frozenset("*?[")


@dataclass(frozen=True, order=True)
class EnvironmentAccess:
    """One syntactically observed access to the ambient process environment."""

    path: str
    reader: str
    access: str
    name: str | None
    line: int


@dataclass(frozen=True, order=True)
class RegistryReader:
    """One exact reader identity approved by the registry."""

    path: str
    reader: str
    access: str


@dataclass(frozen=True)
class RegistryVariable:
    """A documented variable and its exact approved readers."""

    name: str
    category: str
    owner: str
    purpose: str
    sensitivity: str
    validation: str
    direction: str
    readers: tuple[RegistryReader, ...]


@dataclass(frozen=True, order=True)
class PolicyFinding:
    """A stable policy diagnostic."""

    code: str
    message: str
    path: str = ""
    line: int = 0


class RegistryError(ValueError):
    """Raised when the ambient-reader manifest violates its schema."""


def _literal_strings(node: ast.AST, bindings: dict[str, frozenset[str]]) -> frozenset[str]:
    """Resolve a statically enumerable string expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return frozenset({node.value})
    if isinstance(node, ast.Name):
        return bindings.get(node.id, frozenset())
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values: set[str] = set()
        for item in node.elts:
            resolved = _literal_strings(item, bindings)
            if not resolved:
                return frozenset()
            values.update(resolved)
        return frozenset(values)
    if isinstance(node, ast.Dict):
        values = set()
        for key in node.keys:
            if key is None:
                return frozenset()
            resolved = _literal_strings(key, bindings)
            if not resolved:
                return frozenset()
            values.update(resolved)
        return frozenset(values)
    return frozenset()


class _EnvironmentScanner(ast.NodeVisitor):
    """Collect environment accesses while retaining lexical reader identity."""

    def __init__(self, tree: ast.AST, path: str) -> None:
        self.path = path
        self.accesses: list[EnvironmentAccess] = []
        self.os_aliases: set[str] = set()
        self.environ_aliases: set[str] = set()
        self.getenv_aliases: set[str] = set()
        self.putenv_aliases: set[str] = set()
        self.unsetenv_aliases: set[str] = set()
        self.bindings: dict[str, frozenset[str]] = {}
        self.qualnames: list[str] = []
        self.consumed_environment_nodes: set[int] = set()
        self.parents: dict[ast.AST, ast.AST] = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        self._discover_aliases_and_constants(tree)

    @property
    def reader(self) -> str:
        """Return the stable enclosing qualified reader name."""
        return ".".join(self.qualnames) if self.qualnames else "<module>"

    def _discover_aliases_and_constants(self, tree: ast.AST) -> None:
        self._discover_import_aliases(tree)
        self._discover_bindings_and_environment_aliases(tree)

    def _discover_import_aliases(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "os":
                        self.os_aliases.add(alias.asname or "os")
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    if alias.name == "environ":
                        self.environ_aliases.add(alias.asname or alias.name)
                    elif alias.name == "getenv":
                        self.getenv_aliases.add(alias.asname or alias.name)
                    elif alias.name == "putenv":
                        self.putenv_aliases.add(alias.asname or alias.name)
                    elif alias.name == "unsetenv":
                        self.unsetenv_aliases.add(alias.asname or alias.name)

    def _discover_bindings_and_environment_aliases(self, tree: ast.AST) -> None:
        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if not isinstance(target, ast.Name):
                        continue
                    resolved = _literal_strings(value, self.bindings)
                    combined = self.bindings.get(target.id, frozenset()) | resolved
                    if combined and self.bindings.get(target.id) != combined:
                        self.bindings[target.id] = combined
                        changed = True
                    if self._is_environment(value) and target.id not in self.environ_aliases:
                        self.environ_aliases.add(target.id)
                        changed = True

    def _is_os_attribute(self, node: ast.AST, attribute: str) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == attribute
            and isinstance(node.value, ast.Name)
            and node.value.id in self.os_aliases
        )

    def _is_environment(self, node: ast.AST) -> bool:
        return self._is_os_attribute(node, "environ") or (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in self.environ_aliases
        )

    def _consume_environment(self, node: ast.AST) -> None:
        if self._is_environment(node):
            self.consumed_environment_nodes.add(id(node))

    def _record(self, node: ast.AST, access: str, name: str | None) -> None:
        self.accesses.append(
            EnvironmentAccess(self.path, self.reader, access, name, getattr(node, "lineno", 0))
        )

    def _record_names(self, node: ast.AST, expression: ast.AST, access: str) -> None:
        names = _literal_strings(expression, self.bindings)
        if names:
            for name in sorted(names):
                self._record(node, access, name)
        else:
            self._record(node, "dynamic", None)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        shadowed = {argument.arg: self.bindings.pop(argument.arg, None) for argument in arguments}
        self.qualnames.append(node.name)
        self.generic_visit(node)
        self.qualnames.pop()
        for name, previous in shadowed.items():
            if previous is not None:
                self.bindings[name] = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.qualnames.append(node.name)
        self.generic_visit(node)
        self.qualnames.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._is_environment(node.value) and all(
            isinstance(target, ast.Name) for target in node.targets
        ):
            self._consume_environment(node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if (
            node.value is not None
            and self._is_environment(node.value)
            and isinstance(node.target, ast.Name)
        ):
            self._consume_environment(node.value)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._is_environment(node.value):
            self._consume_environment(node.value)
            access = "read"
            if isinstance(node.ctx, ast.Store):
                access = "write"
            elif isinstance(node.ctx, ast.Del):
                access = "delete"
            self._record_names(node, node.slice, access)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        left = node.left
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            if isinstance(operator, (ast.In, ast.NotIn)) and self._is_environment(comparator):
                self._consume_environment(comparator)
                self._record_names(node, left, "membership")
            left = comparator
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        if self._is_os_attribute(function, "getenv") or (
            isinstance(function, ast.Name) and function.id in self.getenv_aliases
        ):
            if node.args:
                self._record_names(node, node.args[0], "read")
            else:
                self._record(node, "dynamic", None)
        elif isinstance(function, ast.Attribute) and self._is_environment(function.value):
            self._consume_environment(function.value)
            if function.attr == "get":
                self._record_names(node, node.args[0], "read") if node.args else self._record(
                    node, "dynamic", None
                )
            elif function.attr in {"setdefault", "pop"}:
                self._record_names(node, node.args[0], "read-write") if node.args else self._record(
                    node, "dynamic", None
                )
            elif function.attr in {"__getitem__"}:
                self._record_names(node, node.args[0], "read") if node.args else self._record(
                    node, "dynamic", None
                )
            else:
                self._record(node, "bulk", None)
        elif self._is_os_attribute(function, "putenv") or (
            isinstance(function, ast.Name) and function.id in self.putenv_aliases
        ):
            self._record_names(node, node.args[0], "write") if node.args else self._record(
                node, "dynamic", None
            )
        elif self._is_os_attribute(function, "unsetenv") or (
            isinstance(function, ast.Name) and function.id in self.unsetenv_aliases
        ):
            self._record_names(node, node.args[0], "delete") if node.args else self._record(
                node, "dynamic", None
            )
        self.generic_visit(node)

    def _iterated_strings(self, node: ast.AST) -> frozenset[str]:
        values = _literal_strings(node, self.bindings)
        if values:
            return values
        if (
            isinstance(node, ast.Call)
            and not node.args
            and not node.keywords
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"items", "keys"}
        ):
            return _literal_strings(node.func.value, self.bindings)
        return frozenset()

    def _loop_name(self, target: ast.AST) -> str | None:
        if isinstance(target, ast.Name):
            return target.id
        if (
            isinstance(target, (ast.Tuple, ast.List))
            and target.elts
            and isinstance(target.elts[0], ast.Name)
        ):
            return target.elts[0].id
        return None

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        previous: frozenset[str] | None = None
        loop_name = self._loop_name(node.target)
        if loop_name is not None:
            previous = self.bindings.get(loop_name)
            values = self._iterated_strings(node.iter)
            if values:
                self.bindings[loop_name] = values
        for statement in (*node.body, *node.orelse):
            self.visit(statement)
        if loop_name is not None:
            if previous is None:
                self.bindings.pop(loop_name, None)
            else:
                self.bindings[loop_name] = previous

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit(node.iter)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def _visit_comprehension_expression(
        self,
        generators: list[ast.comprehension],
        expressions: tuple[ast.AST, ...],
        index: int = 0,
    ) -> None:
        if index == len(generators):
            for expression in expressions:
                self.visit(expression)
            return
        generator = generators[index]
        self.visit(generator.iter)
        previous: frozenset[str] | None = None
        if isinstance(generator.target, ast.Name):
            previous = self.bindings.get(generator.target.id)
            values = _literal_strings(generator.iter, self.bindings)
            if values:
                self.bindings[generator.target.id] = values
        for condition in generator.ifs:
            self.visit(condition)
        self._visit_comprehension_expression(generators, expressions, index + 1)
        if isinstance(generator.target, ast.Name):
            if previous is None:
                self.bindings.pop(generator.target.id, None)
            else:
                self.bindings[generator.target.id] = previous

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension_expression(node.generators, (node.key, node.value))

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension_expression(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension_expression(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension_expression(node.generators, (node.elt,))

    def finish(self, tree: ast.AST) -> list[EnvironmentAccess]:
        """Visit the tree and classify any unconsumed mapping reference as bulk."""
        self.visit(tree)
        for node in ast.walk(tree):
            if self._is_environment(node) and id(node) not in self.consumed_environment_nodes:
                parent = self.parents.get(node)
                if isinstance(parent, ast.Attribute) and parent.value is node:
                    continue
                self._record(node, "bulk", None)
        return sorted(set(self.accesses))


def scan_source(source: str, path: str) -> list[EnvironmentAccess]:
    """Return all ambient environment accesses in one Python source string.

    Raises:
        SyntaxError: If *source* is not valid Python.

    """
    tree = ast.parse(source, filename=path)
    return _EnvironmentScanner(tree, path).finish(tree)


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{key} must be a non-empty string")
    return value


def _validate_reader(raw: Any) -> RegistryReader:
    if not isinstance(raw, dict):
        raise RegistryError("each reader must be a table")
    path = _required_text(raw, "path")
    reader = _required_text(raw, "reader")
    access = _required_text(raw, "access")
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or not pure.parts
        or pure.parts[0] != SCANNED_ROOT
        or pure.suffix != ".py"
        or any(character in path for character in _WILDCARD_CHARS)
        or any(character in reader for character in _WILDCARD_CHARS)
    ):
        raise RegistryError(f"reader path/name must be exact governed identities: {path}:{reader}")
    if access not in _NAMED_ACCESSES:
        raise RegistryError(f"unsupported reader access: {access}")
    if set(raw) != {"path", "reader", "access"}:
        raise RegistryError("reader fields must be exactly path, reader, and access")
    return RegistryReader(path, reader, access)


def _parse_registry_variable(raw: Any, names: set[str]) -> RegistryVariable:
    expected_fields = {
        "name",
        "category",
        "owner",
        "purpose",
        "sensitivity",
        "validation",
        "direction",
        "readers",
    }
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise RegistryError(f"variable fields must be exactly {sorted(expected_fields)}")
    name = _required_text(raw, "name")
    if name in names or any(character in name for character in _WILDCARD_CHARS):
        raise RegistryError(f"duplicate or wildcard variable name: {name}")
    names.add(name)
    category = _required_text(raw, "category")
    sensitivity = _required_text(raw, "sensitivity")
    validation = _required_text(raw, "validation")
    direction = _required_text(raw, "direction")
    enum_values = (
        (category, _CATEGORIES, "category"),
        (sensitivity, _SENSITIVITIES, "sensitivity"),
        (validation, _VALIDATIONS, "validation"),
        (direction, _DIRECTIONS, "direction"),
    )
    for value, choices, field in enum_values:
        if value not in choices:
            raise RegistryError(f"unsupported {field} for {name}: {value}")
    raw_readers = raw["readers"]
    if not isinstance(raw_readers, list) or not raw_readers:
        raise RegistryError(f"{name} must declare at least one exact reader")
    readers = tuple(_validate_reader(item) for item in raw_readers)
    if len(set(readers)) != len(readers):
        raise RegistryError(f"{name} has duplicate readers")
    return RegistryVariable(
        name,
        category,
        _required_text(raw, "owner"),
        _required_text(raw, "purpose"),
        sensitivity,
        validation,
        direction,
        readers,
    )


def load_registry(path: Path) -> tuple[RegistryVariable, ...]:
    """Load and validate the exact ambient-reader manifest."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(f"could not read registry: {exc}") from exc
    if data.get("schema_version") != 1 or set(data) - {"schema_version", "variables"}:
        raise RegistryError("registry requires schema_version = 1 and only variables entries")
    raw_variables = data.get("variables", [])
    if not isinstance(raw_variables, list):
        raise RegistryError("variables must be an array of tables")
    names: set[str] = set()
    return tuple(_parse_registry_variable(raw, names) for raw in raw_variables)


def _markdown_cell(value: str) -> str:
    """Escape one deterministic Markdown table cell."""
    return value.replace("|", "\\|").replace("\n", " ")


def render_markdown_inventory(registry: tuple[RegistryVariable, ...]) -> str:
    """Render the exact marked human-readable table derived from the registry."""
    lines = [
        MARKDOWN_START,
        "| Variable | Category | Owner | Purpose | Sensitivity | Validation | "
        "Direction | Readers |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for variable in sorted(registry, key=lambda item: item.name):
        readers = "<br>".join(
            f"`{reader.path}:{reader.reader}:{reader.access}`"
            for reader in sorted(variable.readers)
        )
        cells = (
            f"`{variable.name}`",
            variable.category,
            variable.owner,
            variable.purpose,
            variable.sensitivity,
            variable.validation,
            variable.direction,
            readers,
        )
        lines.append("| " + " | ".join(_markdown_cell(cell) for cell in cells) + " |")
    lines.append(MARKDOWN_END)
    return "\n".join(lines) + "\n"


def _documentation_findings(
    repo_root: Path, registry: tuple[RegistryVariable, ...]
) -> list[PolicyFinding]:
    expected = render_markdown_inventory(registry)
    path = repo_root / MARKDOWN_PATH
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [
            PolicyFinding(
                "documentation-drift", f"could not read generated inventory: {exc}", MARKDOWN_PATH
            )
        ]
    if content.count(MARKDOWN_START) != 1 or content.count(MARKDOWN_END) != 1:
        return [
            PolicyFinding(
                "documentation-drift",
                "generated inventory markers must each appear exactly once",
                MARKDOWN_PATH,
            )
        ]
    start = content.index(MARKDOWN_START)
    end = content.index(MARKDOWN_END, start) + len(MARKDOWN_END)
    actual = content[start:end] + "\n"
    if actual != expected:
        return [
            PolicyFinding(
                "documentation-drift",
                "generated environment-variable table does not match the TOML registry",
                MARKDOWN_PATH,
            )
        ]
    return []


def _collect_repository_accesses(
    repo_root: Path,
) -> tuple[list[EnvironmentAccess], list[PolicyFinding]]:
    accesses: list[EnvironmentAccess] = []
    findings: list[PolicyFinding] = []
    source_root = repo_root / SCANNED_ROOT
    if not source_root.is_dir():
        return accesses, [
            PolicyFinding("missing-source-root", f"missing governed root: {SCANNED_ROOT}")
        ]
    for source_path in sorted(source_root.rglob("*.py")):
        relative = source_path.relative_to(repo_root).as_posix()
        if relative in EXCLUDED_PATHS:
            continue
        try:
            source = source_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
            accesses.extend(_EnvironmentScanner(tree, relative).finish(tree))
            if relative != RETIRED_REGISTRY_PATH:
                findings.extend(
                    PolicyFinding(
                        "retired-reference",
                        f"retired environment variable {node.value} remains in runtime source",
                        relative,
                        node.lineno,
                    )
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value in RETIRED_ENV_NAMES
                )
        except (OSError, UnicodeError, SyntaxError) as exc:
            line = exc.lineno if isinstance(exc, SyntaxError) and exc.lineno else 0
            findings.append(PolicyFinding("parse-error", str(exc), relative, line))
    return accesses, findings


def _registry_drift_findings(
    accesses: list[EnvironmentAccess], registry: tuple[RegistryVariable, ...]
) -> list[PolicyFinding]:
    named = [item for item in accesses if item.name is not None and item.access in _NAMED_ACCESSES]
    findings: list[PolicyFinding] = []
    documented: dict[str, set[RegistryReader]] = {
        variable.name: set(variable.readers) for variable in registry
    }
    actual_names = {item.name for item in named}
    actual_identities = {
        (item.name, RegistryReader(item.path, item.reader, item.access)) for item in named
    }
    for item in named:
        identity = RegistryReader(item.path, item.reader, item.access)
        if item.name not in documented:
            findings.append(
                PolicyFinding(
                    "unlisted-access",
                    f"{item.name} is not documented in {REGISTRY_PATH}",
                    item.path,
                    item.line,
                )
            )
        elif identity not in documented[item.name]:
            findings.append(
                PolicyFinding(
                    "reader-mismatch",
                    f"{item.name} has undocumented reader {item.path}:{item.reader}:{item.access}",
                    item.path,
                    item.line,
                )
            )
    for variable in registry:
        for reader in variable.readers:
            if variable.name not in actual_names:
                code = "stale-reader"
                message = f"{variable.name} documents a reader no longer present"
            elif (variable.name, reader) not in actual_identities:
                code = "reader-mismatch"
                message = (
                    f"{variable.name} documents non-matching reader "
                    f"{reader.path}:{reader.reader}:{reader.access}"
                )
            else:
                continue
            findings.append(PolicyFinding(code, message, reader.path))
    return findings


def validate_repository(repo_root: Path) -> list[PolicyFinding]:
    """Validate governed Python source against the exact registry."""
    try:
        registry = load_registry(repo_root / REGISTRY_PATH)
    except RegistryError as exc:
        return [PolicyFinding("invalid-registry", str(exc), REGISTRY_PATH)]

    accesses, findings = _collect_repository_accesses(repo_root)
    findings.extend(_documentation_findings(repo_root, registry))
    for item in accesses:
        if item.access in {"dynamic", "bulk"}:
            findings.append(
                PolicyFinding(
                    f"{item.access}-access",
                    f"{item.reader} uses forbidden {item.access} ambient environment access",
                    item.path,
                    item.line,
                )
            )

    findings.extend(_registry_drift_findings(accesses, registry))
    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    """Run the environment-variable policy validation CLI."""
    parser = create_validation_parser(__doc__, prog="hephaestus-check-environment-variables")
    args = parser.parse_args(argv)
    findings = validate_repository(resolve_repo_root(args))
    exit_code = 1 if findings else 0
    if args.json:
        emit_json_status(
            exit_code,
            "environment-variable policy failed" if findings else None,
            findings=[asdict(finding) for finding in findings],
        )
    elif findings:
        print(f"FAIL: {len(findings)} environment-variable policy finding(s):")
        for finding in findings:
            location = finding.path + (f":{finding.line}" if finding.line else "")
            print(f"  [{finding.code}] {location}: {finding.message}")
    else:
        print("OK: ambient environment-variable access exactly matches the registry.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
