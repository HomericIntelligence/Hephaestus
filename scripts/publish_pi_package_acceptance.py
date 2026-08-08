#!/usr/bin/env python3
"""Publish and exactly read back Athena Pi package acceptance evidence.

This is the sole forge-write step in the acceptance workflow. The collector is
read-only; publication creates or updates one authenticated-actor-owned marker
comment on Hephaestus issue #2515.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pi_package_acceptance import (
    ATHENA_ISSUE_URL,
    CATALOG_PATH,
    HEPHAESTUS_REPOSITORY,
    GhGitHubTransport,
    GitHubTransport,
    atomic_write,
    catalog_digest,
    load_catalog,
    render_issue_comment,
)

from hephaestus.github.client import GitHubUnavailableError

MARKER = "<!-- hephaestus-pi-package-acceptance:athena-v0.4.0 -->"
ISSUE_COMMENTS = "/repos/HomericIntelligence/Hephaestus/issues/2515/comments"


class IndeterminateWriteError(RuntimeError):
    """Raised when a forge write may have succeeded without a response."""


def update_athena_catalog_commit(path: Path, accepted_commit: str) -> None:
    """Atomically update only the accepted full Athena commit in the catalog."""
    if re.fullmatch(r"[0-9a-f]{40}", accepted_commit) is None:
        raise ValueError("accepted Athena commit must be a full lowercase SHA")
    try:
        document = _object(json.loads(path.read_text(encoding="utf-8")), "catalog")
        packages = _object(document.get("packages"), "catalog.packages")
        athena = _object(packages.get("athena"), "catalog.packages.athena")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load package catalog: {exc}") from exc
    athena["commit"] = accepted_commit
    # Re-parse before replacement so malformed surrounding records cannot be published.
    temporary = path.with_name(f".{path.name}.validation")
    try:
        temporary.write_text(json.dumps(document), encoding="utf-8")
        load_catalog(temporary)
    finally:
        temporary.unlink(missing_ok=True)
    atomic_write(path, json.dumps(document, indent=2) + "\n")


class PublishingGitHubTransport(GhGitHubTransport):
    """Classify ambiguous transport failures separately from definitive errors."""

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Perform a request, marking transport failures during writes ambiguous."""
        try:
            return super().request(method, path, body)
        except (subprocess.TimeoutExpired, GitHubUnavailableError, OSError) as exc:
            if method.upper() in {"POST", "PATCH"}:
                raise IndeterminateWriteError(str(exc)) from exc
            raise


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a JSON object")
    return cast(dict[str, Any], value)


def _load_evidence(path: Path) -> dict[str, Any]:
    try:
        evidence = _object(json.loads(path.read_text(encoding="utf-8")), "evidence")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load acceptance evidence: {exc}") from exc
    if evidence.get("schema_version") != 1:
        raise ValueError("evidence.schema_version must equal 1")
    return evidence


