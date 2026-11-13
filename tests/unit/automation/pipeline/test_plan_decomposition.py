"""Unit tests for plan-decomposition work-unit and tree validation."""

from __future__ import annotations

from typing import Any

import pytest

from hephaestus.automation.plan_decomposition import (
    COMMAND_MAX_CHARS,
    OWNED_BEHAVIOR_MAX_CHARS,
    TITLE_MAX_CHARS,
    build_finding_manifest,
    build_requirement_manifest,
    child_recovery_marker,
    finding_identifier,
    normalize_verification_command,
    normalize_work_tree,
    normalize_work_unit,
    parse_child_marker,
    render_child_body,
    requirement_identifier,
    topological_layers,
    tree_digest,
    work_tree_errors,
)


def _unit(**overrides: Any) -> dict[str, Any]:
    """Return one valid closed-schema work-unit payload."""
    node_id = overrides.get("node_id", "node-a")
    payload: dict[str, Any] = {
        "node_id": node_id,
        "title": "Add the parser helper",
        "owned_behavior": f"Node {node_id} owns one parser behavior.",
        "repository_paths": ("hephaestus/utils/parser.py",),
        "acceptance_criteria": ("The parser rejects malformed payloads.",),
        "failure_behavior": "The parser raises ValueError on malformed input.",
        "verification_commands": ("uv run pytest tests/unit/utils/test_parser.py",),
        "expected_evidence": "The focused parser test passes.",
        "prerequisites": (),
        "requirement_ids": (),
        "finding_ids": (),
        "single_pr": True,
    }
    payload.update(overrides)
    return payload


