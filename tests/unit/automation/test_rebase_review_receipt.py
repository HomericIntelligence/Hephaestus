"""Test durable rebase records without restoring merge authority."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.rebase_review_receipt import (
    RebaseReviewRecord,
    original_audit_identity,
    parse_review_rebase_record,
    render_review_rebase_record,
)
from hephaestus.automation.review_audit import ReviewAudit, render_implementation_go_audit


def record() -> RebaseReviewRecord:
    """Return complete review and rebase facts."""
    return RebaseReviewRecord(
        repository="LLM360/comet",
        issue_number=3,
        pr_number=7,
        reviewed_head_sha="a" * 40,
        reviewed_base_sha="b" * 40,
        source_head_sha="a" * 40,
        target_base_sha="c" * 40,
        resulting_head_sha="d" * 40,
        resulting_tree_sha="e" * 40,
        original_audit_id=original_audit_identity(7, "a" * 40),
        audit=ReviewAudit("A", "Checks passed.", (), "Evidence retained.", True, "GO"),
    )


def test_record_roundtrip_preserves_original_review() -> None:
    """Retain the original audit across serialization."""
    original = record()
    _, body = render_review_rebase_record(original)
    assert parse_review_rebase_record(body) == original
    assert "retained_rebase_review_proof" not in body


@pytest.mark.parametrize(
    "field,value",
    [
        ("pr_number", True),
        ("issue_number", 0),
        ("resulting_head_sha", "bad"),
        ("repository", "comet"),
        ("state", "approved"),
        ("original_audit_id", "changed"),
    ],
)
def test_record_rejects_invalid_identity(field: str, value: object) -> None:
    """Reject malformed identity fields."""
    _, body = render_review_rebase_record(record())
    marker, _, raw = body.partition("\n")
    payload = json.loads(raw)
    payload[field] = value
    with pytest.raises(ValueError):
        parse_review_rebase_record(marker + "\n" + json.dumps(payload))


def test_parser_rejects_duplicate_fields() -> None:
    """Reject duplicate JSON keys."""
    _, body = render_review_rebase_record(record())
    marker, _, raw = body.partition("\n")
    raw = raw.replace('"state":"active"', '"state":"active","state":"active"')
    with pytest.raises(ValueError, match="duplicate"):
        parse_review_rebase_record(marker + "\n" + raw)


class MemoryHost(PipelineGitHub):
    """Use an in-memory comment transport."""

    org = "LLM360"
    repo = "comet"

    def __init__(self, bodies: list[str]) -> None:
        """Set the initial comment bodies."""
        self.bodies = bodies

    def _repo_issue_comments(self, number: int) -> list[dict[str, object]]:
        return [{"body": body, "databaseId": index + 1} for index, body in enumerate(self.bodies)]

    def _comment_owned_by_viewer(self, comment: dict[str, object]) -> bool:
        return True

    def upsert_issue_comment(
        self, issue_number: int, marker: str, body: str, *, legacy_marker: str | None = None
    ) -> None:
        """Replace the comment with the selected marker."""
        self.bodies = [prior for prior in self.bodies if not prior.startswith(marker)] + [body]

    def _patch_issue_comment(
        self, comment_id: int, body: str, *, repo: tuple[str, str] | None = None
    ) -> None:
        self.bodies[comment_id - 1] = body


def test_publication_requires_original_owned_audit() -> None:
    """Require the original audit for publication and recovery."""
    value = record()
    _, audit_body = render_implementation_go_audit(
        value.audit, pr_number=value.pr_number, head_sha=value.reviewed_head_sha
    )
    host = MemoryHost([audit_body])
    host.publish_review_rebase_record(value)
    assert host.read_review_rebase_record(7) == value
    host.bodies.remove(audit_body)
    with pytest.raises(RuntimeError, match="absent or changed"):
        host.read_review_rebase_record(7)


def test_missing_audit_prevents_publication() -> None:
    """Do not publish rebase facts without the original owned audit."""
    host = MemoryHost([])
    with pytest.raises(RuntimeError, match="absent or changed"):
        host.publish_review_rebase_record(record())
    assert host.bodies == []


@pytest.mark.parametrize("problem", ["revoked", "duplicate", "foreign_repo", "malformed"])
def test_recovery_rejects_ambiguous_or_revoked_records(problem: str) -> None:
    """Stop recovery when its durable evidence is invalid."""
    value = record()
    _, audit_body = render_implementation_go_audit(
        value.audit, pr_number=value.pr_number, head_sha=value.reviewed_head_sha
    )
    if problem == "revoked":
        value = replace(value, state="revoked")
    if problem == "foreign_repo":
        value = replace(value, repository="Other/comet")
    _, body = render_review_rebase_record(value)
    if problem == "malformed":
        marker, _, raw = body.partition("\n")
        payload = json.loads(raw)
        payload["extra"] = "untrusted"
        body = marker + "\n" + json.dumps(payload)
    bodies = [audit_body, body] + ([body] if problem == "duplicate" else [])
    with pytest.raises((RuntimeError, ValueError)):
        MemoryHost(bodies).read_review_rebase_record(7)


def test_restart_retains_record_without_process_proof() -> None:
    """Restore facts without claiming current merge authority."""
    from hephaestus.automation.pipeline.coordinator_sources import SourceCoordinator
    from hephaestus.automation.pipeline.routing import StageName
    from hephaestus.automation.pipeline.seeding import IssueFacts, seed_entry_from_facts

    value = record()
    facts = IssueFacts(
        number=3,
        title="Task",
        is_epic=False,
        labels={"state:plan-go"},
        pr_number=7,
        pr_is_open=True,
        pr_is_merged=False,
        pr_has_implementation_go=True,
        pending_review_rebase_record=value,
    )
    entry = seed_entry_from_facts(facts)
    assert entry.stage is StageName.MERGE_WAIT
    item = SourceCoordinator._entry_to_item(entry, "comet")
    assert item.payload["pending_review_rebase_record"] == value
    assert "retained_rebase_review_proof" not in item.payload
    assert "reviewed_pr_head_sha" not in item.payload


def test_revoked_record_stops_seeding() -> None:
    """Keep revoked evidence out of the review queue."""
    from hephaestus.automation.pipeline.seeding import (
        IssueClassificationError,
        read_review_rebase_record,
    )

    _, body = render_review_rebase_record(replace(record(), state="revoked"))
    with pytest.raises(IssueClassificationError, match="recovery blocked"):
        read_review_rebase_record(MemoryHost([body]), 7)


def test_retained_record_without_go_still_requires_host_verification() -> None:
    """Do not restart review when the GO label is absent."""
    from hephaestus.automation.pipeline.routing import StageName
    from hephaestus.automation.pipeline.seeding import IssueFacts, seed_entry_from_facts

    facts = IssueFacts(
        number=3,
        title="Task",
        is_epic=False,
        labels={"state:plan-go"},
        pr_number=7,
        pr_is_open=True,
        pr_is_merged=False,
        pr_has_implementation_go=False,
        pending_review_rebase_record=record(),
    )
    assert seed_entry_from_facts(facts).stage is StageName.MERGE_WAIT


def test_recovery_ignores_foreign_rebase_records() -> None:
    """A foreign comment cannot restore a review record."""

    class Host(MemoryHost):
        org = "LLM360"
        repo = "comet"

        def _repo_issue_comments(self, number: int) -> list[dict[str, object]]:
            return [{"body": "<!-- hephaestus-review-rebase:v1 -->\n{}"}]

        def _comment_owned_by_viewer(self, comment: dict[str, object]) -> bool:
            return False

    assert Host([]).read_review_rebase_record(7) is None


def test_closed_publication_boundary_rejects_moved_source(tmp_path: Path) -> None:
    """Do not publish a receipt after the live source commit changes."""
    from hephaestus.automation.pipeline import github_jobs
    from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner

    class Host(MemoryHost):
        def gh_pr_state(self, number: int) -> dict[str, object]:
            return {
                "state": "OPEN",
                "headRefOid": "f" * 40,
                "baseRefName": "main",
                "autoMergeRequest": None,
            }

    request_type = getattr(github_jobs, "PublishRebaseReviewRequest", None)
    assert callable(request_type)
    request = request_type(record())
    host = Host([])
    job = github_jobs.GitHubJob("comet", tmp_path, request, "publish rebase record")
    result = PipelineGitHubJobRunner("LLM360", False)._run_request(job, host)
    assert isinstance(result, github_jobs.RebaseReviewPublished)
    assert result.published is False
    assert host.bodies == []


@pytest.mark.parametrize("problem", [None, "no_go", "base_drift", "armed", "late_head_drift"])
def test_publication_boundary_checks_live_identity(tmp_path: Path, problem: str | None) -> None:
    """Require stable source identity and exclusive GO around publication."""
    from hephaestus.automation.pipeline.github_jobs import (
        GitHubJob,
        PublishRebaseReviewRequest,
        RebaseReviewPublished,
    )
    from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner

    value = record()

    class Host(MemoryHost):
        reads = 0

        def gh_pr_state(self, number: int) -> dict[str, object]:
            self.reads += 1
            return {
                "state": "OPEN",
                "headRefOid": (
                    "f" * 40
                    if problem == "late_head_drift" and self.reads > 1
                    else value.source_head_sha
                ),
                "baseRefOid": "f" * 40 if problem == "base_drift" else value.target_base_sha,
                "baseRefName": "main",
                "autoMergeRequest": {} if problem == "armed" else None,
            }

        def pr_has_implementation_state_label(self, number: int) -> tuple[bool, bool]:
            return (True, True) if problem == "no_go" else (True, False)

    _, audit_body = render_implementation_go_audit(
        value.audit, pr_number=value.pr_number, head_sha=value.reviewed_head_sha
    )
    host = Host([audit_body])
    request = PublishRebaseReviewRequest(value)
    job = GitHubJob("comet", tmp_path, request, "publish rebase record")
    result = PipelineGitHubJobRunner("LLM360", False)._run_request(job, host)
    assert isinstance(result, RebaseReviewPublished)
    assert result.request == request
    assert result.published is (problem is None)
    if problem not in {None, "late_head_drift"}:
        assert host.bodies == [audit_body]


@pytest.mark.parametrize("problem", [None, "revoked", "changed", "head", "base", "no_go"])
def test_inspection_requires_fresh_evidence_without_writes(
    tmp_path: Path, problem: str | None
) -> None:
    """Reject changed audit or live identity without modifying comments."""
    from hephaestus.automation.pipeline.github_jobs import (
        GitHubJob,
        InspectRebaseReviewRequest,
        RebaseReviewInspected,
    )
    from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner

    value = record()

    class Host(MemoryHost):
        reads = 0

        def gh_pr_state(self, number: int) -> dict[str, object]:
            return {
                "state": "OPEN",
                "baseRefName": "main",
                "autoMergeRequest": None,
                "headRefOid": "f" * 40 if problem == "head" else value.resulting_head_sha,
                "baseRefOid": "f" * 40 if problem == "base" else value.target_base_sha,
            }

        def pr_has_implementation_state_label(self, number: int) -> tuple[bool, bool]:
            return (True, True) if problem == "no_go" else (True, False)

        def read_review_rebase_record(self, number: int) -> RebaseReviewRecord | None:
            """Model a changed record during the second authenticated read."""
            self.reads += 1
            if self.reads == 2 and problem == "revoked":
                raise RuntimeError("record revoked")
            if self.reads == 2 and problem == "changed":
                return replace(value, resulting_head_sha="f" * 40)
            return value

    host = Host([])
    request = InspectRebaseReviewRequest(value)
    job = GitHubJob("comet", tmp_path, request, "inspect rebase record")
    result = PipelineGitHubJobRunner("LLM360", False)._run_request(job, host)
    assert isinstance(result, RebaseReviewInspected)
    assert result.request == request
    assert result.verified is (problem is None)
    assert host.bodies == []


def test_fresh_review_clears_old_process_rebase_proof() -> None:
    """Discard a prior rebase proof when a new source review begins."""
    from hephaestus.automation.pipeline.stages.pr_review_threads import _clear_round_review_state
    from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem

    item = WorkItem(repo="comet", kind=ItemKind.PR, pr=7, issue=3)
    prior = record()
    item.payload.update(
        {
            "retained_rebase_review_proof": object(),
            "pending_review_rebase_record": prior,
            "reviewed_pr_head_sha": prior.reviewed_head_sha,
        }
    )
    _clear_round_review_state(item)
    assert "retained_rebase_review_proof" not in item.payload
    assert "pending_review_rebase_record" not in item.payload


def test_new_clean_go_supersedes_record_and_preserves_original_audit() -> None:
    """A new source review replaces active rebase recovery without deleting history."""
    value = record()
    new_head = "f" * 40

    class Host(MemoryHost):
        def gh_pr_state(self, number: int) -> dict[str, object]:
            return {
                "state": "OPEN",
                "headRefOid": new_head,
                "baseRefName": "main",
                "autoMergeRequest": None,
            }

        def pr_has_implementation_state_label(self, number: int) -> tuple[bool, bool]:
            return True, False

    _, original_body = render_implementation_go_audit(
        value.audit, pr_number=7, head_sha=value.reviewed_head_sha
    )
    _, rebase_body = render_review_rebase_record(value)
    host = Host([original_body, rebase_body])
    host.publish_implementation_go_audit(7, new_head, value.audit)
    assert host.read_review_rebase_record(7) is None
    assert original_body in host.bodies
    retained = [parse_review_rebase_record(body) for body in host.bodies]
    superseded = next(item for item in retained if item is not None)
    assert superseded.state == "superseded"
    assert superseded.superseded_by_head_sha == new_head


def test_new_pending_go_precedes_old_rebase_recovery() -> None:
    """Complete fresh GO publication after a crash before retiring old rebase facts."""
    from hephaestus.automation.implementation_go_audit_receipt import PendingImplementationGoAudit
    from hephaestus.automation.pipeline.routing import StageName
    from hephaestus.automation.pipeline.seeding import IssueFacts, seed_entry_from_facts

    value = record()
    pending = PendingImplementationGoAudit(7, "f" * 40, value.audit)
    entry = seed_entry_from_facts(
        IssueFacts(
            number=3,
            title="Task",
            is_epic=False,
            labels={"state:plan-go"},
            pr_number=7,
            pr_is_open=True,
            pr_is_merged=False,
            pr_has_implementation_go=True,
            pending_review_rebase_record=value,
            pending_implementation_go_audit=pending,
        )
    )
    assert entry.stage is StageName.PR_REVIEW


@pytest.mark.parametrize("problem", ["missing_audit", "duplicate_audit", "revoked"])
def test_supersession_requires_authentic_new_review(problem: str) -> None:
    """Keep explicit revocation and missing supersession evidence closed."""
    value = record()
    new_head = "f" * 40
    retired = replace(value, state="superseded", superseded_by_head_sha=new_head)
    if problem == "revoked":
        retired = replace(value, state="revoked")
    _, old_body = render_implementation_go_audit(
        value.audit, pr_number=7, head_sha=value.reviewed_head_sha
    )
    _, new_body = render_implementation_go_audit(value.audit, pr_number=7, head_sha=new_head)
    _, retained_body = render_review_rebase_record(retired)
    bodies = [old_body, retained_body]
    if problem == "duplicate_audit":
        bodies.extend([new_body, new_body])
    if problem == "revoked":
        bodies.append(new_body)
    with pytest.raises(RuntimeError):
        MemoryHost(bodies).read_review_rebase_record(7)


def test_superseded_record_roundtrip_retains_both_review_identities() -> None:
    """Keep prior evidence while a fresh clean GO replaces recovery eligibility."""
    value = replace(record(), state="superseded", superseded_by_head_sha="f" * 40)
    _, body = render_review_rebase_record(value)
    assert parse_review_rebase_record(body) == value


def test_interrupted_supersession_preserves_pending_new_review() -> None:
    """Recover new review publication after the supersession write fails."""
    from hephaestus.automation.implementation_go_audit_receipt import (
        render_pending_implementation_go_audit,
    )

    value = record()
    new_head = "f" * 40

    class Host(MemoryHost):
        def _supersede_review_rebase_record(self, pr_number: int, head_sha: str) -> None:
            raise RuntimeError("supersession transport unavailable")

    _, original = render_implementation_go_audit(
        value.audit, pr_number=7, head_sha=value.reviewed_head_sha
    )
    _, retained = render_review_rebase_record(value)
    _, pending = render_pending_implementation_go_audit(7, new_head, value.audit)
    host = Host([original, retained, pending])
    with pytest.raises(RuntimeError, match="transport unavailable"):
        host.publish_implementation_go_audit(7, new_head, value.audit)
    restored = host.pending_implementation_go_audit(7)
    assert restored is not None
    assert restored.head_sha == new_head
    assert original in host.bodies
