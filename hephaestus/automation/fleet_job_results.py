"""Retain private results for one explicitly associated Fleet turn."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from hephaestus.automation.fleet_journal import WorkerJournal

SCHEMA = "hi/fleet/job/v1"
MAX_ANSWER_BYTES = 64 * 1024
MAX_RECORD_BYTES = 768 * 1024
_SELECTOR = {"operation", "schema", "jobId", "targetId", "generation", "bindingDigest"}
_ASSOCIATION = _SELECTOR | {"inputCommandId", "inputIdempotencyKey", "inputSha256"}
_OWNER = [
    "workerId",
    "poolId",
    "hostId",
    "allocationId",
    "generation",
    "sessionId",
    "taskId",
    "executionId",
    "agentId",
    "providerThreadId",
    "workspace",
    "stage",
]


def encoded(value: Any) -> bytes:
    """Use the journal and socket's JSON escaping when checking byte limits."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    """Bind the complete encoded value."""
    return hashlib.sha256(encoded(value)).hexdigest()


def validate_request(message: dict[str, Any], operation: str) -> None:
    """Check the private request before using its identity fields."""
    fields = _ASSOCIATION if operation == "associate-job" else _SELECTOR
    if set(message) != fields or message["operation"] != operation or message["schema"] != SCHEMA:
        raise ValueError("invalid_job_request")
    if type(message["generation"]) is not int or message["generation"] < 1:
        raise ValueError("invalid_job_generation")
    for key in fields - {"generation"}:
        value = message[key]
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise ValueError("invalid_job_identity")
    for key in fields & {"bindingDigest", "inputSha256"}:
        if re.fullmatch(r"[0-9a-f]{64}", message[key]) is None:
            raise ValueError("invalid_job_digest")


def owner_identity(session: dict[str, Any]) -> dict[str, Any]:
    """Copy existing ownership facts without granting source or task authority."""
    return {key: session.get(key) for key in _OWNER}