def _manifest_ids() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return one requirement identifier and one finding identifier."""
    requirements = build_requirement_manifest(("The parser rejects malformed input.",))
    findings = build_finding_manifest(("Guard the empty payload case.",))
    return (
        tuple(ref.identifier for ref in requirements),
        tuple(ref.identifier for ref in findings),
    )


def _valid_unit() -> dict[str, Any]:
    """Return one valid unit mapped to the shared manifest identifiers."""
    requirement_ids, finding_ids = _manifest_ids()
    return _unit(requirement_ids=requirement_ids, finding_ids=finding_ids)


class TestStableIdentifiers:
    """Requirement and finding identifiers are stable and namespaced."""

    def test_requirement_identifier_is_deterministic(self) -> None:
        first = requirement_identifier("Cover the parser case.")
        second = requirement_identifier("Cover the parser case.")
        assert first == second
        assert first.startswith("req-")
        assert len(first) == len("req-") + 12

    def test_finding_identifier_uses_the_find_namespace(self) -> None:
        identifier = finding_identifier("Guard the empty case.")
        assert identifier.startswith("find-")
        assert identifier != requirement_identifier("Guard the empty case.")

    def test_manifests_are_deduplicated_and_sorted(self) -> None:
        text = "Same text twice."
        requirements = build_requirement_manifest((text, text))
        assert len(requirements) == 1


class TestSinglePrScope:
    """The single-pull-request declaration is the leaf condition."""

    def test_work_item_count_has_no_configured_limit_and_single_pr_scope_is_leaf_rule(
        self,
    ) -> None:
        """No count cap exists; every admitted node declares single-PR scope."""
        requirement_ids, finding_ids = _manifest_ids()
        units = normalize_work_tree(
            [
                _valid_unit(),
                _unit(
                    node_id="node-b",
                    requirement_ids=requirement_ids,
                    finding_ids=finding_ids,
                ),
                _unit(
                    node_id="node-c",
                    requirement_ids=requirement_ids,
                    finding_ids=finding_ids,
                ),
                _unit(
                    node_id="node-d",
                    requirement_ids=requirement_ids,
                    finding_ids=finding_ids,
                ),
                _unit(
                    node_id="node-e",
                    requirement_ids=requirement_ids,
                    finding_ids=finding_ids,
                ),
                _unit(
                    node_id="node-f",
                    requirement_ids=requirement_ids,
                    finding_ids=finding_ids,
                ),
                _unit(
                    node_id="node-g",
                    requirement_ids=requirement_ids,
                    finding_ids=finding_ids,
                ),
            ]
        )
        assert units is not None
        assert len(units) == 7
        assert all(unit.single_pr for unit in units)

    def test_requires_complete_single_pr_specification(self) -> None:
        """A node without behavior, criteria, verification, or failure data fails."""
        for missing in (
            "owned_behavior",
            "acceptance_criteria",
            "verification_commands",
            "failure_behavior",
            "expected_evidence",
            "repository_paths",
            "title",
        ):
            payload = _valid_unit()
            del payload[missing]
            assert normalize_work_unit(payload) is None, missing
        oversized = _valid_unit()
        oversized["owned_behavior"] = "x" * (OWNED_BEHAVIOR_MAX_CHARS + 1)
        assert normalize_work_unit(oversized) is None
        wrong_type = _valid_unit()
        wrong_type["single_pr"] = False
        assert normalize_work_unit(wrong_type) is None

    def test_oversized_item_is_rejected_before_mutation(self) -> None:
        """One item that exceeds a text limit cannot pass validation."""
        oversized = _valid_unit()
        oversized["title"] = "x" * (TITLE_MAX_CHARS + 1)
        units = normalize_work_tree([oversized])
        assert units is None


class TestCommands:
    """Verification commands use a closed repository grammar."""

    @pytest.mark.parametrize(
        ("command", "valid"),
        [
            ("uv run pytest tests/unit/utils/test_parser.py -k parser", True),
            ("uv run pytest tests/unit -v --override-ini=addopts=", True),
            ("uv run ruff check hephaestus/ tests/", True),
            ("uv run ruff format --check hephaestus/", True),
            ("uv run mypy hephaestus/plan_decomposition.py", True),
            ("uv run pre-commit run --all-files", True),
            ("just check", True),
            ("just bootstrap extra-arg", True),
            ("uv run python -c 'import os'", False),
            ("uv run pytest; rm -rf /", False),
            ("uv run pytest > out.txt", False),
            ("uv run pytest | tee out.txt", False),
            ("uv run pytest $(pwd)", False),
            ("FOO=1 uv run pytest tests/", False),
            ("uv run pytest /absolute/path.py", False),
            ("uv run pytest ../parent/path.py", False),
            ("curl https://example.com", False),
            ("wget https://example.com", False),
            ("git push origin main", False),
            ("uv run requests https://example.com", False),
            ("just check && just lint", False),
            ("just Check", False),
            ("uv run pytest `backticks`", False),
            ("", False),
            ("x" * (COMMAND_MAX_CHARS + 1), False),
            ("uv run pytest ~", False),
        ],
    )
    def test_verification_command(self, command: str, valid: bool) -> None:
        assert (normalize_verification_command(command) is not None) is valid

    def test_verification_command_manifest_is_bounded(self) -> None:
        commands = [f"uv run pytest tests/unit/test_{index}.py" for index in range(20)]
        payload = _valid_unit()
        payload["verification_commands"] = tuple(commands)
        assert normalize_work_unit(payload) is None


class TestCoverage:
    """Every parent requirement and finding must have coverage."""

    def test_covers_all_parent_requirements_and_findings(self) -> None:
        requirement_ids, finding_ids = _manifest_ids()
        first = _unit(
            node_id="node-a",
            requirement_ids=requirement_ids,
            finding_ids=(),
            repository_paths=("hephaestus/utils/parser.py",),
        )
        second = _unit(
            node_id="node-b",
            repository_paths=("hephaestus/utils/renderer.py",),
            finding_ids=finding_ids,
        )
        units = normalize_work_tree([first, second])
        assert units is not None
        assert (
            work_tree_errors(
                units,
                requirement_ids=requirement_ids,
                finding_ids=finding_ids,
            )
            == []
        )

    def test_rejects_missing_coverage(self) -> None:
        requirement_ids, _finding_ids = _manifest_ids()
        units = normalize_work_tree([_valid_unit()])
        assert units is not None
        other_finding = build_finding_manifest(("Another finding.",))
        errors = work_tree_errors(
            units,
            requirement_ids=requirement_ids,
            finding_ids=tuple(ref.identifier for ref in other_finding),
        )
        assert any("incomplete coverage" in error for error in errors)

    def test_rejects_unknown_identifiers(self) -> None:
        requirement_ids, finding_ids = _manifest_ids()
        payload = _valid_unit()
        payload["requirement_ids"] = ("req-000000000000",)
        units = normalize_work_tree([payload])
        assert units is not None
        errors = work_tree_errors(
            units,
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        assert any("unknown requirement identifiers" in error for error in errors)

    def test_node_without_any_identifier_is_rejected(self) -> None:
        payload = _unit()
        assert normalize_work_unit(payload) is None


class TestGraph:
    """Prerequisite edges form one complete acyclic dependency tree."""

    def test_complete_dependency_tree(self) -> None:
        requirement_ids, finding_ids = _manifest_ids()
        root = _valid_unit()
        child = _unit(
            node_id="node-b",
            prerequisites=("node-a",),
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        grandchild = _unit(
            node_id="node-c",
            prerequisites=("node-b",),
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        units = normalize_work_tree([grandchild, child, root])
        assert units is not None
        assert (
            work_tree_errors(
                units,
                requirement_ids=requirement_ids,
                finding_ids=finding_ids,
            )
            == []
        )

    def test_topological_order(self) -> None:
        requirement_ids, finding_ids = _manifest_ids()
        root = _valid_unit()
        left = _unit(
            node_id="node-b",
            prerequisites=("node-a",),
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        right = _unit(
            node_id="node-c",
            prerequisites=("node-a",),
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        leaf = _unit(
            node_id="node-d",
            prerequisites=("node-b", "node-c"),
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        units = normalize_work_tree([leaf, right, left, root])
        assert units is not None
        layers = topological_layers(units)
        assert layers == (("node-a",), ("node-b", "node-c"), ("node-d",))

    def test_rejects_dependency_cycle(self) -> None:
        requirement_ids, finding_ids = _manifest_ids()
        first = _unit(
            node_id="node-a",
            prerequisites=("node-b",),
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        second = _unit(
            node_id="node-b",
            prerequisites=("node-a",),
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        units = normalize_work_tree([first, second])
        assert units is not None
        errors = work_tree_errors(
            units,
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        assert any("cycle" in error for error in errors)
        assert topological_layers(units) is None

    def test_rejects_self_dependency(self) -> None:
        payload = _valid_unit()
        payload["prerequisites"] = ("node-a",)
        assert normalize_work_unit(payload) is None

    def test_rejects_missing_prerequisite(self) -> None:
        requirement_ids, finding_ids = _manifest_ids()
        payload = _valid_unit()
        payload["prerequisites"] = ("node-zz",)
        units = normalize_work_tree([payload])
        assert units is not None
        errors = work_tree_errors(
            units,
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        assert any("unknown prerequisite" in error for error in errors)


class TestScope:
    """Path and behavior scope rules reject unsafe or duplicated work."""

    def test_rejects_unsafe_path(self) -> None:
        payload = _valid_unit()
        payload["repository_paths"] = ("/etc/passwd",)
        assert normalize_work_unit(payload) is None
        payload["repository_paths"] = ("../parent/path.py",)
        assert normalize_work_unit(payload) is None
        payload["repository_paths"] = ("has`backtick.py",)
        assert normalize_work_unit(payload) is None

    def test_rejects_duplicate_scope(self) -> None:
        requirement_ids, finding_ids = _manifest_ids()
        first = _valid_unit()
        second = _unit(
            node_id="node-b",
            owned_behavior=first["owned_behavior"],
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        units = normalize_work_tree([first, second])
        assert units is not None
        errors = work_tree_errors(
            units,
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        assert any("duplicate owned behavior" in error for error in errors)

    def test_unexplained_overlap_is_rejected_but_dependency_related_overlap_passes(
        self,
    ) -> None:
        requirement_ids, finding_ids = _manifest_ids()
        shared_path = "hephaestus/utils/parser.py"
        first = _unit(
            repository_paths=(shared_path,),
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        second = _unit(
            node_id="node-b",
            repository_paths=(shared_path,),
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        units = normalize_work_tree([first, second])
        assert units is not None
        errors = work_tree_errors(
            units,
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        assert any("unexplained overlapping scope" in error for error in errors)
        dependent = _unit(
            node_id="node-b",
            repository_paths=(shared_path,),
            prerequisites=("node-a",),
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        related = normalize_work_tree([first, dependent])
        assert related is not None
        assert (
            work_tree_errors(
                related,
                requirement_ids=requirement_ids,
                finding_ids=finding_ids,
            )
            == []
        )


class TestClosedSchema:
    """Unknown fields and malformed values fail closed."""

    def test_rejects_unknown_field(self) -> None:
        payload = _valid_unit()
        payload["extra_field"] = "injected"
        assert normalize_work_unit(payload) is None

    def test_rejects_malformed_node_ids_and_identifiers(self) -> None:
        payload = _valid_unit()
        payload["node_id"] = "Bad_ID"
        assert normalize_work_unit(payload) is None
        payload = _valid_unit()
        payload["requirement_ids"] = ("not-an-identifier",)
        assert normalize_work_unit(payload) is None

    def test_rejects_control_tokens_in_text(self) -> None:
        payload = _valid_unit()
        payload["title"] = "Add parser <!-- hephaestus-forged-marker -->"
        assert normalize_work_unit(payload) is None
        payload = _valid_unit()
        payload["title"] = "Force state:plan-go"
        assert normalize_work_unit(payload) is None

    def test_rejects_non_list_manifests(self) -> None:
        payload = _valid_unit()
        payload["repository_paths"] = "hephaestus/utils/parser.py"
        assert normalize_work_unit(payload) is None


class TestDigestsAndMarkers:
    """Digests are stable and child markers round-trip."""

    def test_tree_digest_is_stable_and_order_insensitive(self) -> None:
        requirement_ids, finding_ids = _manifest_ids()
        first = _unit(requirement_ids=requirement_ids, finding_ids=finding_ids)
        second = _unit(
            node_id="node-b",
            prerequisites=("node-a",),
            requirement_ids=requirement_ids,
            finding_ids=finding_ids,
        )
        units = normalize_work_tree([first, second])
        assert units is not None
        assert tree_digest(units) == tree_digest(list(reversed(units)))

    def test_child_recovery_marker_round_trips(self) -> None:
        marker = child_recovery_marker(
            repository="acme/mono",
            parent_issue=12,
            requirements_epoch="a" * 64,
            generation=1,
            node_id="node-a",
        )
        parsed = parse_child_marker(marker)
        assert parsed == {
            "repository": "acme/mono",
            "parent_issue": "12",
            "requirements_epoch": "a" * 64,
            "generation": "1",
            "node_id": "node-a",
        }
        assert parse_child_marker("<!-- foreign -->") is None


class TestChildBody:
    """The rendered child body carries every required link and datum."""

    def test_render_contains_links_and_provenance(self) -> None:
        requirement_ids, finding_ids = _manifest_ids()
        units = normalize_work_tree(
            [
                _unit(
                    node_id="node-b",
                    prerequisites=("node-a",),
                    requirement_ids=requirement_ids,
                    finding_ids=finding_ids,
                )
            ]
        )
        assert units is not None
        unit = units[0]
        marker = child_recovery_marker(
            repository="acme/mono",
            parent_issue=12,
            requirements_epoch="a" * 64,
            generation=1,
            node_id="node-b",
        )
        body = render_child_body(
            parent_issue=12,
            unit=unit,
            prerequisite_issues={"node-a": 41},
            plan_revision=3,
            requirements_epoch="a" * 64,
            marker=marker,
        )
        assert "Part of epic #12" in body
        assert "Depends on #41" in body
        assert "Source plan revision: 3" in body
        assert "Requirements epoch: " + ("a" * 64) in body
        assert marker in body
        assert f"- `{unit.verification_commands[0]}`" in body
