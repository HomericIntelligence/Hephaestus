"""Tests for ``hephaestus.automation.reconcile_implementation_labels``.

The pass exists because a review verdict published *outside* the automation loop never
reaches the loop-owned ``state:implementation-go`` label. Scylla #2093 carried that label
for three weeks after a current-head NO-GO verdict was published.

Tests cover the untrusted-carrier boundary (integrity, exact-head binding, target identity),
the one-way fail-safe decision, the atomic mutation plus exclusive readback, and dry-run.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.reconcile_implementation_labels import (
    VERDICT_GO,
    VERDICT_NO_GO,
    PublishedVerdict,
    canonical_state_digest,
    decide,
    fetch_pr_snapshot,
    main,
    parse_carrier,
    reconcile_pr,
    select_current_head_verdict,
)
from hephaestus.automation.state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_CARRIER = REPO_ROOT / "tests" / "fixtures" / "review-exchange" / "scylla-2093-state-carrier.md"

REPOSITORY = "HomericIntelligence/Scylla"
PR_NUMBER = 2093
HEAD_OID = "2adeaf52b67ccd31dbd3f62b60407043057db45f"
OTHER_OID = "b" * 40
ZERO_DIGEST = "0" * 64


def _progress(*, revision: str = HEAD_OID, required_remaining: int = 2) -> list[dict[str, object]]:
    """Build one review round's published progress entry."""
    return [
        {
            "artifact_revision": revision,
            "required_remaining": required_remaining,
            "round": 1,
            "scope": ["AGENTS.md"],
            "scope_size": 1,
        }
    ]


def _state(**overrides: object) -> dict[str, object]:
    """Build a ``state`` object shaped like the real published carrier.

    The shape mirrors the live carrier on Scylla #2093: the "how many required
    findings remain" count is published inside ``progress`` (alongside the reviewed
    revision), not at the top level.
    """
    state: dict[str, object] = {
        "artifact_binding": {
            "revision": HEAD_OID,
            "sha256": "a" * 64,
            "visible_content_sha256": "b" * 64,
        },
        "coverage_complete": True,
        "findings": [
            {"disposition": "required", "id": "F-001", "state": "open"},
            {"disposition": "required", "id": "F-002", "state": "open"},
        ],
        "go_eligible": True,
        "next_action": "author_response",
        "phase": "awaiting_author",
        "progress": _progress(),
        "round": 1,
        "round_limit": 5,
        "surface": "pull_request",
        "target": {
            "number": PR_NUMBER,
            "provider": "github",
            "repository": REPOSITORY,
            "url": f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
        },
        "verdict": VERDICT_NO_GO,
    }
    state.update(overrides)
    return state


def _carrier(
    state: dict[str, object],
    *,
    marker_digest: str | None = None,
    field_digest: str | None = None,
    kind: str = "state",
    prefix: str = "## Review assessment\n\nVerdict: NO-GO\n",
) -> str:
    """Render a review-exchange carrier whose digest can be deliberately broken."""
    computed = canonical_state_digest(state)
    document = {
        "schema_id": "athena.review-exchange.state",
        "schema_version": 1,
        "state": state,
        "state_sha256": computed if field_digest is None else field_digest,
    }
    marker = computed if marker_digest is None else marker_digest
    rendered = json.dumps(document, separators=(",", ":"), sort_keys=True)
    return (
        f"{prefix}\n"
        f"<!-- HomericIntelligence:review-exchange:v1 kind={kind} sha256={marker} -->\n"
        "```json\n"
        f"{rendered}\n"
        "```\n"
    )


def _review(body: str, *, oid: str = HEAD_OID, submitted: str = "2026-09-14T17:07:08Z") -> dict:
    """Build one ``gh pr view --json reviews`` entry."""
    return {
        "author": {"login": "mvillmow"},
        "body": body,
        "commit": {"oid": oid},
        "state": "COMMENTED",
        "submittedAt": submitted,
    }


@pytest.fixture
def mock_gh_call() -> Iterator[MagicMock]:
    """Patch the module's ``gh_call`` boundary."""
    with patch("hephaestus.automation.reconcile_implementation_labels.gh_call") as mocked:
        yield mocked


