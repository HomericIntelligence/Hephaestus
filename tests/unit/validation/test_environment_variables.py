"""Tests for the ambient environment-variable policy validator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.validation import environment_variables as env_policy


def _write_registry(root: Path, entries: str = "") -> None:
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    registry_path = docs / "environment-variables.toml"
    registry_path.write_text(
        "schema_version = 1\n" + entries,
        encoding="utf-8",
    )
    try:
        registry = env_policy.load_registry(registry_path)
    except env_policy.RegistryError:
        registry = ()
    (docs / "environment-variables.md").write_text(
        "# Environment variables\n\n" + env_policy.render_markdown_inventory(registry),
        encoding="utf-8",
    )


def _entry(
    name: str,
    *,
    path: str = "hephaestus/example.py",
    reader: str = "read_value",
    access: str = "read",
) -> str:
    return f'''\n[[variables]]
name = "{name}"
category = "operator-config"
owner = "maintainers"
purpose = "Controls the example behavior."
sensitivity = "public"
validation = "non-empty"
direction = "input"
readers = [{{ path = "{path}", reader = "{reader}", access = "{access}" }}]
'''


def _scan(source: str) -> list[env_policy.EnvironmentAccess]:
    return env_policy.scan_source(source, "hephaestus/example.py")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('import os\ndef read_value(): return os.getenv("ONE")\n', {("ONE", "read")}),
        (
            'import os as operating\ndef read_value(): return operating.environ.get("TWO")\n',
            {("TWO", "read")},
        ),
        (
            'from os import getenv as read_env\ndef read_value(): return read_env("THREE")\n',
            {("THREE", "read")},
        ),
        (
            'from os import environ as ambient\ndef read_value(): return ambient["FOUR"]\n',
            {("FOUR", "read")},
        ),
        (
            'import os\nNAME = "FIVE"\ndef read_value(): return os.environ.get(NAME)\n',
            {("FIVE", "read")},
        ),
    ],
)
def test_scan_source_detects_named_read_syntaxes(
    source: str, expected: set[tuple[str, str]]
) -> None:
    """Canonical, aliased, and constant-indirected reads are equivalent."""
    accesses = _scan(source)
    assert {(item.name, item.access) for item in accesses} == expected
    assert {item.reader for item in accesses} == {"read_value"}


@pytest.mark.parametrize(
    ("statement", "access"),
    [
        ('value = os.environ["X"]', "read"),
        ('os.environ["X"] = "yes"', "write"),
        ('del os.environ["X"]', "delete"),
        ('present = "X" in os.environ', "membership"),
        ('value = os.environ.setdefault("X", "yes")', "read-write"),
        ('value = os.environ.pop("X", None)', "read-write"),
        ('os.putenv("X", "yes")', "write"),
        ('os.unsetenv("X")', "delete"),
    ],
)
def test_scan_source_classifies_access_direction(statement: str, access: str) -> None:
    """Reads and process-environment mutations have distinct policy identities."""
    result = _scan(f"import os\ndef read_value():\n    {statement}\n")
    assert [(item.name, item.access) for item in result] == [("X", access)]


def test_scan_source_detects_aliased_mutation_imports() -> None:
    """Imported putenv/unsetenv aliases cannot bypass mutation enforcement."""
    source = """from os import putenv as set_env, unsetenv as delete_env
def read_value():
    set_env("ONE", "yes")
    delete_env("TWO")
"""
    assert {(item.name, item.access) for item in _scan(source)} == {
        ("ONE", "write"),
        ("TWO", "delete"),
    }


def test_scan_source_tracks_simple_environment_alias() -> None:
    """Assigning ``os.environ`` to another name cannot evade the scanner."""
    result = _scan(
        'import os\ndef read_value():\n    ambient = os.environ\n    return ambient.get("X")\n'
    )
    assert [(item.name, item.access) for item in result] == [("X", "read")]


@pytest.mark.parametrize(
    "expression",
    [
        "os.environ.copy()",
        "dict(os.environ)",
        "list(os.environ.items())",
        "consume(os.environ)",
        "{**os.environ}",
        "[key for key in os.environ]",
    ],
)
def test_scan_source_rejects_bulk_or_escaped_mapping(expression: str) -> None:
    """An inherited mapping cannot be represented by a complete named registry."""
    source = f"import os\ndef read_value():\n    return {expression}\n"
    assert any(item.access == "bulk" and item.name is None for item in _scan(source))


def test_scan_source_rejects_dynamic_name() -> None:
    """A runtime-computed name cannot satisfy an exact-name allowlist."""
    result = _scan("import os\ndef read_value(name): return os.environ.get(name)\n")
    assert [(item.name, item.access) for item in result] == [(None, "dynamic")]


def test_function_parameter_shadows_static_name_binding() -> None:
    """A parameter-derived name stays dynamic even when a static name exists elsewhere."""
    source = """import os
