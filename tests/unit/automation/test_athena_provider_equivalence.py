"""Tests for provider-neutral Athena skill request construction."""
# ruff: noqa: D103

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from hephaestus.automation.pipeline.athena_skill_jobs import build_athena_skill_request


def test_providers_build_equivalent_advise_requests_except_runtime_adapter(tmp_path: Path) -> None:
    payload = {"issue_number": 7, "issue_title": "Title", "issue_body": "Body"}

    requests = [
        build_athena_skill_request(
            kind="advise",
            repo="HomericIntelligence/Hephaestus",
            issue=7,
            agent=agent,
            model="default",
            cwd=tmp_path,
            timeout_s=60,
            payload=payload,
        )
        for agent in ("claude", "codex", "pi")
    ]

    normalized = [replace(request, agent="<provider>") for request in requests]
    assert normalized[0] == normalized[1] == normalized[2]


def test_direct_and_pipeline_factories_can_share_learn_request_shape(tmp_path: Path) -> None:
    pipeline_request = build_athena_skill_request(
        kind="learn",
        repo="HomericIntelligence/Hephaestus",
        issue=8,
        agent="pi",
        model="default",
        cwd=tmp_path,
        timeout_s=90,
        payload={"context": "approved plan"},
    )
    direct_request = build_athena_skill_request(
        kind="learn",
        repo="HomericIntelligence/Hephaestus",
        issue=8,
        agent="pi",
        model="default",
        cwd=Path(tmp_path),
        timeout_s=90,
        payload={"context": "approved plan"},
    )

    assert direct_request == pipeline_request