class FleetJobResults:
    """Use the worker's single journal writer for private turn correlation."""

    def __init__(self, journal: WorkerJournal) -> None:
        """Keep terminal evidence and identify unfinished work from an earlier process."""
        self.journal = journal
        self._recovered = set(journal.jobs)

    def for_session(self, session_id: str) -> dict[str, Any] | None:
        """Return the association that reserves this session's next turn."""
        return next(
            (job for job in self.journal.jobs.values() if job["owner"]["sessionId"] == session_id),
            None,
        )

    def _store(self, job: dict[str, Any]) -> None:
        if len(encoded({"kind": "job", "value": job})) > MAX_RECORD_BYTES:
            raise ValueError("job_record_limit")
        self.journal.append("job", job)

    def associate(
        self, message: dict[str, Any], session: dict[str, Any], lease: dict[str, Any]
    ) -> dict[str, Any]:
        """Record an existing session and future input, without executing either."""
        validate_request(message, "associate-job")
        if message["generation"] != session["generation"]:
            raise ValueError("job_owner_mismatch")
        prior = self.journal.jobs.get(message["jobId"])
        if prior is not None:
            if (
                prior["association"] != message
                or prior["owner"] != owner_identity(session)
                or prior["lease"] != lease
            ):
                raise ValueError("job_association_conflict")
        else:
            if (
                self.for_session(session["sessionId"]) is not None
                or session["released"]
                or not session["admissionReserved"]
                or session["activity"] != "idle"
                or message["inputCommandId"] in self.journal.command_ids
                or message["inputIdempotencyKey"] in self.journal.commands
            ):
                raise ValueError("job_session_not_ready")
            self._store(
                {
                    "jobId": message["jobId"],
                    "association": message,
                    "owner": owner_identity(session),
                    "lease": lease,
                    "phase": "associated",
                    "providerTurnId": None,
                    "answer": None,
                    "result": None,
                }
            )
        return {"schema": SCHEMA, "jobId": message["jobId"], "status": "associated"}

    def before_input(self, command: dict[str, Any], session: dict[str, Any]) -> None:
        """Persist the effect boundary before a matching admitted turn starts."""
        job = self.for_session(session["sessionId"])
        if job is None:
            return
        association = job["association"]
        if (
            job["jobId"] in self._recovered
            or job["phase"] != "associated"
            or job["owner"] != owner_identity(session)
            or session["activity"] != "idle"
            or command["commandId"] != association["inputCommandId"]
            or command["idempotencyKey"] != association["inputIdempotencyKey"]
            or hashlib.sha256(command["payload"]["text"].encode()).hexdigest()
            != association["inputSha256"]
        ):
            raise ValueError("job_input_mismatch")
        self._store({**job, "phase": "dispatching"})

    def started(self, session: dict[str, Any]) -> None:
        """Bind the returned provider turn after the input response."""
        job = self.for_session(session["sessionId"])
        if job is not None:
            self._store({**job, "phase": "running", "providerTurnId": session["providerTurnId"]})

    def capture(self, session: dict[str, Any], params: dict[str, Any]) -> str | None:
        """Retain a final item and report a conflict with a completed result."""
        job = self.for_session(session["sessionId"])
        item = params.get("item", {})
        if (
            job is None
            or (job["phase"] != "running" and job["result"] is None)
            or params.get("threadId") != job["owner"]["providerThreadId"]
            or params.get("turnId") != job["providerTurnId"]
            or item.get("type") != "agentMessage"
            or item.get("phase") != "final_answer"
            or item.get("delivery") is not None
        ):
            return None
        text, item_id = item.get("text"), item.get("id")
        try:
            valid = (
                isinstance(text, str)
                and bool(text)
                and len(text.encode()) <= MAX_ANSWER_BYTES
                and isinstance(item_id, str)
                and 0 < len(item_id) <= 1024
            )
        except UnicodeError:
            valid = False
        if not valid:
            self.unknown(job, "job_answer_invalid")
            return "job_answer_invalid" if job["result"] is not None else None
        answer = {
            "itemId": item_id,
            "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        if job["result"] is not None:
            if job["result"]["answer"] != answer:
                self.unknown(job, "job_answer_conflict")
                return "job_answer_conflict"
            return None
        if job["answer"] is not None and job["answer"] != answer:
            self._store({**job, "phase": "unknown", "error": "job_answer_conflict"})
        elif job["answer"] is None:
            self._store({**job, "answer": answer})
        return None

    def unknown(self, job: dict[str, Any], reason: str) -> None:
        """Retain unresolved work without giving permission to retry it."""
        if job["phase"] != "unknown" or job.get("error") != reason:
            self._store({**job, "phase": "unknown", "error": reason})

    def complete(self, job: dict[str, Any], disposal: dict[str, Any], turn: dict[str, Any]) -> None:
        """Persist the observed outcome after causal disposal."""
        association = job["association"]
        receipt = self.journal.commands.get(association["inputIdempotencyKey"], {}).get("result")
        if (
            not isinstance(receipt, dict)
            or receipt.get("status") != "completed"
            or receipt.get("commandId") != association["inputCommandId"]
            or receipt.get("receipt", {}).get("providerTurnId") != job["providerTurnId"]
        ):
            self.unknown(job, "job_input_unconfirmed")
            return
        outcome, error = turn["status"], turn.get("error")
        if job["phase"] != "running" or (outcome == "completed" and job["answer"] is None):
            self.unknown(job, job.get("error", "job_answer_missing"))
            return
        if outcome == "failed" and (
            not isinstance(error, dict)
            or not isinstance(error.get("message"), str)
            or not error["message"]
            or len(encoded(error)) > MAX_ANSWER_BYTES
        ):
            self.unknown(job, "job_error_invalid")
            return
        result = {
            "schema": "hi/fleet/job-result/v1",
            "jobId": job["jobId"],
            "bindingDigest": association["bindingDigest"],
            "owner": job["owner"],
            "input": {
                "commandId": association["inputCommandId"],
                "idempotencyKey": association["inputIdempotencyKey"],
                "sha256": association["inputSha256"],
            },
            "providerTurnId": job["providerTurnId"],
            "outcome": outcome,
            "answer": job["answer"],
            "error": error,
            "disposal": disposal,
        }
        self._store(
            {
                **job,
                "phase": outcome,
                "answer": None,
                "terminalSha256": digest(turn),
                "result": {**result, "sha256": digest(result)},
            }
        )

    def read(self, message: dict[str, Any]) -> dict[str, Any]:
        """Read only the exact job, session, generation, and bound reference."""
        validate_request(message, "job-result")
        job = self.journal.jobs.get(message["jobId"])
        if job is None or any(
            message[key] != job["association"][key]
            for key in ("targetId", "generation", "bindingDigest")
        ):
            raise ValueError("job_not_found")
        status = job["phase"]
        if status in {"associated", "running"}:
            status = "unknown" if job["jobId"] in self._recovered else "pending"
        elif status == "dispatching":
            status = "unknown"
        response: dict[str, Any] = json.loads(
            encoded(
                {
                    "schema": SCHEMA,
                    "jobId": job["jobId"],
                    "status": status,
                    "result": job["result"] if status in {"completed", "failed"} else None,
                }
            )
        )
        return response