NAME = "STATIC"
def read_value(NAME):
    return os.getenv(NAME)
"""
    assert [(item.name, item.access) for item in _scan(source)] == [(None, "dynamic")]


def test_scan_source_expands_literal_loop_names() -> None:
    """Literal collections remain statically inventoryable."""
    source = """import os
def read_value():
    return {name: os.environ.get(name) for name in ("ONE", "TWO")}
"""
    assert {(item.name, item.access) for item in _scan(source)} == {
        ("ONE", "read"),
        ("TWO", "read"),
    }


def test_scan_source_expands_literal_dict_items_loop_names() -> None:
    """Tuple-unpacked keys from a static mapping remain exactly inventoryable."""
    source = """import os
def read_value():
    mapping = {"ONE": "first", "TWO": "second"}
    for name, field in mapping.items():
        os.environ.get(name)
"""
    assert {(item.name, item.access) for item in _scan(source)} == {
        ("ONE", "read"),
        ("TWO", "read"),
    }


def test_scan_source_converges_when_a_constant_name_is_reassigned() -> None:
    """Discovery unions static values instead of oscillating forever."""
    source = """import os
NAME = "ONE"
first = os.getenv(NAME)
NAME = "TWO"
second = os.getenv(NAME)
"""
    assert {(item.name, item.access) for item in _scan(source)} == {
        ("ONE", "read"),
        ("TWO", "read"),
    }


def test_scan_source_ignores_nonambient_environment_shapes() -> None:
    """Strings, comments, local mappings, and subprocess env arguments are not ambient reads."""
    source = """
def read_value():
    # os.environ["COMMENT"]
    text = 'os.getenv("STRING")'
    env = {"CHILD": "literal"}
    run(command, env=env)
    return text
"""
    assert _scan(source) == []


def test_validate_repository_accepts_exact_documented_reader(tmp_path: Path) -> None:
    """A fully documented exact reader is allowed."""
    package = tmp_path / "hephaestus"
    package.mkdir()
    (package / "example.py").write_text(
        'import os\ndef read_value(): return os.environ.get("APP_MODE")\n',
        encoding="utf-8",
    )
    _write_registry(tmp_path, _entry("APP_MODE"))
    assert env_policy.validate_repository(tmp_path) == []


def test_registry_accepts_numeric_validation_contract(tmp_path: Path) -> None:
    """Float-valued runtime inputs can document their exact numeric validation."""
    docs = tmp_path / "docs"
    docs.mkdir()
    registry_path = docs / "environment-variables.toml"
    registry_path.write_text(
        "schema_version = 1\n" + _entry("NATS_BACKOFF").replace('"non-empty"', '"number"')
    )
    assert env_policy.load_registry(registry_path)[0].validation == "number"


@pytest.mark.parametrize(
    ("registry", "expected_code"),
    [
        ("", "unlisted-access"),
        (_entry("STALE"), "stale-reader"),
        (_entry("APP_MODE", reader="other"), "reader-mismatch"),
    ],
)
def test_validate_repository_enforces_exact_registry(
    tmp_path: Path, registry: str, expected_code: str
) -> None:
    """Names and reader identities must match in both directions."""
    package = tmp_path / "hephaestus"
    package.mkdir()
    (package / "example.py").write_text(
        'import os\ndef read_value(): return os.environ.get("APP_MODE")\n', encoding="utf-8"
    )
    _write_registry(tmp_path, registry)
    assert expected_code in {finding.code for finding in env_policy.validate_repository(tmp_path)}


@pytest.mark.parametrize(
    "entries",
    [
        _entry("DUP") + _entry("DUP"),
        _entry("X", path="../escape.py"),
        _entry("X", path="hephaestus/*.py"),
        """