@pytest.fixture
def mock_edit() -> Iterator[MagicMock]:
    """Patch the module's atomic label-edit boundary."""
    with patch(
        "hephaestus.automation.reconcile_implementation_labels.gh_issue_edit_labels"
    ) as mocked:
        yield mocked


def _ok_proc(stdout: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = stdout
    proc.stderr = ""
    return proc


class TestCanonicalStateDigest:
    """The published digest must be verifiable by recomputation."""

    def test_matches_the_real_published_carrier(self) -> None:
        """A real carrier from Scylla #2093 verifies against its own marker digest."""
        verdict = parse_carrier(
            REAL_CARRIER.read_text(encoding="utf-8"),
            pr_number=PR_NUMBER,
            repository=REPOSITORY,
        )
        assert verdict.verdict == VERDICT_NO_GO
        assert verdict.head_oid == HEAD_OID
        assert verdict.required_remaining == 2

    def test_reads_required_remaining_from_published_progress(self) -> None:
        """The count comes from the newest ``progress`` entry."""
        state = _state(progress=_progress(required_remaining=3))
        verdict = parse_carrier(_carrier(state), pr_number=PR_NUMBER, repository=REPOSITORY)
        assert verdict.required_remaining == 3

    def test_derives_required_remaining_from_open_findings(self) -> None:
        """Without a usable ``progress`` count, open required findings are counted."""
        state = _state(
            progress=[],
            findings=[
                {"disposition": "required", "id": "F-001", "state": "open"},
                {"disposition": "required", "id": "F-002", "state": "closed"},
                {"disposition": "suggestion", "id": "F-003", "state": "open"},
            ],
        )
        verdict = parse_carrier(_carrier(state), pr_number=PR_NUMBER, repository=REPOSITORY)
        assert verdict.required_remaining == 1

    def test_rejects_progress_revision_mismatch(self) -> None:
        """A carrier cannot report progress for a different revision than it binds."""
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        state = _state(progress=_progress(revision=OTHER_OID))
        with pytest.raises(CarrierRejectedError, match="progress revision"):
            parse_carrier(_carrier(state), pr_number=PR_NUMBER, repository=REPOSITORY)

    def test_rejects_carrier_without_any_finding_count(self) -> None:
        """Fail closed when neither ``progress`` nor ``findings`` publishes a count."""
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        state = _state(progress=[], findings=None)
        with pytest.raises(CarrierRejectedError, match="required-finding count"):
            parse_carrier(_carrier(state), pr_number=PR_NUMBER, repository=REPOSITORY)

    def test_locks_the_canonical_encoding(self) -> None:
        """Digest is sha256 over compact, key-sorted JSON — the published scheme."""
        state = {"b": 2, "a": 1}
        assert canonical_state_digest(state) == canonical_state_digest({"a": 1, "b": 2})
        compact = json.dumps(state, separators=(",", ":"), sort_keys=True)
        import hashlib

        assert canonical_state_digest(state) == hashlib.sha256(compact.encode("utf-8")).hexdigest()


class TestParseCarrierRejections:
    """Every malformed, tampered, or off-target carrier fails closed."""

    def test_rejects_body_without_marker(self) -> None:
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        with pytest.raises(CarrierRejectedError):
            parse_carrier("no carrier here", pr_number=PR_NUMBER, repository=REPOSITORY)

    def test_rejects_marker_without_json_block(self) -> None:
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        body = (
            f"<!-- HomericIntelligence:review-exchange:v1 kind=state "
            f"sha256={ZERO_DIGEST} -->\nno fenced block\n"
        )
        with pytest.raises(CarrierRejectedError):
            parse_carrier(body, pr_number=PR_NUMBER, repository=REPOSITORY)

    def test_rejects_tampered_marker_digest(self) -> None:
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        with pytest.raises(CarrierRejectedError):
            parse_carrier(
                _carrier(_state(), marker_digest=ZERO_DIGEST),
                pr_number=PR_NUMBER,
                repository=REPOSITORY,
            )

    def test_rejects_tampered_field_digest(self) -> None:
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        with pytest.raises(CarrierRejectedError):
            parse_carrier(
                _carrier(_state(), field_digest=ZERO_DIGEST),
                pr_number=PR_NUMBER,
                repository=REPOSITORY,
            )

    def test_rejects_tampered_state_with_matching_field(self) -> None:
        """Editing the state without updating either digest is rejected."""
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        state = _state()
        body = _carrier(state)
        tampered = body.replace(f'"revision":"{HEAD_OID}"', f'"revision":"{OTHER_OID}"')
        with pytest.raises(CarrierRejectedError):
            parse_carrier(tampered, pr_number=PR_NUMBER, repository=REPOSITORY)

    def test_rejects_non_state_kind(self) -> None:
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        with pytest.raises(CarrierRejectedError):
            parse_carrier(
                _carrier(_state(), kind="anchors"),
                pr_number=PR_NUMBER,
                repository=REPOSITORY,
            )

    def test_rejects_wrong_target_number(self) -> None:
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        state = _state(target={"number": 1, "repository": REPOSITORY})
        with pytest.raises(CarrierRejectedError):
            parse_carrier(_carrier(state), pr_number=PR_NUMBER, repository=REPOSITORY)

    def test_rejects_wrong_target_repository(self) -> None:
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        state = _state(target={"number": PR_NUMBER, "repository": "Other/Repo"})
        with pytest.raises(CarrierRejectedError):
            parse_carrier(_carrier(state), pr_number=PR_NUMBER, repository=REPOSITORY)

    def test_rejects_non_pull_request_surface(self) -> None:
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        with pytest.raises(CarrierRejectedError):
            parse_carrier(
                _carrier(_state(surface="issue")),
                pr_number=PR_NUMBER,
                repository=REPOSITORY,
            )

    def test_rejects_missing_bound_revision(self) -> None:
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        state = _state(artifact_binding={"sha256": "a" * 64})
        with pytest.raises(CarrierRejectedError):
            parse_carrier(_carrier(state), pr_number=PR_NUMBER, repository=REPOSITORY)

    def test_rejects_unknown_verdict(self) -> None:
        from hephaestus.automation.reconcile_implementation_labels import CarrierRejectedError

        with pytest.raises(CarrierRejectedError):
            parse_carrier(
                _carrier(_state(verdict="MAYBE")),
                pr_number=PR_NUMBER,
                repository=REPOSITORY,
            )


class TestSelectCurrentHeadVerdict:
    """Only a verdict bound to the live head is usable."""

    def test_returns_verdict_bound_to_head(self) -> None:
        reviews = [_review(_carrier(_state()))]
        verdict = select_current_head_verdict(
            reviews, pr_number=PR_NUMBER, repository=REPOSITORY, head_oid=HEAD_OID
        )
        assert verdict is not None
        assert verdict.verdict == VERDICT_NO_GO

    def test_ignores_review_bound_to_another_head(self) -> None:
        reviews = [_review(_carrier(_state()), oid=OTHER_OID)]
        assert (
            select_current_head_verdict(
                reviews,
                pr_number=PR_NUMBER,
                repository=REPOSITORY,
                head_oid=HEAD_OID,
            )
            is None
        )

    def test_ignores_carrier_whose_state_binds_another_revision(self) -> None:
        state = _state(
            artifact_binding={"revision": OTHER_OID, "sha256": "a" * 64},
            progress=_progress(revision=OTHER_OID),
        )
        reviews = [_review(_carrier(state))]
        assert (
            select_current_head_verdict(
                reviews,
                pr_number=PR_NUMBER,
                repository=REPOSITORY,
                head_oid=HEAD_OID,
            )
            is None
        )

    def test_returns_none_without_reviews(self) -> None:
        assert (
            select_current_head_verdict(
                [],
                pr_number=PR_NUMBER,
                repository=REPOSITORY,
                head_oid=HEAD_OID,
            )
            is None
        )

    def test_newest_head_bound_carrier_wins(self) -> None:
        older = _review(
            _carrier(_state(verdict=VERDICT_GO)),
            submitted="2026-09-01T00:00:00Z",
        )
        newer = _review(
            _carrier(_state(verdict=VERDICT_NO_GO)),
            submitted="2026-09-14T17:07:08Z",
        )
        verdict = select_current_head_verdict(
            [older, newer],
            pr_number=PR_NUMBER,
            repository=REPOSITORY,
            head_oid=HEAD_OID,
        )
        assert verdict is not None
        assert verdict.verdict == VERDICT_NO_GO

    def test_ignores_unparsable_carrier(self) -> None:
        reviews = [_review("some unrelated review body")]
        assert (
            select_current_head_verdict(
                reviews,
                pr_number=PR_NUMBER,
                repository=REPOSITORY,
                head_oid=HEAD_OID,
            )
            is None
        )


class TestDecide:
    """The decision is one-way: it may remove eligibility, never grant it."""

    def _verdict(self, **overrides: object) -> PublishedVerdict:
        base: dict[str, object] = {
            "verdict": VERDICT_NO_GO,
            "head_oid": HEAD_OID,
            "pr_number": PR_NUMBER,
            "repository": REPOSITORY,
            "required_remaining": 2,
        }
        base.update(overrides)
        return PublishedVerdict(**base)  # type: ignore[arg-type]

    def test_no_verdict_skips(self) -> None:
        assert decide(labels=[STATE_IMPLEMENTATION_GO], verdict=None).action == "skip"

    def test_stale_go_with_current_head_no_go_reconciles(self) -> None:
        decision = decide(labels=[STATE_IMPLEMENTATION_GO], verdict=self._verdict())
        assert decision.action == "reconcile"

    def test_go_verdict_never_reconciles(self) -> None:
        decision = decide(
            labels=[STATE_IMPLEMENTATION_NO_GO],
            verdict=self._verdict(verdict=VERDICT_GO),
        )
        assert decision.action == "skip"

    def test_no_go_verdict_with_zero_remaining_skips(self) -> None:
        decision = decide(
            labels=[STATE_IMPLEMENTATION_GO],
            verdict=self._verdict(required_remaining=0),
        )
        assert decision.action == "skip"

    def test_already_exclusive_no_go_skips(self) -> None:
        decision = decide(labels=[STATE_IMPLEMENTATION_NO_GO], verdict=self._verdict())
        assert decision.action == "skip"

    def test_unlabelled_pr_skips(self) -> None:
        decision = decide(labels=[], verdict=self._verdict())
        assert decision.action == "skip"


class TestReconcilePr:
    """Correction is one atomic edit followed by exclusive readback."""

    def _snapshot(self, labels: list[str], reviews: list[dict] | None = None) -> MagicMock:
        return _ok_proc(
            stdout=json.dumps(
                {
                    "headRefOid": HEAD_OID,
                    "labels": [{"name": name} for name in labels],
                    "reviews": reviews if reviews is not None else [_review(_carrier(_state()))],
                }
            )
        )

    def test_swaps_go_for_no_go_and_reads_back(
        self, mock_gh_call: MagicMock, mock_edit: MagicMock
    ) -> None:
        mock_gh_call.side_effect = [
            self._snapshot([STATE_IMPLEMENTATION_GO]),
            self._snapshot([STATE_IMPLEMENTATION_NO_GO]),
        ]
        changed = reconcile_pr(REPOSITORY, PR_NUMBER, dry_run=False)
        assert changed is True
        mock_edit.assert_called_once_with(
            PR_NUMBER,
            add=[STATE_IMPLEMENTATION_NO_GO],
            remove=[STATE_IMPLEMENTATION_GO],
            repo=("HomericIntelligence", "Scylla"),
        )

    def test_dry_run_mutates_nothing(self, mock_gh_call: MagicMock, mock_edit: MagicMock) -> None:
        mock_gh_call.side_effect = [self._snapshot([STATE_IMPLEMENTATION_GO])]
        assert reconcile_pr(REPOSITORY, PR_NUMBER, dry_run=True) is False
        mock_edit.assert_not_called()

    def test_failed_readback_raises(self, mock_gh_call: MagicMock, mock_edit: MagicMock) -> None:
        mock_gh_call.side_effect = [
            self._snapshot([STATE_IMPLEMENTATION_GO]),
            # Readback still shows GO plus NO-GO: not exclusive.
            self._snapshot([STATE_IMPLEMENTATION_GO, STATE_IMPLEMENTATION_NO_GO]),
        ]
        with pytest.raises(RuntimeError, match="readback"):
            reconcile_pr(REPOSITORY, PR_NUMBER, dry_run=False)

    def test_go_verdict_does_not_write(self, mock_gh_call: MagicMock, mock_edit: MagicMock) -> None:
        reviews = [_review(_carrier(_state(verdict=VERDICT_GO)))]
        mock_gh_call.side_effect = [self._snapshot([STATE_IMPLEMENTATION_NO_GO], reviews)]
        assert reconcile_pr(REPOSITORY, PR_NUMBER, dry_run=False) is False
        mock_edit.assert_not_called()

    def test_absent_verdict_does_not_write(
        self, mock_gh_call: MagicMock, mock_edit: MagicMock
    ) -> None:
        mock_gh_call.side_effect = [self._snapshot([STATE_IMPLEMENTATION_GO], [])]
        assert reconcile_pr(REPOSITORY, PR_NUMBER, dry_run=False) is False
        mock_edit.assert_not_called()

    def test_never_adds_go_label(self, mock_gh_call: MagicMock, mock_edit: MagicMock) -> None:
        """Structural guard: the pass can only ever add the NO-GO label."""
        mock_gh_call.side_effect = [
            self._snapshot([STATE_IMPLEMENTATION_GO]),
            self._snapshot([STATE_IMPLEMENTATION_NO_GO]),
        ]
        reconcile_pr(REPOSITORY, PR_NUMBER, dry_run=False)
        for call in mock_edit.call_args_list:
            assert call.kwargs["add"] == [STATE_IMPLEMENTATION_NO_GO]
            assert STATE_IMPLEMENTATION_GO not in call.kwargs["add"]


class TestFetchPrSnapshot:
    """The snapshot is read repo-scoped, never from ambient cwd."""

    def test_passes_repo_scope(self, mock_gh_call: MagicMock) -> None:
        mock_gh_call.return_value = _ok_proc(
            stdout=json.dumps({"headRefOid": HEAD_OID, "labels": [], "reviews": []})
        )
        snapshot = fetch_pr_snapshot(REPOSITORY, PR_NUMBER)
        argv = mock_gh_call.call_args[0][0]
        assert "--repo" in argv
        assert REPOSITORY in argv
        assert snapshot["head_oid"] == HEAD_OID

    def test_failure_propagates(self, mock_gh_call: MagicMock) -> None:
        mock_gh_call.side_effect = subprocess.CalledProcessError(1, ["gh"])
        with pytest.raises(subprocess.CalledProcessError):
            fetch_pr_snapshot(REPOSITORY, PR_NUMBER)


class TestMain:
    """CLI smoke tests."""

    def test_repo_and_pr_dry_run(self, mock_gh_call: MagicMock, mock_edit: MagicMock) -> None:
        mock_gh_call.side_effect = [
            _ok_proc(
                stdout=json.dumps(
                    {
                        "headRefOid": HEAD_OID,
                        "labels": [{"name": STATE_IMPLEMENTATION_GO}],
                        "reviews": [_review(_carrier(_state()))],
                    }
                )
            )
        ]
        rc = main(["--repo", REPOSITORY, "--pr", str(PR_NUMBER), "--dry-run"])
        assert rc == 0
        mock_edit.assert_not_called()

    def test_org_enumerates_open_prs(self, mock_gh_call: MagicMock, mock_edit: MagicMock) -> None:
        mock_gh_call.side_effect = [
            _ok_proc(
                stdout=json.dumps([{"name": "OneRepo", "isArchived": False, "isFork": False}])
            ),
            _ok_proc(stdout=json.dumps([{"number": 7}])),
            _ok_proc(
                stdout=json.dumps(
                    {
                        "headRefOid": HEAD_OID,
                        "labels": [{"name": STATE_IMPLEMENTATION_GO}],
                        "reviews": [
                            _review(
                                _carrier(
                                    _state(target={"number": 7, "repository": "AnOrg/OneRepo"})
                                )
                            )
                        ],
                    }
                )
            ),
            _ok_proc(
                stdout=json.dumps(
                    {
                        "headRefOid": HEAD_OID,
                        "labels": [{"name": STATE_IMPLEMENTATION_NO_GO}],
                        "reviews": [],
                    }
                )
            ),
        ]
        rc = main(["--org", "AnOrg"])
        assert rc == 0
        assert mock_edit.call_count == 1
