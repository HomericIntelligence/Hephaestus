"""Typed, fail-closed execution for automation GraphQL operations.

The automation product layer is the only place that needs GraphQL.  Keeping
transport, envelope validation, operation validation, and mutation intent in
this module makes it impossible for a helper to turn an unreadable response
into an empty successful result or to replay a mutation whose dispatch is
uncertain.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar, cast, overload

from hephaestus.github.client import (
    _GH_BREAKER as _GH_BREAKER,
    _GH_THROTTLE as _GH_THROTTLE,
    ClaudeUsageCapError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    gh_call as _raw_gh_call,
    gh_cli_timeout,
)
from hephaestus.github.rate_limit import detect_rate_limit, detect_secondary_rate_limit

T = TypeVar("T")
GraphQLScalar = int | str
GraphQLTransport = Callable[..., subprocess.CompletedProcess[str]]


class GraphQLResponseError(RuntimeError):
    """Base class for a response that cannot be consumed safely."""


class GraphQLDeterministicError(GraphQLResponseError):
    """A response or request failure that cannot succeed by repeating it."""


class GraphQLRetryableError(GraphQLResponseError):
    """A read failure that is safe to retry, or a pre-dispatch rejection."""

    def __init__(
        self,
        message: str,
        *,
        pre_dispatch: bool = False,
        reset_epoch: int | None = None,
    ) -> None:
        """Store retry metadata without performing a retry."""
        super().__init__(message)
        self.pre_dispatch = pre_dispatch
        self.reset_epoch = reset_epoch


@dataclass(frozen=True)
class GraphQLMutationIntent:
    """Sanitized proof of one mutation attempt.

    Mutable values never appear in this object.  Their hashes let logs and
    callers correlate an attempt without retaining a comment or review body.
    """

    operation: str
    client_mutation_id: str
    targets: tuple[tuple[str, str], ...]
    content_hashes: tuple[tuple[str, str], ...]

    def safe_summary(self) -> str:
        """Return a log-safe description that excludes mutable content."""
        return (
            f"operation={self.operation} correlation={self.client_mutation_id} "
            f"targets={dict(self.targets)!r} content_hashes={dict(self.content_hashes)!r}"
        )


class GraphQLMutationOutcomeUnknownError(GraphQLResponseError):
    """A mutation may have reached GitHub, so it must not be replayed."""

    def __init__(self, message: str, *, intent: GraphQLMutationIntent) -> None:
        """Store the sanitized intent that must not be replayed."""
        super().__init__(message)
        self.intent = intent


class ReviewCommentNotEditableError(GraphQLMutationOutcomeUnknownError):
    """The one mutation rejection that permits a shadow comment fallback."""


@dataclass(frozen=True)
class GraphQLQuerySpec[T]:
    """A named, validated GraphQL query operation."""

    operation: str
    query: str
    validate: Callable[[dict[str, Any]], T]

    def __post_init__(self) -> None:
        """Reject a spec whose document is not a query."""
        _require_operation_kind(self.query, "query")

    def __contains__(self, value: str) -> bool:
        """Allow legacy test doubles to inspect the named document text."""
        return value in self.query


@dataclass(frozen=True)
class GraphQLMutationSpec[T]:
    """A factory-created mutation with executor-owned variables."""

    operation: str
    query: str
    variables: Mapping[str, GraphQLScalar]
    target_fields: tuple[str, ...]
    content_fields: tuple[str, ...]
    validate: Callable[[dict[str, Any], GraphQLMutationIntent], T]

    def __post_init__(self) -> None:
        """Reject malformed mutation specs before they can be dispatched."""
        _require_operation_kind(self.query, "mutation")
        if "clientMutationId" in self.variables:
            raise ValueError("clientMutationId is executor-owned")
        if "$clientMutationId" not in self.query:
            raise ValueError("mutation must declare executor-owned clientMutationId")
        fields = set(self.variables)
        if not set(self.target_fields).issubset(fields):
            raise ValueError("mutation target fields must be bound variables")
        if not set(self.content_fields).issubset(fields):
            raise ValueError("mutation content fields must be bound variables")

    def __contains__(self, value: str) -> bool:
        """Allow legacy test doubles to inspect the named document text."""
        return value in self.query


@dataclass(frozen=True)
class _PreparedGraphQLMutation[T]:
    spec: GraphQLMutationSpec[T]
    intent: GraphQLMutationIntent
    variables: Mapping[str, GraphQLScalar]


_OPERATION_RE = re.compile(r"^\s*(query|mutation)\b", re.IGNORECASE)
_DETERMINISTIC_RE = re.compile(
    r"HTTP\s+(?:400|401|403|404|422)\b|"
    r"\b(?:unauthorized|forbidden)\b|"
    r"resource not accessible by|"
    r"could not resolve to (?:an? )?(?:issue|pull ?request)\b|"
    r"expected value|unknown_char|parse error|doesn't accept argument|"
    r"is declared .* but not used",
    re.IGNORECASE | re.DOTALL,
)


def _require_operation_kind(document: str, expected: str) -> None:
    """Require the document's root operation to be *expected*."""
    match = _OPERATION_RE.match(document)
    if match is None or match.group(1).lower() != expected:
        raise ValueError(f"root operation must be {expected}")


def _is_graphql_argv(args: list[str]) -> bool:
    """Return whether ``gh`` arguments select the GraphQL API.

    The endpoint may be written as ``graphql`` or ``/graphql`` and options may
    appear between ``api`` and the endpoint, so checking only ``args[1]`` is
    insufficient.
    """
    try:
        api_index = args.index("api")
    except ValueError:
        return False
    return any(
        token.lstrip("/").casefold() == "graphql"
        for token in args[api_index + 1 :]
        if not token.startswith("-") or token in {"--method", "-X"}
    )