def _validate_inputs(evidence_path: Path, comment_path: Path) -> tuple[dict[str, Any], str]:
    evidence = _load_evidence(evidence_path)
    catalog = load_catalog(CATALOG_PATH)
    if evidence.get("catalog_sha256") != catalog_digest(CATALOG_PATH):
        raise ValueError("evidence does not bind the current package catalog")
    if evidence.get("package") != asdict(catalog.package):
        raise ValueError("evidence package does not match the catalog")
    if evidence.get("compatibility") != asdict(catalog.compatibility):
        raise ValueError("evidence compatibility does not match the catalog")
    if evidence.get("upstream") != asdict(catalog.upstream):
        raise ValueError("evidence upstream identity does not match the catalog")
    implementation = _object(evidence.get("implementation"), "evidence.implementation")
    if implementation.get("repository") != HEPHAESTUS_REPOSITORY:
        raise ValueError("evidence implementation repository is invalid")
    if (
        not isinstance(implementation.get("pull_request"), int)
        or implementation["pull_request"] <= 0
    ):
        raise ValueError("evidence implementation pull request is invalid")
    head = implementation.get("head")
    if not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise ValueError("evidence implementation head is invalid")
    discovery = _object(evidence.get("discovery"), "evidence.discovery")
    if discovery.get("installed_commit") != catalog.package.ref:
        raise ValueError("evidence installed commit does not match the catalog")
    if discovery.get("commands") != ["skill:advise", "skill:learn", "skill:pr-review"]:
        raise ValueError("evidence command discovery is incomplete")
    archive = _object(evidence.get("archive"), "evidence.archive")
    archive_digest = archive.get("sha256")
    if (
        not isinstance(archive_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", archive_digest) is None
        or not isinstance(archive.get("members"), int)
        or archive["members"] <= 0
    ):
        raise ValueError("evidence archive receipt is invalid")
    required_check = _object(evidence.get("required_check"), "evidence.required_check")
    if required_check.get("name") != catalog.upstream.required_check or not isinstance(
        required_check.get("url"), str
    ):
        raise ValueError("evidence required check receipt is invalid")
    try:
        comment = comment_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot load generated issue comment: {exc}") from exc
    expected = render_issue_comment(evidence)
    if comment != expected or not comment.startswith(MARKER + "\n"):
        raise ValueError("comment is not the exact rendering of acceptance evidence")
    return evidence, comment


def _validate_implementation_pr(evidence: dict[str, Any], transport: GitHubTransport) -> None:
    implementation = _object(evidence["implementation"], "evidence.implementation")
    number = cast(int, implementation["pull_request"])
    response = _object(
        transport.request("GET", f"/repos/{HEPHAESTUS_REPOSITORY}/pulls/{number}"),
        "Hephaestus pull request response",
    )
    body = response.get("body")
    if not isinstance(body, str) or ATHENA_ISSUE_URL not in body:
        raise ValueError("implementation PR no longer links Athena #61")
    if "Closes #2515" not in body.splitlines():
        raise ValueError("implementation PR lacks the literal Closes #2515 line")
    head = _object(response.get("head"), "Hephaestus pull request head")
    if head.get("sha") != implementation["head"]:
        raise ValueError("implementation PR head no longer matches acceptance evidence")


def _list_comments(transport: GitHubTransport) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    page = 1
    while True:
        response = transport.request("GET", f"{ISSUE_COMMENTS}?per_page=100&page={page}")
        if not isinstance(response, list):
            raise ValueError("issue comments response must be a list")
        for item in response:
            comments.append(_object(item, "issue comment"))
        if len(response) < 100:
            return comments
        page += 1


def _marker_comments(transport: GitHubTransport) -> list[dict[str, Any]]:
    return [
        comment
        for comment in _list_comments(transport)
        if isinstance(comment.get("body"), str) and MARKER in comment["body"]
    ]


def _actor_login(transport: GitHubTransport) -> str:
    actor = _object(transport.request("GET", "/user"), "authenticated actor")
    login = actor.get("login")
    if not isinstance(login, str) or not login:
        raise ValueError("authenticated actor response lacks login")
    return login


def _comment_owner(comment: dict[str, Any]) -> str:
    user = _object(comment.get("user"), "issue comment user")
    login = user.get("login")
    if not isinstance(login, str) or not login:
        raise ValueError("issue comment user lacks login")
    return login


def _comment_id(comment: dict[str, Any]) -> int:
    identifier = comment.get("id")
    if not isinstance(identifier, int) or identifier <= 0:
        raise ValueError("issue comment lacks a positive id")
    return identifier


def _exact_comment_url(comment: dict[str, Any], body: str, actor: str) -> str:
    if comment.get("body") != body or _comment_owner(comment) != actor:
        raise ValueError("GitHub comment readback does not exactly match the actor-owned body")
    url = comment.get("html_url")
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        raise ValueError("GitHub comment readback lacks a canonical URL")
    return url


def _reconcile_indeterminate_write(transport: GitHubTransport, body: str, actor: str) -> str:
    matching = _marker_comments(transport)
    exact = [
        comment
        for comment in matching
        if comment.get("body") == body and _comment_owner(comment) == actor
    ]
    if len(exact) != 1:
        raise IndeterminateWriteError(
            "indeterminate write did not produce one exact actor-owned comment"
        )
    return _exact_comment_url(exact[0], body, actor)


def publish_acceptance(
    *,
    evidence: Path,
    comment: Path,
    transport: GitHubTransport,
) -> str:
    """Publish one actor-owned acceptance comment and return its read-back URL."""
    evidence_document, body = _validate_inputs(evidence, comment)
    _validate_implementation_pr(evidence_document, transport)
    actor = _actor_login(transport)
    markers = _marker_comments(transport)
    if len(markers) > 1:
        raise ValueError("multiple Athena acceptance markers exist")
    if markers and _comment_owner(markers[0]) != actor:
        raise ValueError("Athena acceptance marker is owned by another actor")

    if markers and markers[0].get("body") == body:
        identifier = _comment_id(markers[0])
        readback = _object(
            transport.request(
                "GET", f"/repos/{HEPHAESTUS_REPOSITORY}/issues/comments/{identifier}"
            ),
            "issue comment readback",
        )
        return _exact_comment_url(readback, body, actor)

    method = "PATCH" if markers else "POST"
    path = (
        f"/repos/{HEPHAESTUS_REPOSITORY}/issues/comments/{_comment_id(markers[0])}"
        if markers
        else ISSUE_COMMENTS
    )
    try:
        written = _object(
            transport.request(method, path, {"body": body}),
            "issue comment write response",
        )
    except IndeterminateWriteError:
        return _reconcile_indeterminate_write(transport, body, actor)
    identifier = _comment_id(written)
    readback = _object(
        transport.request("GET", f"/repos/{HEPHAESTUS_REPOSITORY}/issues/comments/{identifier}"),
        "issue comment readback",
    )
    return _exact_comment_url(readback, body, actor)


def build_parser() -> argparse.ArgumentParser:
    """Build the publication command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--comment", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the explicit acceptance publication step."""
    arguments = build_parser().parse_args(argv)
    try:
        url = publish_acceptance(
            evidence=arguments.acceptance,
            comment=arguments.comment,
            transport=PublishingGitHubTransport(),
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
