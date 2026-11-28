"""Immutable learning intents shared by the queue and the learning host."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any


class LearningIntentKind(StrEnum):
    """Supported auxiliary learning sources."""

    APPROVED_PLAN = "approved_plan"
    POST_MERGE = "post_merge"


@dataclass(frozen=True)
class LearningIntent:
    """Immutable identity and bounded input for one learning operation."""

    kind: LearningIntentKind
    repo: str
    issue: int
    pr: int | None = None
    plan_revision: int | None = None
    plan_fingerprint: str = ""
    identity_repo: str = ""

    @property
    def key(self) -> str:
        """Return a deterministic identity key without persisting content."""
        identity = {
            "issue": self.issue,
            "kind": self.kind.value,
            "plan_fingerprint": self.plan_fingerprint,
            "plan_revision": self.plan_revision,
            "pr": self.pr,
            "repo": self.identity_repo or self.repo,
        }
        digest = sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        return f"{self.kind.value}:{digest}"

    def journal_identity(self) -> dict[str, object]:
        """Return the bounded identity fields needed for restart recovery."""
        return {
            "repo": self.identity_repo or self.repo,
            "issue": self.issue,
            "pr": self.pr,
            "plan_revision": self.plan_revision,
            "plan_fingerprint": self.plan_fingerprint,
        }

    def to_payload(self, *, owner: str | None = None) -> dict[str, object]:
        """Return the closed semantic request accepted by the learning host.

        Args:
            owner: Trusted organization that qualifies a short internal
                repository name at the external learning boundary.

        Raises:
            ValueError: If the repository identity is malformed or belongs to
                a different owner.

        """
        repository = self.repo
        identity_repo = self.identity_repo
        if owner is not None:
            component = r"[A-Za-z0-9_.-]+"
            if re.fullmatch(component, owner) is None:
                raise ValueError("learning repository identity is invalid")
            parts = repository.split("/")
            if len(parts) == 1 and re.fullmatch(component, repository):
                if identity_repo and identity_repo != repository:
                    raise ValueError("learning repository identity is foreign or invalid")
                identity_repo = repository
                repository = f"{owner}/{repository}"
            elif (
                len(parts) != 2
                or parts[0] != owner
                or not all(re.fullmatch(component, part) for part in parts)
            ):
                raise ValueError("learning repository identity is foreign or invalid")
            if identity_repo and (
                re.fullmatch(component, identity_repo) is None
                or repository.rsplit("/", 1)[-1] != identity_repo
            ):
                raise ValueError("learning repository identity is foreign or invalid")
            if len(repository) > 200:
                raise ValueError("learning repository identity is invalid")
        payload: dict[str, object] = {
            "kind": self.kind.value,
            "repo": repository,
            "issue": self.issue,
            "intent_key": self.key,
        }
        if identity_repo:
            payload["identity_repo"] = identity_repo
        if self.kind is LearningIntentKind.APPROVED_PLAN:
            payload.update(
                {
                    "plan_revision": self.plan_revision,
                    "plan_fingerprint": self.plan_fingerprint,
                }
            )
        else:
            payload["pr"] = self.pr
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> LearningIntent:
        """Parse a complete learning intent without accepting extra fields."""
        kind_raw = payload.get("kind")
        try:
            kind = LearningIntentKind(kind_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("learning intent payload has invalid kind") from exc
        common = {"kind", "repo", "issue", "intent_key"}
        expected = common | (
            {"plan_revision", "plan_fingerprint"}
            if kind is LearningIntentKind.APPROVED_PLAN
            else {"pr"}
        )
        if set(payload) not in {frozenset(expected), frozenset(expected | {"identity_repo"})}:
            raise ValueError("learning intent payload has unsupported or missing fields")
        repo = payload.get("repo")
        identity_repo = payload.get("identity_repo", "")
        issue = payload.get("issue")
        intent_key = payload.get("intent_key")
        if (
            not isinstance(repo, str)
            or len(repo) > 200
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None
            or not isinstance(identity_repo, str)
            or (
                bool(identity_repo)
                and (
                    re.fullmatch(r"[A-Za-z0-9_.-]+", identity_repo) is None
                    or repo.rsplit("/", 1)[-1] != identity_repo
                )
            )
            or isinstance(issue, bool)
            or not isinstance(issue, int)
            or issue <= 0
            or not isinstance(intent_key, str)
            or not intent_key
        ):
            raise ValueError("learning intent payload has invalid identity fields")
        if kind is LearningIntentKind.APPROVED_PLAN:
            revision = payload.get("plan_revision")
            fingerprint = payload.get("plan_fingerprint")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision <= 0
                or not isinstance(fingerprint, str)
                or not fingerprint
            ):
                raise ValueError("learning intent payload has invalid approved-plan fields")
            intent = cls(
                kind=kind,
                repo=repo,
                issue=issue,
                plan_revision=revision,
                plan_fingerprint=fingerprint,
                identity_repo=identity_repo,
            )
        else:
            pr = payload.get("pr")
            if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
                raise ValueError("learning intent payload has invalid post-merge fields")
            intent = cls(
                kind=kind,
                repo=repo,
                issue=issue,
                pr=pr,
                identity_repo=identity_repo,
            )
        if intent.key != intent_key:
            raise ValueError("learning intent payload identity does not match its key")
        return intent

    @classmethod
    def from_journal(cls, record: dict[str, Any]) -> LearningIntent:
        """Rebuild an intent from a validated journal record."""
        intent = cls(
            kind=LearningIntentKind(str(record["kind"])),
            repo=str(record["repo"]),
            issue=int(record["issue"]),
            pr=int(record["pr"]) if record.get("pr") is not None else None,
            plan_revision=(
                int(record["plan_revision"]) if record.get("plan_revision") is not None else None
            ),
            plan_fingerprint=str(record.get("plan_fingerprint", "")),
        )
        if intent.key != record.get("key"):
            raise ValueError("learning journal identity does not match its key")
        return intent

    @classmethod
    def approved_plan(
        cls,
        *,
        repo: str,
        issue: int,
        plan_revision: int,
        plan_fingerprint: str,
    ) -> LearningIntent:
        """Build an approved-plan learning identity."""
        return cls(
            kind=LearningIntentKind.APPROVED_PLAN,
            repo=repo,
            issue=issue,
            plan_revision=plan_revision,
            plan_fingerprint=plan_fingerprint,
        )

    @classmethod
    def post_merge(cls, *, repo: str, issue: int, pr: int) -> LearningIntent:
        """Build a post-merge learning identity."""
        return cls(kind=LearningIntentKind.POST_MERGE, repo=repo, issue=issue, pr=pr)