[[variables]]
name = "X"
category = "operator-config"
owner = ""
purpose = ""
sensitivity = "public"
validation = "none"
direction = "input"
readers = []
""",
    ],
)
def test_validate_repository_fails_closed_on_invalid_registry(tmp_path: Path, entries: str) -> None:
    """Malformed, duplicate, wildcard, escaped, or rationale-free entries are invalid."""
    (tmp_path / "hephaestus").mkdir()
    _write_registry(tmp_path, entries)
    findings = env_policy.validate_repository(tmp_path)
    assert any(finding.code == "invalid-registry" for finding in findings)


def test_validate_repository_fails_closed_on_malformed_toml(tmp_path: Path) -> None:
    """Unreadable policy data cannot disable enforcement."""
    (tmp_path / "hephaestus").mkdir()
    _write_registry(tmp_path, "[[variables]\n")
    assert {item.code for item in env_policy.validate_repository(tmp_path)} == {"invalid-registry"}


def test_validate_repository_fails_closed_on_malformed_python(tmp_path: Path) -> None:
    """A syntax error in governed source cannot silently evade analysis."""
    package = tmp_path / "hephaestus"
    package.mkdir()
    (package / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    _write_registry(tmp_path)
    assert {item.code for item in env_policy.validate_repository(tmp_path)} == {"parse-error"}


def test_validate_repository_rejects_retired_runtime_references(tmp_path: Path) -> None:
    """A retired name cannot return as a child-write or protocol constant."""
    package = tmp_path / "hephaestus"
    package.mkdir()
    (package / "example.py").write_text(
        'CHILD_ENV = {"HEPH_GH_TIMEOUT": "120"}\n',
        encoding="utf-8",
    )
    _write_registry(tmp_path)

    findings = env_policy.validate_repository(tmp_path)

    assert [(item.code, item.path, item.line) for item in findings] == [
        ("retired-reference", "hephaestus/example.py", 1)
    ]


def test_validate_repository_detects_generated_markdown_drift(tmp_path: Path) -> None:
    """The human inventory is executable documentation derived from TOML."""
    (tmp_path / "hephaestus").mkdir()
    _write_registry(tmp_path)
    (tmp_path / "docs" / "environment-variables.md").write_text(
        "# Environment variables\n\n<!-- stale generated inventory -->\n",
        encoding="utf-8",
    )
    assert {item.code for item in env_policy.validate_repository(tmp_path)} == {
        "documentation-drift"
    }


def test_render_markdown_inventory_is_deterministic(tmp_path: Path) -> None:
    """The coordinator can insert the exact marked table rendered by the policy API."""
    assert hasattr(env_policy, "render_markdown_inventory")
    docs = tmp_path / "docs"
    docs.mkdir()
    registry_path = docs / "environment-variables.toml"
    registry_path.write_text("schema_version = 1\n" + _entry("APP_MODE"))
    registry = env_policy.load_registry(registry_path)
    rendered = env_policy.render_markdown_inventory(registry)
    assert rendered.startswith(env_policy.MARKDOWN_START + "\n")
    assert rendered.endswith(env_policy.MARKDOWN_END + "\n")
    assert "Controls the example behavior." in rendered
    assert "`hephaestus/example.py:read_value:read`" in rendered


def test_validate_repository_excludes_generated_version_file(tmp_path: Path) -> None:
    """The hatch-vcs generated module is outside the source policy."""
    package = tmp_path / "hephaestus"
    package.mkdir()
    (package / "_version.py").write_text('import os\nx = os.getenv("IGNORED")\n')
    _write_registry(tmp_path)
    assert env_policy.validate_repository(tmp_path) == []


def test_main_has_stable_text_and_json_exit_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI reports violations in human and machine-readable forms."""
    package = tmp_path / "hephaestus"
    package.mkdir()
    (package / "example.py").write_text('import os\nx = os.getenv("UNLISTED")\n')
    _write_registry(tmp_path)

    assert env_policy.main(["--repo-root", str(tmp_path)]) == 1
    assert "unlisted-access" in capsys.readouterr().out
    assert env_policy.main(["--repo-root", str(tmp_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 1
    assert payload["findings"][0]["code"] == "unlisted-access"


def test_scope_is_exactly_hephaestus_python() -> None:
    """The initial executable boundary cannot widen silently."""
    assert env_policy.SCANNED_ROOT == "hephaestus"
    assert frozenset({"hephaestus/_version.py"}) == env_policy.EXCLUDED_PATHS
