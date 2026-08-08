"""Tests for publishing actor-owned Athena Pi acceptance evidence."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "publish_pi_package_acceptance.py"
COLLECTOR = Path(__file__).resolve().parents[3] / "scripts" / "pi_package_acceptance.py"
ATHENA_REF = "496815b00f6fb4c8e97466489371b364d52588b5"
MARKER = "<!-- hephaestus-pi-package-acceptance:athena-v0.4.0 -->"


def _load(path: Path, name: str) -> ModuleType:
    assert path.is_file(), f"{path.name} must exist"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules() -> tuple[ModuleType, ModuleType]:
    """Load the collector and publisher standalone scripts."""
    collector = _load(COLLECTOR, "pi_package_acceptance_for_publish_tests")
    publisher = _load(SCRIPT, "publish_pi_package_acceptance")
    return collector, publisher


class FakeTransport:
    """Record requests and return endpoint-specific responses."""

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path, body))
        response = self.responses[(method, path)]
        if isinstance(response, Exception):
            raise response
        return response


def _write_inputs(
    tmp_path: Path, collector: ModuleType, publisher: ModuleType
) -> tuple[Path, Path, Path, str]:
    catalog = tmp_path / "athena-pi-package.json"
    document = {
        "schema_version": 1,
        "package": {
            "source": "git:github.com/HomericIntelligence/Athena",
            "version": "v0.4.0",
            "ref": ATHENA_REF,
        },
        "compatibility": {
            "pi": "@earendil-works/pi-coding-agent@0.80.2",
            "delegation": "pi-subagents@0.37.2",
            "web_access": "pi-web-access@0.15.0",
        },
        "upstream": {
            "issue": "https://github.com/HomericIntelligence/Athena/issues/61",
            "pull_request": "https://github.com/HomericIntelligence/Athena/pull/62",
            "release_tag": "v0.4.0",
            "required_check": "package",
        },
    }
    catalog.write_text(json.dumps(document), encoding="utf-8")
    publisher.__dict__["CATALOG_PATH"] = catalog
    evidence_document = {
        "schema_version": 1,
        "catalog_sha256": collector.catalog_digest(catalog),
        "package": document["package"],
        "compatibility": document["compatibility"],
        "upstream": document["upstream"],
        "implementation": {
            "repository": "HomericIntelligence/Hephaestus",
            "pull_request": 77,
            "head": "a" * 40,
            "url": "https://github.com/HomericIntelligence/Hephaestus/pull/77",
        },
        "discovery": {
            "installed_commit": ATHENA_REF,
            "commands": ["skill:advise", "skill:learn", "skill:pr-review"],
        },
        "archive": {"sha256": "b" * 64, "members": 42},
        "required_check": {"name": "package", "url": "https://github.com/example/check/1"},
    }
    evidence = tmp_path / "acceptance.json"
    evidence.write_text(json.dumps(evidence_document), encoding="utf-8")
    body = collector.render_issue_comment(evidence_document)
    comment = tmp_path / "issue-comment.md"
    comment.write_text(body, encoding="utf-8")
    return catalog, evidence, comment, body


def _base_responses(body: str) -> dict[tuple[str, str], Any]:
    return {
        ("GET", "/user"): {"login": "maintainer"},
        ("GET", "/repos/HomericIntelligence/Hephaestus/pulls/77"): {
            "body": (
                "Upstream: https://github.com/HomericIntelligence/Athena/issues/61"
                "\n\nCloses #2515\n"
            ),
            "head": {"sha": "a" * 40},
        },
        (
            "GET",
            "/repos/HomericIntelligence/Hephaestus/issues/2515/comments?per_page=100&page=1",
        ): [],
        ("POST", "/repos/HomericIntelligence/Hephaestus/issues/2515/comments"): {
            "id": 10,
            "body": body,
            "user": {"login": "maintainer"},
            "html_url": "https://github.com/example/comment/10",
        },
        ("GET", "/repos/HomericIntelligence/Hephaestus/issues/comments/10"): {
            "id": 10,
            "body": body,
            "user": {"login": "maintainer"},
            "html_url": "https://github.com/example/comment/10",
        },
    }


def test_catalog_commit_update_preserves_every_other_record(
    modules: tuple[ModuleType, ModuleType], tmp_path: Path
) -> None:
    """Acceptance publication changes only a validated full Athena commit."""
    collector, publisher = modules
    catalog, _evidence, _comment, _body = _write_inputs(tmp_path, collector, publisher)
    # Exercise the packaged-catalog form owned by #2516 rather than the legacy fixture.
    source = (
        Path(__file__).resolve().parents[3] / "hephaestus" / "agents" / "pi_package_catalog.json"
    )
    before = json.loads(source.read_text(encoding="utf-8"))
    catalog.write_text(json.dumps(before), encoding="utf-8")

    publisher.update_athena_catalog_commit(catalog, "b" * 40)

    after = json.loads(catalog.read_text(encoding="utf-8"))
    assert after["packages"]["athena"]["commit"] == "b" * 40
    before["packages"]["athena"]["commit"] = "b" * 40
    assert after == before
    for invalid in ("main", "b" * 12, "B" * 40):
        with pytest.raises(ValueError, match="full lowercase SHA"):
            publisher.update_athena_catalog_commit(catalog, invalid)


def test_create_and_exact_readback(modules: tuple[ModuleType, ModuleType], tmp_path: Path) -> None:
    """No marker creates one actor-owned comment and verifies its exact body."""
    collector, publisher = modules
    _, evidence, comment, body = _write_inputs(tmp_path, collector, publisher)
    transport = FakeTransport(_base_responses(body))

    url = publisher.publish_acceptance(evidence=evidence, comment=comment, transport=transport)

    assert url.endswith("/10")
    assert ("POST", publisher.ISSUE_COMMENTS, {"body": body}) in transport.calls


def test_actor_owned_comment_updates_or_is_idempotent(
    modules: tuple[ModuleType, ModuleType], tmp_path: Path
) -> None:
    """The publisher updates only its own marker and skips an exact match."""
    collector, publisher = modules
    _, evidence, comment, body = _write_inputs(tmp_path, collector, publisher)
    comments_path = publisher.ISSUE_COMMENTS + "?per_page=100&page=1"
    responses = _base_responses(body)
    responses[("GET", comments_path)] = [
        {"id": 9, "body": MARKER + "\nstale", "user": {"login": "maintainer"}}
    ]
    responses[("PATCH", "/repos/HomericIntelligence/Hephaestus/issues/comments/9")] = {
        "id": 9,
        "body": body,
        "user": {"login": "maintainer"},
        "html_url": "https://github.com/example/comment/9",
    }
    responses[("GET", "/repos/HomericIntelligence/Hephaestus/issues/comments/9")] = responses[
        ("PATCH", "/repos/HomericIntelligence/Hephaestus/issues/comments/9")
    ]
    transport = FakeTransport(responses)

    assert publisher.publish_acceptance(
        evidence=evidence, comment=comment, transport=transport
    ).endswith("/9")
    assert any(call[0] == "PATCH" for call in transport.calls)

    responses[("GET", comments_path)] = [
        responses[("GET", "/repos/HomericIntelligence/Hephaestus/issues/comments/9")]
    ]
    transport = FakeTransport(responses)
    assert publisher.publish_acceptance(
        evidence=evidence, comment=comment, transport=transport
    ).endswith("/9")
    assert not any(call[0] in {"POST", "PATCH"} for call in transport.calls)


@pytest.mark.parametrize(
    "comments,match",
    [
        ([{"id": 1, "body": MARKER, "user": {"login": "someone-else"}}], "owned by"),
        (
            [
                {"id": 1, "body": MARKER, "user": {"login": "maintainer"}},
                {"id": 2, "body": MARKER, "user": {"login": "maintainer"}},
            ],
            "multiple",
        ),
    ],
)
def test_foreign_or_duplicate_markers_fail_closed(
    modules: tuple[ModuleType, ModuleType],
    tmp_path: Path,
    comments: list[dict[str, Any]],
    match: str,
) -> None:
    """Marker ownership ambiguity never causes a blind create or update."""
    collector, publisher = modules
    _, evidence, comment, body = _write_inputs(tmp_path, collector, publisher)
    responses = _base_responses(body)
    responses[("GET", publisher.ISSUE_COMMENTS + "?per_page=100&page=1")] = comments

    with pytest.raises(ValueError, match=match):
        publisher.publish_acceptance(
            evidence=evidence, comment=comment, transport=FakeTransport(responses)
        )


def test_comment_pagination_finds_marker_on_later_page(
    modules: tuple[ModuleType, ModuleType], tmp_path: Path
) -> None:
    """Marker reconciliation inspects every comments page."""
    collector, publisher = modules
    _, evidence, comment, body = _write_inputs(tmp_path, collector, publisher)
    responses = _base_responses(body)
    responses[("GET", publisher.ISSUE_COMMENTS + "?per_page=100&page=1")] = [
        {"id": index, "body": "ordinary", "user": {"login": "user"}} for index in range(100)
    ]
    responses[("GET", publisher.ISSUE_COMMENTS + "?per_page=100&page=2")] = [
        {
            "id": 101,
            "body": body,
            "user": {"login": "maintainer"},
            "html_url": "https://github.com/example/comment/101",
        }
    ]
    responses[("GET", "/repos/HomericIntelligence/Hephaestus/issues/comments/101")] = responses[
        ("GET", publisher.ISSUE_COMMENTS + "?per_page=100&page=2")
    ][0]

    url = publisher.publish_acceptance(
        evidence=evidence, comment=comment, transport=FakeTransport(responses)
    )

    assert url.endswith("/101")


def test_malformed_evidence_or_readback_mismatch_is_rejected(
    modules: tuple[ModuleType, ModuleType], tmp_path: Path
) -> None:
    """Evidence/catalog drift and non-exact GitHub readback both fail closed."""
    collector, publisher = modules
    _, evidence, comment, body = _write_inputs(tmp_path, collector, publisher)
    document = json.loads(evidence.read_text(encoding="utf-8"))
    document["package"]["ref"] = "c" * 40
    evidence.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="catalog"):
        publisher.publish_acceptance(
            evidence=evidence,
            comment=comment,
            transport=FakeTransport(_base_responses(body)),
        )

    _, evidence, comment, body = _write_inputs(tmp_path, collector, publisher)
    responses = _base_responses(body)
    responses[("GET", "/repos/HomericIntelligence/Hephaestus/issues/comments/10")]["body"] = (
        body + "changed"
    )
    with pytest.raises(ValueError, match="readback"):
        publisher.publish_acceptance(
            evidence=evidence, comment=comment, transport=FakeTransport(responses)
        )


def test_indeterminate_create_recovers_only_from_exact_readback(
    modules: tuple[ModuleType, ModuleType], tmp_path: Path
) -> None:
    """An ambiguous write is reconciled once without blindly writing again."""
    collector, publisher = modules
    _, evidence, comment, body = _write_inputs(tmp_path, collector, publisher)
    responses = _base_responses(body)
    responses[("POST", publisher.ISSUE_COMMENTS)] = publisher.IndeterminateWriteError("timeout")
    comments_path = publisher.ISSUE_COMMENTS + "?per_page=100&page=1"
    pages = iter(
        [
            [],
            [
                {
                    "id": 12,
                    "body": body,
                    "user": {"login": "maintainer"},
                    "html_url": "https://github.com/example/comment/12",
                }
            ],
        ]
    )

    class RecoveringTransport(FakeTransport):
        def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            if (method, path) == ("GET", comments_path):
                self.calls.append((method, path, body))
                return next(pages)
            return super().request(method, path, body)

    transport = RecoveringTransport(responses)

    assert publisher.publish_acceptance(
        evidence=evidence, comment=comment, transport=transport
    ).endswith("/12")
    assert sum(call[0] == "POST" for call in transport.calls) == 1