def gh_call(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a non-GraphQL automation ``gh`` command through the library client."""
    internal_graphql = bool(kwargs.pop("_graphql_internal", False))
    if _is_graphql_argv(args) and not internal_graphql:
        raise RuntimeError("automation GraphQL calls must use run_graphql()")
    return _raw_gh_call(args, **kwargs)


def _execute_graphql(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Dispatch through the package seam while retaining the raw boundary.

    The package-level ``_gh_call`` name is an established test seam.  Looking
    it up at dispatch time keeps existing callers patchable while the default
    binding remains the guarded façade in this module.  The private marker is
    consumed by that façade and is never forwarded to the client executor.
    """
    package = sys.modules.get("hephaestus.automation.github_api")
    transport = getattr(package, "_gh_call", gh_call)
    return transport(argv, _graphql_internal=True, **kwargs)


def _graphql_argv(query: str, variables: Mapping[str, GraphQLScalar]) -> list[str]:
    """Build one safe ``gh api graphql`` invocation."""
    argv = ["api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        option = "-F" if isinstance(value, int) and not isinstance(value, bool) else "-f"
        argv.extend([option, f"{name}={value}"])
    return argv


def _prepare[T](
    spec: GraphQLQuerySpec[T] | GraphQLMutationSpec[T],
) -> tuple[GraphQLQuerySpec[T], None] | _PreparedGraphQLMutation[T]:
    if isinstance(spec, GraphQLQuerySpec):
        return spec, None
    correlation = uuid.uuid4().hex
    targets = tuple((field, str(spec.variables[field])) for field in spec.target_fields)
    content_hashes = tuple(
        (
            field,
            hashlib.sha256(str(spec.variables[field]).encode("utf-8")).hexdigest(),
        )
        for field in spec.content_fields
    )
    intent = GraphQLMutationIntent(spec.operation, correlation, targets, content_hashes)
    variables = {**spec.variables, "clientMutationId": correlation}
    return _PreparedGraphQLMutation(spec, intent, variables)


def _output_from_exception(error: BaseException) -> str:
    """Combine text carried by a process exception without exposing it in logs."""
    values = [str(error)]
    for name in ("stdout", "stderr", "output"):
        value = getattr(error, name, None)
        if isinstance(value, str):
            values.append(value)
    return "\n".join(values)


def _rate_limit_evidence(text: str) -> int | None:
    """Return primary reset metadata or zero for secondary/unknown limits."""
    reset = detect_rate_limit(text)
    if reset is not None:
        return reset
    if detect_secondary_rate_limit(text):
        return 0
    return None


def _mutation_unknown[T](
    message: str, prepared: _PreparedGraphQLMutation[T]
) -> GraphQLMutationOutcomeUnknownError:
    return GraphQLMutationOutcomeUnknownError(message, intent=prepared.intent)


def _require_prepared[T](
    prepared: _PreparedGraphQLMutation[T] | None,
) -> _PreparedGraphQLMutation[T]:
    """Return the mutation context required by a post-preparation branch."""
    if prepared is None:
        raise RuntimeError("mutation classification was attempted before preparation")
    return prepared


def _classify_transport_error[T](  # noqa: C901
    spec: GraphQLQuerySpec[T] | GraphQLMutationSpec[T],
    prepared: _PreparedGraphQLMutation[T] | None,
    error: BaseException,
) -> GraphQLResponseError:
    """Classify an exception before response-envelope validation."""
    text = _output_from_exception(error)
    if isinstance(error, GitHubUnavailableError):
        if isinstance(spec, GraphQLQuerySpec):
            return GraphQLRetryableError(str(error), pre_dispatch=True)
        prepared = _require_prepared(prepared)
        return GraphQLRetryableError(str(error), pre_dispatch=True)
    if isinstance(error, (FileNotFoundError, PermissionError)):
        return GraphQLDeterministicError(str(error))
    if isinstance(error, GitHubRateLimitError):
        if isinstance(spec, GraphQLQuerySpec):
            return GraphQLRetryableError(str(error), reset_epoch=error.reset_epoch)
        prepared = _require_prepared(prepared)
        return _mutation_unknown(str(error), prepared)
    if isinstance(error, subprocess.CalledProcessError):
        reset = _rate_limit_evidence(text)
        if reset is not None:
            if isinstance(spec, GraphQLQuerySpec):
                return GraphQLRetryableError(text, reset_epoch=reset)
            prepared = _require_prepared(prepared)
            return _mutation_unknown(text, prepared)
        if _is_sole_not_editable_exception(error) and isinstance(spec, GraphQLMutationSpec):
            prepared = _require_prepared(prepared)
            return ReviewCommentNotEditableError(text, intent=prepared.intent)
        if _DETERMINISTIC_RE.search(text):
            if isinstance(spec, GraphQLQuerySpec):
                return GraphQLDeterministicError(text)
            prepared = _require_prepared(prepared)
            return _mutation_unknown(text, prepared)
        if isinstance(spec, GraphQLQuerySpec):
            return GraphQLRetryableError(text)
        prepared = _require_prepared(prepared)
        return _mutation_unknown(text, prepared)
    if isinstance(
        error,
        (
            subprocess.TimeoutExpired,
            BrokenPipeError,
            ConnectionError,
            OSError,
            subprocess.SubprocessError,
        ),
    ):
        if isinstance(spec, GraphQLQuerySpec):
            return GraphQLRetryableError(text)
        prepared = _require_prepared(prepared)
        return _mutation_unknown(text, prepared)
    raise error


def _is_not_editable(text: str) -> bool:
    return text.strip().casefold() == "body is not editable"


def _is_sole_not_editable_exception(error: BaseException) -> bool:
    """Recognize the exact standalone process error without mixing streams."""
    for name in ("stderr", "stdout", "output"):
        value = getattr(error, name, None)
        if isinstance(value, str) and _is_not_editable(value):
            return True
    return _is_not_editable(str(error))


def _classify_status[T](
    spec: GraphQLQuerySpec[T] | GraphQLMutationSpec[T],
    prepared: _PreparedGraphQLMutation[T] | None,
    result: subprocess.CompletedProcess[str],
) -> None:
    """Raise the appropriate typed error for a nonzero process result."""
    text = "\n".join(value for value in (result.stdout, result.stderr) if isinstance(value, str))
    reset = _rate_limit_evidence(text)
    if reset is not None:
        if isinstance(spec, GraphQLQuerySpec):
            raise GraphQLRetryableError(text, reset_epoch=reset)
        prepared = _require_prepared(prepared)
        raise _mutation_unknown(text, prepared)
    if isinstance(spec, GraphQLMutationSpec) and (
        _is_not_editable(result.stderr or "") or _is_not_editable(result.stdout or "")
    ):
        prepared = _require_prepared(prepared)
        raise ReviewCommentNotEditableError(text, intent=prepared.intent)
    if _DETERMINISTIC_RE.search(text):
        if isinstance(spec, GraphQLQuerySpec):
            raise GraphQLDeterministicError(text)
        prepared = _require_prepared(prepared)
        raise _mutation_unknown(text, prepared)
    if isinstance(spec, GraphQLQuerySpec):
        raise GraphQLRetryableError(text)
    prepared = _require_prepared(prepared)
    raise _mutation_unknown(text, prepared)


def _parse_envelope[T](  # noqa: C901
    spec: GraphQLQuerySpec[T] | GraphQLMutationSpec[T],
    prepared: _PreparedGraphQLMutation[T] | None,
    stdout: str,
) -> dict[str, Any]:
    """Parse and classify the common GraphQL JSON envelope."""
    if not stdout or not stdout.strip():
        raise (
            GraphQLDeterministicError("GraphQL response was empty")
            if prepared is None
            else _mutation_unknown("GraphQL response was empty", prepared)
        )
    try:
        envelope = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as error:
        if prepared is None:
            raise GraphQLDeterministicError("GraphQL response was not valid JSON") from error
        raise _mutation_unknown("GraphQL response was not valid JSON", prepared) from error
    if not isinstance(envelope, dict):
        if prepared is None:
            raise GraphQLDeterministicError("GraphQL response envelope was not an object")
        raise _mutation_unknown("GraphQL response envelope was not an object", prepared)

    if "errors" in envelope:
        errors = envelope["errors"]
        valid_errors = (
            isinstance(errors, list)
            and bool(errors)
            and all(
                isinstance(error, dict)
                and isinstance(error.get("message"), str)
                and bool(error["message"].strip())
                for error in errors
            )
        )
        if not valid_errors:
            message = "GraphQL errors member was malformed"
            if prepared is None:
                raise GraphQLDeterministicError(message)
            raise _mutation_unknown(message, prepared)
        messages = [str(error["message"]) for error in errors]
        all_rate_limited = all(
            str(error.get("type", "")).upper() == "RATE_LIMITED"
            or _rate_limit_evidence(str(error["message"])) is not None
            for error in errors
        )
        if all_rate_limited:
            reset_values = [_rate_limit_evidence(message) for message in messages]
            reset = next((value for value in reset_values if value is not None), None)
            if prepared is None:
                raise GraphQLRetryableError("; ".join(messages), reset_epoch=reset)
            raise _mutation_unknown("; ".join(messages), prepared)
        if len(errors) == 1 and _is_not_editable(messages[0]) and prepared is not None:
            raise ReviewCommentNotEditableError(messages[0], intent=prepared.intent)
        message = "; ".join(messages)
        if prepared is None:
            raise GraphQLDeterministicError(message)
        raise _mutation_unknown(message, prepared)

    data = envelope.get("data")
    if not isinstance(data, dict):
        message = "GraphQL response data was missing or not an object"
        if prepared is None:
            raise GraphQLDeterministicError(message)
        raise _mutation_unknown(message, prepared)
    return data


def _validate_result[T](
    spec: GraphQLQuerySpec[T] | GraphQLMutationSpec[T],
    prepared: _PreparedGraphQLMutation[T] | None,
    result: subprocess.CompletedProcess[str],
) -> T:
    """Apply envelope and operation validation, normalizing ordinary failures."""
    try:
        data = _parse_envelope(spec, prepared, result.stdout or "")
        if isinstance(spec, GraphQLQuerySpec):
            return spec.validate(data)
        prepared = _require_prepared(prepared)
        return spec.validate(data, prepared.intent)
    except GraphQLResponseError:
        raise
    except Exception as error:
        message = f"GraphQL {spec.operation} payload validation failed: {error}"
        if prepared is None:
            raise GraphQLDeterministicError(message) from error
        raise _mutation_unknown(message, prepared) from error


@overload
def run_graphql[T](
    spec: GraphQLQuerySpec[T],
    variables: Mapping[str, GraphQLScalar],
    *,
    call: GraphQLTransport | None = None,
) -> T: ...


@overload
def run_graphql[T](
    spec: GraphQLMutationSpec[T],
    variables: None = None,
    *,
    call: GraphQLTransport | None = None,
) -> T: ...


def run_graphql[T](
    spec: GraphQLQuerySpec[T] | GraphQLMutationSpec[T],
    variables: Mapping[str, GraphQLScalar] | None = None,
    *,
    call: GraphQLTransport | None = None,
) -> T:
    """Execute exactly one typed GraphQL operation and validate its result."""
    if isinstance(spec, GraphQLQuerySpec):
        if variables is None:
            raise TypeError("query variables are required")
        prepared: _PreparedGraphQLMutation[T] | None = None
        request_variables = variables
    else:
        if variables is not None:
            raise TypeError("mutation variables are owned by the mutation spec")
        prepared_result = _prepare(spec)
        if not isinstance(prepared_result, _PreparedGraphQLMutation):
            raise TypeError("mutation preparation failed")
        prepared = prepared_result
        request_variables = prepared.variables
    try:
        transport_args = _graphql_argv(spec.query, request_variables)
        transport_kwargs = {
            "check": False,
            "retry_on_rate_limit": False,
            "max_retries": 1,
            "log_on_error": False,
            "throttle": False,
        }
        if call is None:
            result = _execute_graphql(transport_args, **transport_kwargs)
        elif call is gh_call:
            result = call(
                transport_args,
                _graphql_internal=True,
                **transport_kwargs,
            )
        else:
            # An explicit transport seam is already the raw one-attempt
            # executor. Keep its call signature exact so tests and adapters
            # can assert the no-retry contract without façade-only metadata.
            result = call(transport_args, **transport_kwargs)
    except BaseException as error:
        classified = _classify_transport_error(spec, prepared, error)
        raise classified from error
    returncode = getattr(result, "returncode", None)
    if not isinstance(returncode, int):
        message = "GraphQL transport result had no integer process status"
        if prepared is None:
            raise GraphQLDeterministicError(message)
        raise _mutation_unknown(message, prepared)
    if returncode != 0:
        _classify_status(spec, prepared, result)
    return _validate_result(spec, prepared, result)


def _repo_identity(data: dict[str, Any], owner: str, name: str) -> dict[str, Any]:
    repository = data.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("repository payload was missing")
    actual_name = repository.get("name")
    actual_owner = repository.get("owner")
    login = actual_owner.get("login") if isinstance(actual_owner, dict) else None
    if actual_name != name or login != owner:
        raise ValueError("repository identity did not match the requested repository")
    return repository


def _page_info(connection: object) -> dict[str, Any]:
    if not isinstance(connection, dict):
        raise ValueError("GraphQL connection was not an object")
    page_info = connection.get("pageInfo")
    if not isinstance(page_info, dict) or not isinstance(page_info.get("hasNextPage"), bool):
        raise ValueError("GraphQL connection pageInfo was malformed")
    cursor = page_info.get("endCursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError("GraphQL connection endCursor was malformed")
    nodes = connection.get("nodes")
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        raise ValueError("GraphQL connection nodes were malformed")
    return connection


def _query[T](
    operation: str, document: str, validator: Callable[[dict[str, Any]], T]
) -> GraphQLQuerySpec[T]:
    return GraphQLQuerySpec(operation, document, validator)


def issue_comment_ids_query(
    owner: str, name: str, issue_number: int
) -> GraphQLQuerySpec[list[dict[str, Any]]]:
    """Build the validated issue-comment metadata query."""
    document = (
        "query issueCommentIds($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){owner{login} name "
        "issue(number:$number){number comments(last:100,orderBy:{field:UPDATED_AT,direction:DESC}){"
        "pageInfo{hasNextPage endCursor} nodes{id databaseId body createdAt url viewerDidAuthor "
        "authorAssociation author{login}}}}}}"
    )

    def validate(data: dict[str, Any]) -> list[dict[str, Any]]:
        repository = _repo_identity(data, owner, name)
        issue = repository.get("issue")
        if not isinstance(issue, dict) or issue.get("number") != issue_number:
            raise ValueError("issue identity was malformed")
        connection = _page_info(issue.get("comments"))
        nodes = cast(list[dict[str, Any]], connection["nodes"])
        for node in nodes:
            if not isinstance(node.get("id"), str) or not node["id"]:
                raise ValueError("comment id was malformed")
            if isinstance(node.get("databaseId"), bool) or not isinstance(
                node.get("databaseId"), int
            ):
                raise ValueError("comment database id was malformed")
            for field in ("body", "createdAt", "url"):
                if not isinstance(node.get(field), str):
                    raise ValueError(f"comment {field} was malformed")
            if not isinstance(node.get("viewerDidAuthor"), bool):
                raise ValueError("comment ownership was malformed")
            if not isinstance(node.get("authorAssociation"), str):
                raise ValueError("comment author association was malformed")
        return nodes

    return _query("issueCommentIds", document, validate)


def inline_review_threads_page_query(
    owner: str, name: str, pr_number: int
) -> GraphQLQuerySpec[dict[str, Any]]:
    """Build one validated inline-review-thread page."""
    document = (
        "query InlineReviewThreads($owner:String!,$name:String!,$number:Int!,$after:String){"
        "repository(owner:$owner,name:$name){owner{login} name "
        "pullRequest(number:$number){id number reviewThreads(first:100,after:$after){"
        "pageInfo{hasNextPage endCursor} nodes{id isResolved path line side:diffSide "
        "comments(first:1){pageInfo{hasNextPage endCursor} nodes{id body viewerCanUpdate "
        "pullRequestReview{id}}}}}}}}"
    )

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        repository = _repo_identity(data, owner, name)
        pull_request = repository.get("pullRequest")
        if (
            not isinstance(pull_request, dict)
            or not isinstance(pull_request.get("id"), str)
            or pull_request.get("number") != pr_number
        ):
            raise ValueError("pull request identity was malformed")
        connection = _page_info(pull_request.get("reviewThreads"))
        for node in connection["nodes"]:
            if not isinstance(node.get("id"), str) or not node["id"]:
                raise ValueError("review thread id was malformed")
            if not isinstance(node.get("isResolved"), bool) or not isinstance(
                node.get("path"), str
            ):
                raise ValueError("review thread anchor was malformed")
            if node.get("line") is not None and (
                isinstance(node.get("line"), bool) or not isinstance(node.get("line"), int)
            ):
                raise ValueError("review thread line was malformed")
            comments = _page_info(node.get("comments"))
            for comment in comments["nodes"]:
                for field in ("id", "body"):
                    if not isinstance(comment.get(field), str):
                        raise ValueError("inline comment field was malformed")
                if not isinstance(comment.get("viewerCanUpdate"), bool):
                    raise ValueError("inline comment ownership was malformed")
        return connection

    return _query("inlineReviewThreads", document, validate)


def _mutation[T](
    operation: str,
    document: str,
    variables: Mapping[str, GraphQLScalar],
    targets: tuple[str, ...],
    content: tuple[str, ...],
    validator: Callable[[dict[str, Any], GraphQLMutationIntent], T],
) -> GraphQLMutationSpec[T]:
    return GraphQLMutationSpec(operation, document, variables, targets, content, validator)


def update_review_comment_mutation(
    comment_node_id: str, body: str
) -> GraphQLMutationSpec[dict[str, Any]]:
    """Build an exact review-comment edit receipt mutation."""
    document = (
        "mutation UpdateReviewComment($id:ID!,$body:String!,$clientMutationId:String!){"
        "updatePullRequestReviewComment(input:{pullRequestReviewCommentId:$id,body:$body,"
        "clientMutationId:$clientMutationId}){clientMutationId pullRequestReviewComment{id body}}}"
    )

    def validate(data: dict[str, Any], intent: GraphQLMutationIntent) -> dict[str, Any]:
        payload = data.get("updatePullRequestReviewComment")
        if (
            not isinstance(payload, dict)
            or payload.get("clientMutationId") != intent.client_mutation_id
        ):
            raise ValueError("update receipt correlation was missing")
        comment = payload.get("pullRequestReviewComment")
        if (
            not isinstance(comment, dict)
            or comment.get("id") != comment_node_id
            or comment.get("body") != body
        ):
            raise ValueError("update receipt did not echo the requested comment")
        return {"clientMutationId": intent.client_mutation_id, **comment}

    return _mutation(
        "updatePullRequestReviewComment",
        document,
        {"id": comment_node_id, "body": body},
        ("id",),
        ("body",),
        validate,
    )


def review_thread_snapshot_page_query(
    owner: str, name: str, pr_number: int, thread_id: str
) -> GraphQLQuerySpec[dict[str, Any]]:
    """Build a complete page query for one PR-local review thread."""
    document = (
        "query ReviewThreadSnapshot($owner:String!,$name:String!,$number:Int!"
        ",$threadId:ID!,$after:String){"
        "repository(owner:$owner,name:$name){owner{login} name "
        "pullRequest(number:$number){id number}}"
        "node(id:$threadId){... on PullRequestReviewThread{id isResolved path line side:diffSide "
        "pullRequest{id number repository{name owner{login}}} comments(first:100,after:$after){"
        "pageInfo{hasNextPage endCursor} nodes{id body author{login}}}}}}"
    )

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        repository = _repo_identity(data, owner, name)
        requested_pr = repository.get("pullRequest")
        node = data.get("node")
        if (
            not isinstance(requested_pr, dict)
            or not isinstance(requested_pr.get("id"), str)
            or not isinstance(node, dict)
        ):
            raise ValueError("thread snapshot identity was malformed")
        thread_pr = node.get("pullRequest")
        thread_repo = thread_pr.get("repository") if isinstance(thread_pr, dict) else None
        thread_owner = thread_repo.get("owner") if isinstance(thread_repo, dict) else None
        if (
            node.get("id") != thread_id
            or not isinstance(node.get("isResolved"), bool)
            or not isinstance(node.get("path"), str)
            or not isinstance(thread_pr, dict)
            or thread_pr.get("id") != requested_pr.get("id")
            or thread_pr.get("number") != pr_number
            or not isinstance(thread_repo, dict)
            or thread_repo.get("name") != name
            or not isinstance(thread_owner, dict)
            or thread_owner.get("login") != owner
        ):
            raise ValueError("thread snapshot did not match the requested PR")
        connection = _page_info(node.get("comments"))
        for comment in connection["nodes"]:
            if not isinstance(comment.get("id"), str) or not isinstance(comment.get("body"), str):
                raise ValueError("thread comment fields were malformed")
            author = comment.get("author")
            if author is not None and (
                not isinstance(author, dict) or not isinstance(author.get("login"), str)
            ):
                raise ValueError("thread comment author was malformed")
        return {"pullRequest": requested_pr, "thread": node, "comments": connection}

    return _query("reviewThreadSnapshot", document, validate)


def unresolved_review_threads_page_query(
    owner: str, name: str, pr_number: int
) -> GraphQLQuerySpec[dict[str, Any]]:
    """Build a validated unresolved-thread page."""
    document = (
        "query UnresolvedReviewThreads($owner:String!,$name:String!,$number:Int!,$after:String){"
        "repository(owner:$owner,name:$name){owner{login} name "
        "pullRequest(number:$number){id number reviewThreads(first:100,after:$after){"
        "pageInfo{hasNextPage endCursor} nodes{id isResolved}}}}}"
    )

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        repository = _repo_identity(data, owner, name)
        pull_request = repository.get("pullRequest")
        if (
            not isinstance(pull_request, dict)
            or not isinstance(pull_request.get("id"), str)
            or pull_request.get("number") != pr_number
        ):
            raise ValueError("pull request identity was malformed")
        connection = _page_info(pull_request.get("reviewThreads"))
        for node in connection["nodes"]:
            if not isinstance(node.get("id"), str) or not isinstance(node.get("isResolved"), bool):
                raise ValueError("review thread fields were malformed")
        return connection

    return _query("unresolvedReviewThreads", document, validate)


def batch_issue_states_query(
    batch: list[int], owner: str, name: str
) -> GraphQLQuerySpec[dict[int, str]]:
    """Build an aliased query requiring every requested issue state."""
    var_decls = ",".join(f"$n{idx}:Int!" for idx in range(len(batch)))
    fragments = " ".join(
        f"issue{idx}:issue(number:$n{idx}){{number state}}" for idx in range(len(batch))
    )
    document = (
        f"query BatchIssueStates($owner:String!,$name:String!,{var_decls}){{"
        f"repository(owner:$owner,name:$name){{owner{{login}} name {fragments}}}}}"
    )

    def validate(data: dict[str, Any]) -> dict[int, str]:
        repository = _repo_identity(data, owner, name)
        result: dict[int, str] = {}
        for idx, expected_number in enumerate(batch):
            issue = repository.get(f"issue{idx}")
            if (
                not isinstance(issue, dict)
                or issue.get("number") != expected_number
                or issue.get("state") not in {"OPEN", "CLOSED"}
            ):
                raise ValueError(f"issue alias issue{idx} was missing or mismatched")
            result[expected_number] = str(issue["state"])
        return result

    return _query("batchIssueStates", document, validate)


def issue_comments_query(
    owner: str, name: str, issue_number: int
) -> GraphQLQuerySpec[list[dict[str, Any]]]:
    """Build a strict single-issue comment query."""
    document = (
        "query IssueComments($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){owner{login} name "
        "issue(number:$number){number comments(last:100,orderBy:{"
        "field:UPDATED_AT,direction:DESC}){"
        "pageInfo{hasNextPage endCursor} nodes{body updatedAt url}}}}}"
    )

    def validate(data: dict[str, Any]) -> list[dict[str, Any]]:
        repository = _repo_identity(data, owner, name)
        issue = repository.get("issue")
        if not isinstance(issue, dict) or issue.get("number") != issue_number:
            raise ValueError("issue identity was malformed")
        connection = _page_info(issue.get("comments"))
        for node in connection["nodes"]:
            if not all(isinstance(node.get(field), str) for field in ("body", "updatedAt", "url")):
                raise ValueError("issue comment fields were malformed")
        return list(reversed(connection["nodes"]))

    return _query("issueComments", document, validate)


def _batch_issue_query(
    operation: str,
    fields: str,
    batch: list[int],
    owner: str,
    name: str,
) -> GraphQLQuerySpec[dict[int, Any]]:
    var_decls = ",".join(f"$n{idx}:Int!" for idx in range(len(batch)))
    fragments = " ".join(
        f"issue{idx}:issue(number:$n{idx}){{number {fields}}}" for idx in range(len(batch))
    )
    document = (
        f"query {operation}($owner:String!,$name:String!,{var_decls}){{"
        f"repository(owner:$owner,name:$name){{owner{{login}} name {fragments}}}}}"
    )

    def validate(data: dict[str, Any]) -> dict[int, Any]:
        repository = _repo_identity(data, owner, name)
        result: dict[int, Any] = {}
        for idx, expected_number in enumerate(batch):
            issue = repository.get(f"issue{idx}")
            if not isinstance(issue, dict) or issue.get("number") != expected_number:
                raise ValueError(f"issue alias issue{idx} was missing or mismatched")
            result[expected_number] = issue
        return result

    return _query(operation, document, validate)


def batch_issue_comments_query(
    batch: list[int], owner: str, name: str
) -> GraphQLQuerySpec[dict[int, list[dict[str, Any]]]]:
    """Build a strict aliased batch issue-comment query."""
    spec = _batch_issue_query(
        "BatchIssueComments",
        "comments(last:100,orderBy:{field:UPDATED_AT,direction:DESC}){"
        "pageInfo{hasNextPage endCursor} nodes{body updatedAt url}}",
        batch,
        owner,
        name,
    )

    def validate(data: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
        raw = spec.validate(data)
        result: dict[int, list[dict[str, Any]]] = {}
        for number, issue in raw.items():
            connection = _page_info(issue.get("comments"))
            nodes = connection["nodes"]
            if not all(
                all(isinstance(node.get(field), str) for field in ("body", "updatedAt", "url"))
                for node in nodes
            ):
                raise ValueError("issue comment fields were malformed")
            result[number] = list(reversed(nodes))
        return result

    return GraphQLQuerySpec(spec.operation, spec.query, validate)


def batch_issue_labels_query(
    batch: list[int], owner: str, name: str
) -> GraphQLQuerySpec[dict[int, list[str]]]:
    """Build a strict aliased batch issue-label query."""
    spec = _batch_issue_query(
        "BatchIssueLabels",
        "labels(first:50){pageInfo{hasNextPage endCursor} nodes{name}}",
        batch,
        owner,
        name,
    )

    def validate(data: dict[str, Any]) -> dict[int, list[str]]:
        raw = spec.validate(data)
        result: dict[int, list[str]] = {}
        for number, issue in raw.items():
            connection = _page_info(issue.get("labels"))
            names = [node.get("name") for node in connection["nodes"]]
            if not all(isinstance(label, str) and label for label in names):
                raise ValueError("label names were malformed")
            result[number] = names
        return result

    return GraphQLQuerySpec(spec.operation, spec.query, validate)


def batch_issue_titles_query(
    batch: list[int], owner: str, name: str
) -> GraphQLQuerySpec[dict[int, str]]:
    """Build a strict aliased batch issue-title query."""
    spec = _batch_issue_query("BatchIssueTitles", "title", batch, owner, name)

    def validate(data: dict[str, Any]) -> dict[int, str]:
        raw = spec.validate(data)
        result: dict[int, str] = {}
        for number, issue in raw.items():
            if not isinstance(issue.get("title"), str):
                raise ValueError("issue title was malformed")
            result[number] = issue["title"]
        return result

    return GraphQLQuerySpec(spec.operation, spec.query, validate)


def pipeline_unresolved_threads_page_query(
    owner: str, name: str, pr_number: int
) -> GraphQLQuerySpec[dict[str, Any]]:
    """Build the repository-scoped pipeline thread page query."""
    return unresolved_review_threads_page_query(owner, name, pr_number)


def review_receipts_page_query(
    owner: str, name: str, pr_number: int, review_id: str
) -> GraphQLQuerySpec[dict[str, Any]]:
    """Build a strict page query for comments belonging to one review."""
    document = (
        "query ReviewReceipts($owner:String!,$name:String!,$number:Int!,$after:String){"
        "repository(owner:$owner,name:$name){owner{login} name "
        "pullRequest(number:$number){id number reviewThreads(first:100,after:$after){"
        "pageInfo{hasNextPage endCursor} nodes{id isResolved path line side:diffSide "
        "comments(first:2){pageInfo{hasNextPage endCursor} nodes{id body "
        "author{login} pullRequestReview{id state commit{oid}}}}}}}}}"
    )

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        repository = _repo_identity(data, owner, name)
        pull_request = repository.get("pullRequest")
        if (
            not isinstance(pull_request, dict)
            or not isinstance(pull_request.get("id"), str)
            or pull_request.get("number") != pr_number
        ):
            raise ValueError("pull request identity was malformed")
        connection = _page_info(pull_request.get("reviewThreads"))
        for node in connection["nodes"]:
            if (
                not isinstance(node.get("id"), str)
                or not isinstance(node.get("isResolved"), bool)
                or not isinstance(node.get("path"), str)
            ):
                raise ValueError("review receipt thread identity was malformed")
            line = node.get("line")
            if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
                raise ValueError("review receipt line was malformed")
            side = node.get("side")
            if side is not None and not isinstance(side, str):
                raise ValueError("review receipt diff side was malformed")
            comments = _page_info(node.get("comments"))
            for comment in comments["nodes"]:
                if not isinstance(comment.get("id"), str) or not isinstance(
                    comment.get("body"), str
                ):
                    raise ValueError("review receipt comment was malformed")
                author = comment.get("author")
                if author is not None and (
                    not isinstance(author, dict) or not isinstance(author.get("login"), str)
                ):
                    raise ValueError("review receipt comment author was malformed")
                review = comment.get("pullRequestReview")
                if not isinstance(review, dict) or not isinstance(review.get("id"), str):
                    raise ValueError("review receipt review identity was malformed")
                commit = review.get("commit")
                if (
                    not isinstance(review.get("state"), str)
                    or not isinstance(commit, dict)
                    or not isinstance(commit.get("oid"), str)
                ):
                    raise ValueError("review receipt review commit was malformed")
        return connection

    return _query("reviewReceipts", document, validate)


def merge_authorization_reviews_page_query(
    owner: str, name: str, pr_number: int
) -> GraphQLQuerySpec[dict[str, Any]]:
    """Build one validated merge-authorization native-review page query."""
    document = (
        "query($owner:String!,$name:String!,$number:Int!,$after:String){"
        " repository(owner:$owner,name:$name){"
        "  id name owner{login}"
        "  pullRequest(number:$number){"
        "   id number headRefOid"
        "   reviews(first:100,after:$after){"
        "    totalCount pageInfo{hasNextPage endCursor}"
        "    nodes{id fullDatabaseId body state submittedAt updatedAt "
        "includesCreatedEdit lastEditedAt viewerDidAuthor "
        "author{login __typename} commit{oid}}"
        "   }"
        "  }"
        " }"
        "}"
    )

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        repository = _repo_identity(data, owner, name)
        if not isinstance(repository.get("id"), str):
            raise ValueError("repository identity was malformed")
        pull_request = repository.get("pullRequest")
        if (
            not isinstance(pull_request, dict)
            or not isinstance(pull_request.get("id"), str)
            or pull_request.get("number") != pr_number
            or not isinstance(pull_request.get("headRefOid"), str)
            or not pull_request["headRefOid"]
        ):
            raise ValueError("merge authorization pull request identity was malformed")
        connection = _page_info(pull_request.get("reviews"))
        total_count = connection.get("totalCount")
        if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
            raise ValueError("merge authorization review count was malformed")
        for node in connection["nodes"]:
            review_id = node.get("id")
            if not isinstance(review_id, str) or not review_id:
                raise ValueError("merge authorization review identity was malformed")
        return pull_request

    return _query("MergeAuthorizationReviews", document, validate)


def _receipt_mutation[T](
    operation: str,
    document: str,
    variables: Mapping[str, GraphQLScalar],
    targets: tuple[str, ...],
    content: tuple[str, ...],
    payload_name: str,
    required: Callable[[dict[str, Any], GraphQLMutationIntent, dict[str, Any]], T],
) -> GraphQLMutationSpec[T]:
    def validate(data: dict[str, Any], intent: GraphQLMutationIntent) -> T:
        payload = data.get(payload_name)
        if (
            not isinstance(payload, dict)
            or payload.get("clientMutationId") != intent.client_mutation_id
        ):
            raise ValueError("mutation receipt correlation was missing")
        return required(payload, intent, data)

    return _mutation(operation, document, variables, targets, content, validate)


def add_implementation_thread_reply_mutation(
    thread_id: str, body: str, *, pending_review_id: str, expected_head_sha: str
) -> GraphQLMutationSpec[dict[str, Any]]:
    """Build a receipt-bound implementation reply mutation."""
    document = (
        "mutation AddImplementationReply($threadId:ID!,$body:String!,$clientMutationId:String!){"
        "addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId,"
        "body:$body,clientMutationId:$clientMutationId}){"
        "clientMutationId comment{id body viewerDidAuthor "
        "pullRequestReview{id state commit{oid}}}}}"
    )

    def required(
        payload: dict[str, Any], intent: GraphQLMutationIntent, _: dict[str, Any]
    ) -> dict[str, Any]:
        comment = payload.get("comment")
        review = comment.get("pullRequestReview") if isinstance(comment, dict) else None
        if (
            not isinstance(comment, dict)
            or comment.get("body") != body
            or comment.get("viewerDidAuthor") is not True
            or not isinstance(comment.get("id"), str)
        ):
            raise ValueError("implementation reply receipt was incomplete")
        if (
            not isinstance(review, dict)
            or review.get("id") != pending_review_id
            or review.get("state") != "PENDING"
            or (review.get("commit") or {}).get("oid") != expected_head_sha
        ):
            raise ValueError("implementation reply review receipt was incomplete")
        return {"clientMutationId": intent.client_mutation_id, **comment}

    return _receipt_mutation(
        "addPullRequestReviewThreadReply",
        document,
        {"threadId": thread_id, "body": body},
        ("threadId",),
        ("body",),
        "addPullRequestReviewThreadReply",
        required,
    )


def add_thread_reply_mutation(
    thread_id: str,
    body: str,
    *,
    pending_review_id: str,
    expected_head_sha: str,
) -> GraphQLMutationSpec[dict[str, Any]]:
    """Build the implementation reply spec under the canonical plan name."""
    return add_implementation_thread_reply_mutation(
        thread_id,
        body,
        pending_review_id=pending_review_id,
        expected_head_sha=expected_head_sha,
    )


def add_reviewer_feedback_reply_mutation(
    thread_id: str, body: str, *, expected_head_sha: str
) -> GraphQLMutationSpec[dict[str, Any]]:
    """Build a receipt-bound reviewer feedback reply mutation."""
    document = (
        "mutation AddReviewerFeedback($threadId:ID!,$body:String!,$clientMutationId:String!){"
        "addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId,"
        "body:$body,clientMutationId:$clientMutationId}){"
        "clientMutationId comment{id body viewerDidAuthor "
        "pullRequestReview{id state commit{oid}}}}}"
    )

    def required(
        payload: dict[str, Any], intent: GraphQLMutationIntent, _: dict[str, Any]
    ) -> dict[str, Any]:
        comment = payload.get("comment")
        review = comment.get("pullRequestReview") if isinstance(comment, dict) else None
        if (
            not isinstance(comment, dict)
            or comment.get("body") != body
            or comment.get("viewerDidAuthor") is not True
            or not isinstance(comment.get("id"), str)
        ):
            raise ValueError("reviewer feedback receipt was incomplete")
        if (
            not isinstance(review, dict)
            or review.get("state") != "COMMENTED"
            or (review.get("commit") or {}).get("oid") != expected_head_sha
        ):
            raise ValueError("reviewer feedback review receipt was incomplete")
        return {"clientMutationId": intent.client_mutation_id, **comment}

    return _receipt_mutation(
        "addPullRequestReviewThreadReply",
        document,
        {"threadId": thread_id, "body": body},
        ("threadId",),
        ("body",),
        "addPullRequestReviewThreadReply",
        required,
    )


def create_pending_review_mutation(
    pull_request_id: str, head_sha: str
) -> GraphQLMutationSpec[dict[str, Any]]:
    """Build the pending-review creation receipt mutation."""
    document = (
        "mutation CreatePendingReview($pullRequestId:ID!,$headSha:GitObjectID!,$clientMutationId:"
        "String!){"
        "addPullRequestReview(input:{pullRequestId:$pullRequestId,commitOID:$headSha,clientMutationId:$clientMutationId}){"
        "clientMutationId pullRequestReview{id state pullRequest{id} commit{oid}}}}"
    )

    def required(
        payload: dict[str, Any], intent: GraphQLMutationIntent, _: dict[str, Any]
    ) -> dict[str, Any]:
        review = payload.get("pullRequestReview")
        if (
            not isinstance(review, dict)
            or not isinstance(review.get("id"), str)
            or review.get("state") != "PENDING"
            or (review.get("pullRequest") or {}).get("id") != pull_request_id
            or (review.get("commit") or {}).get("oid") != head_sha
        ):
            raise ValueError("pending review receipt was incomplete")
        return {"clientMutationId": intent.client_mutation_id, **review}

    return _receipt_mutation(
        "addPullRequestReview",
        document,
        {"pullRequestId": pull_request_id, "headSha": head_sha},
        ("pullRequestId",),
        (),
        "addPullRequestReview",
        required,
    )


def submit_review_mutation(
    review_id: str, pull_request_id: str, head_sha: str
) -> GraphQLMutationSpec[dict[str, Any]]:
    """Build the exact submitted-review receipt mutation."""
    document = (
        "mutation SubmitReview($reviewId:ID!,$clientMutationId:String!){"
        "submitPullRequestReview(input:{pullRequestReviewId:$reviewId,event:COMMENT,clientMutationId:$clientMutationId}){"
        "clientMutationId pullRequestReview{id state pullRequest{id} commit{oid}}}}"
    )

    def required(
        payload: dict[str, Any], intent: GraphQLMutationIntent, _: dict[str, Any]
    ) -> dict[str, Any]:
        review = payload.get("pullRequestReview")
        if (
            not isinstance(review, dict)
            or review.get("id") != review_id
            or review.get("state") != "COMMENTED"
            or (review.get("pullRequest") or {}).get("id") != pull_request_id
            or (review.get("commit") or {}).get("oid") != head_sha
        ):
            raise ValueError("submitted review receipt was incomplete")
        return {"clientMutationId": intent.client_mutation_id, **review}

    return _receipt_mutation(
        "submitPullRequestReview",
        document,
        {"reviewId": review_id},
        ("reviewId",),
        (),
        "submitPullRequestReview",
        required,
    )


def pipeline_thread_snapshot_page_query(
    owner: str, name: str, pr_number: int, thread_id: str
) -> GraphQLQuerySpec[dict[str, Any]]:
    """Build a full PR/thread page used for mutation readback proofs."""
    document = (
        "query PipelineThreadSnapshot($owner:String!,$name:String!,$number:Int!,$threadId:ID!"
        ",$after:String){"
        "repository(owner:$owner,name:$name){owner{login} name pullRequest(number:$number){"
        "id number state headRefOid autoMergeRequest{enabledAt}}}"
        "node(id:$threadId){... on PullRequestReviewThread{id isResolved path line side:diffSide "
        "pullRequest{id number repository{name owner{login}}} comments(first:100,after:$after){"
        "pageInfo{hasNextPage endCursor} nodes{id body viewerDidAuthor author{login __typename} "
        "pullRequestReview{id state body commit{oid}}}}}}}"
    )

    def validate(data: dict[str, Any]) -> dict[str, Any]:
        repository = _repo_identity(data, owner, name)
        pull_request = repository.get("pullRequest")
        node = data.get("node")
        if not isinstance(pull_request, dict) or not isinstance(node, dict):
            raise ValueError("pipeline snapshot identity was malformed")
        pr_id = pull_request.get("id")
        if (
            not isinstance(pr_id, str)
            or pull_request.get("number") != pr_number
            or not isinstance(pull_request.get("state"), str)
            or not isinstance(pull_request.get("headRefOid"), str)
            or "autoMergeRequest" not in pull_request
            or (
                pull_request.get("autoMergeRequest") is not None
                and not isinstance(pull_request.get("autoMergeRequest"), dict)
            )
        ):
            raise ValueError("pipeline pull request state was malformed")
        thread_pr = node.get("pullRequest")
        thread_repo = thread_pr.get("repository") if isinstance(thread_pr, dict) else None
        thread_owner = thread_repo.get("owner") if isinstance(thread_repo, dict) else None
        if (
            node.get("id") != thread_id
            or not isinstance(node.get("isResolved"), bool)
            or not isinstance(node.get("path"), str)
            or not isinstance(thread_pr, dict)
            or thread_pr.get("id") != pr_id
            or thread_pr.get("number") != pr_number
            or not isinstance(thread_repo, dict)
            or thread_repo.get("name") != name
            or not isinstance(thread_owner, dict)
            or thread_owner.get("login") != owner
        ):
            raise ValueError("pipeline thread identity did not match the requested PR")
        connection = _page_info(node.get("comments"))
        for comment in connection["nodes"]:
            if (
                not isinstance(comment.get("id"), str)
                or not isinstance(comment.get("body"), str)
                or not isinstance(comment.get("viewerDidAuthor"), bool)
            ):
                raise ValueError("pipeline thread comment fields were malformed")
            author = comment.get("author")
            if author is not None and (
                not isinstance(author, dict) or not isinstance(author.get("login"), str)
            ):
                raise ValueError("pipeline thread comment author was malformed")
            review = comment.get("pullRequestReview")
            if not isinstance(review, dict):
                raise ValueError("pipeline thread review was missing")
            commit = review.get("commit")
            if not isinstance(review.get("id"), str) or not isinstance(review.get("state"), str):
                raise ValueError("pipeline thread review identity was malformed")
            if not isinstance(commit, dict) or not isinstance(commit.get("oid"), str):
                raise ValueError("pipeline thread review commit was malformed")
        return {
            "pullRequest": pull_request,
            "thread": node,
            "comments": connection,
            "pr_node_id": pr_id,
            "pr_state": {
                "state": pull_request["state"],
                "headRefOid": pull_request["headRefOid"],
                "autoMergeRequest": pull_request["autoMergeRequest"],
            },
        }

    return _query("pipelineThreadSnapshot", document, validate)


def resolve_thread_mutation(thread_id: str) -> GraphQLMutationSpec[dict[str, Any]]:
    """Build the exact thread-resolution receipt mutation."""
    document = (
        "mutation ResolveThread($threadId:ID!,$clientMutationId:String!){"
        "resolveReviewThread(input:{threadId:$threadId,clientMutationId:$clientMutationId}){"
        "clientMutationId thread{id isResolved}}}"
    )

    def required(
        payload: dict[str, Any], intent: GraphQLMutationIntent, _: dict[str, Any]
    ) -> dict[str, Any]:
        thread = payload.get("thread")
        if (
            not isinstance(thread, dict)
            or thread.get("id") != thread_id
            or thread.get("isResolved") is not True
        ):
            raise ValueError("resolve receipt was incomplete")
        return {"clientMutationId": intent.client_mutation_id, **thread}

    return _receipt_mutation(
        "resolveReviewThread",
        document,
        {"threadId": thread_id},
        ("threadId",),
        (),
        "resolveReviewThread",
        required,
    )


def github_schema_contract_query() -> GraphQLQuerySpec[dict[str, Any]]:
    """Build a read-only introspection query for the live schema contract lane."""
    document = (
        "query GithubSchemaContract{__schema{queryType{name} mutationType{name} "
        "types{name kind fields{name} inputFields{name} possibleTypes{name}}}}"
    )
    return _query("githubSchemaContract", document, lambda data: data)


__all__ = [
    "ClaudeUsageCapError",
    "GitHubRateLimitError",
    "GitHubUnavailableError",
    "GraphQLDeterministicError",
    "GraphQLMutationIntent",
    "GraphQLMutationOutcomeUnknownError",
    "GraphQLMutationSpec",
    "GraphQLQuerySpec",
    "GraphQLResponseError",
    "GraphQLRetryableError",
    "ReviewCommentNotEditableError",
    "add_implementation_thread_reply_mutation",
    "add_reviewer_feedback_reply_mutation",
    "add_thread_reply_mutation",
    "batch_issue_comments_query",
    "batch_issue_labels_query",
    "batch_issue_states_query",
    "batch_issue_titles_query",
    "create_pending_review_mutation",
    "gh_call",
    "gh_cli_timeout",
    "github_schema_contract_query",
    "inline_review_threads_page_query",
    "issue_comment_ids_query",
    "issue_comments_query",
    "pipeline_thread_snapshot_page_query",
    "pipeline_unresolved_threads_page_query",
    "resolve_thread_mutation",
    "review_receipts_page_query",
    "review_thread_snapshot_page_query",
    "run_graphql",
    "submit_review_mutation",
    "unresolved_review_threads_page_query",
    "update_review_comment_mutation",
]
