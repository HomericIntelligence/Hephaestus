"""Fail-closed release preflight and immutable-release withdrawal helpers.

Published PyPI files and immutable GitHub release assets are deliberately not
replaced by this module.  A rollback verifies that every PyPI file is already
yanked, then adds a withdrawal advisory to the immutable release notes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from email.message import Message
from typing import Any, Protocol

WITHDRAWN_MARKER = "<!-- hephaestus-release-withdrawn -->"
DEFAULT_REPOSITORY = "HomericIntelligence/Hephaestus"
DEFAULT_PACKAGE = "HomericIntelligence-Hephaestus"
DEFAULT_GITHUB_API_BASE = "https://api.github.com"
DEFAULT_PYPI_API_BASE = "https://pypi.org"
DEFAULT_TESTPYPI_API_BASE = "https://test.pypi.org"
_TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ReleaseRollbackError(RuntimeError):
    """Raised when release state is unsafe, incomplete, or cannot be verified."""


@dataclass(frozen=True)
class AssetFingerprint:
    """The immutable fields used to prove GitHub release assets were preserved."""

    asset_id: int
    name: str
    size: int
    digest: str | None


@dataclass(frozen=True)
class ReleaseState:
    """The remote publication state required for preflight or withdrawal."""

    tag_exists: bool
    testpypi_exists: bool
    pypi_exists: bool
    pypi_yanked: bool
    github_release: dict[str, Any] | None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject every redirect so GitHub credentials cannot leave the API origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        """Reject redirects instead of replaying the authenticated request."""
        return None


def validate_tag(tag: str) -> str:
    """Validate and return a strict release tag in ``vX.Y.Z`` form.

    Args:
        tag: Candidate annotated release tag.

    Returns:
        The validated tag.

    Raises:
        ReleaseRollbackError: If *tag* is not a strict release tag.

    """
    if not _TAG_PATTERN.fullmatch(tag):
        raise ReleaseRollbackError("release tag must match vX.Y.Z")
    return tag


def validate_repository(repository: str) -> str:
    """Validate and return a GitHub ``owner/repository`` identifier.

    Args:
        repository: Repository identifier supplied by an operator or workflow.

    Returns:
        The validated identifier.

    Raises:
        ReleaseRollbackError: If the identifier cannot safely form an API path.

    """
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise ReleaseRollbackError("repository must match owner/repository")
    return repository


def _validate_package(package: str) -> str:
    """Validate a PyPI project name before URL-encoding it into an API path."""
    if not package or package.strip() != package or "/" in package:
        raise ReleaseRollbackError("package must be a non-empty PyPI project name")
    return package


class ReleaseApiClient:
    """Small standard-library client for the GitHub and PyPI release APIs."""

    def __init__(
        self,
        *,
        repository: str,
        package: str,
        github_api_base: str = DEFAULT_GITHUB_API_BASE,
        pypi_api_base: str = DEFAULT_PYPI_API_BASE,
        testpypi_api_base: str | None = None,
        token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        """Configure a client without performing any remote request.

        Args:
            repository: GitHub ``owner/repository`` containing release records.
            package: Published PyPI project name.
            github_api_base: GitHub REST API base, injectable for rehearsals.
            pypi_api_base: Production PyPI API base, injectable for rehearsals.
            testpypi_api_base: Optional TestPyPI base; defaults to the real
                TestPyPI endpoint, or the custom PyPI base for one-server tests.
            token: Optional GitHub token used for GitHub API authentication.
            timeout: Request timeout in seconds.

        Raises:
            ReleaseRollbackError: If a local API configuration is invalid.

        """
        self.repository = validate_repository(repository)
        self.package = _validate_package(package)
        self.github_api_base = _normalize_api_base(github_api_base, "GitHub API base")
        self.pypi_api_base = _normalize_api_base(pypi_api_base, "PyPI API base")
        if testpypi_api_base is None:
            self.testpypi_api_base = (
                self.pypi_api_base
                if self.pypi_api_base != DEFAULT_PYPI_API_BASE
                else DEFAULT_TESTPYPI_API_BASE
            )
        else:
            self.testpypi_api_base = _normalize_api_base(testpypi_api_base, "TestPyPI API base")
        if timeout <= 0:
            raise ReleaseRollbackError("timeout must be greater than zero")
        self.token = token
        self.timeout = timeout

    def remote_tag_exists(self, tag: str) -> bool:
        """Return whether the exact tag exists in the configured GitHub repository."""
        validate_tag(tag)
        encoded_tag = urllib.parse.quote(tag, safe="")
        url = f"{self.github_api_base}/repos/{self.repository}/git/ref/tags/{encoded_tag}"
        return self._github_json("GET", url, allow_404=True) is not None

    def get_release_by_tag(self, tag: str) -> dict[str, Any] | None:
        """Fetch a GitHub release or draft by tag, treating only 404 as absence."""
        validate_tag(tag)
        encoded_tag = urllib.parse.quote(tag, safe="")
        url = f"{self.github_api_base}/repos/{self.repository}/releases/tags/{encoded_tag}"
        return self._github_json("GET", url, allow_404=True)

    def get_release_or_draft_by_tag(self, tag: str) -> dict[str, Any] | None:
        """Fetch a release by tag, including same-tag drafts hidden by the tag endpoint."""
        release = self.get_release_by_tag(tag)
        if release is not None:
            return release
        if not self.token:
            raise ReleaseRollbackError(
                "GH_TOKEN is required to verify no GitHub release draft exists"
            )

        page = 1
        while True:
            url = (
                f"{self.github_api_base}/repos/{self.repository}/releases?per_page=100&page={page}"
            )
            releases = self._github_json_list("GET", url)
            for candidate in releases:
                if candidate.get("tag_name") == tag:
                    return candidate
            if len(releases) < 100:
                return None
            page += 1

    def get_pypi_release(self, *, tag: str, testpypi: bool) -> dict[str, Any] | None:
        """Fetch PyPI metadata for *tag*, treating only 404 as an absent version."""
        version = validate_tag(tag)[1:]
        api_base = self.testpypi_api_base if testpypi else self.pypi_api_base
        encoded_package = urllib.parse.quote(self.package, safe="")
        encoded_version = urllib.parse.quote(version, safe="")
        url = f"{api_base}/pypi/{encoded_package}/{encoded_version}/json"
        return self._pypi_json(url, allow_404=True)

    def get_state(self, tag: str, *, include_testpypi: bool = True) -> ReleaseState:
        """Collect and validate remote release state for a tag.

        Args:
            tag: Strict release tag to inspect.
            include_testpypi: Whether to query TestPyPI as well as production PyPI.

        Returns:
            A typed snapshot of the queried release state.

        Raises:
            ReleaseRollbackError: If an API response is malformed or unavailable.

        """
        validate_tag(tag)
        tag_exists = self.remote_tag_exists(tag)
        github_release = self.get_release_or_draft_by_tag(tag)
        testpypi_release = None
        if include_testpypi:
            testpypi_release = self.get_pypi_release(tag=tag, testpypi=True)
        pypi_release = self.get_pypi_release(tag=tag, testpypi=False)
        pypi_yanked = False
        if pypi_release is not None:
            pypi_yanked = _pypi_release_is_yanked(pypi_release)
        return ReleaseState(
            tag_exists=tag_exists,
            testpypi_exists=testpypi_release is not None,
            pypi_exists=pypi_release is not None,
            pypi_yanked=pypi_yanked,
            github_release=github_release,
        )

    def update_release_notes(self, release_id: int, body: str) -> None:
        """PATCH only the GitHub release notes after an operator confirmation."""
        if not self.token:
            raise ReleaseRollbackError("GH_TOKEN is required before updating GitHub release notes")
        if isinstance(release_id, bool) or release_id < 1:
            raise ReleaseRollbackError("GitHub release ID must be a positive integer")
        url = f"{self.github_api_base}/repos/{self.repository}/releases/{release_id}"
        self._github_json("PATCH", url, payload={"body": body})

    def _github_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        """Request GitHub JSON without leaking the GitHub token to other hosts."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self._request_json(
            method, url, headers=headers, payload=payload, allow_404=allow_404
        )
        if response is None:
            return None
        if not isinstance(response, dict):
            raise ReleaseRollbackError(f"{method} {url} returned a JSON value other than an object")
        return response

    def _pypi_json(self, url: str, *, allow_404: bool) -> dict[str, Any] | None:
        """Request public PyPI metadata without forwarding the GitHub token."""
        response = self._request_json(
            "GET", url, headers={"Accept": "application/json"}, allow_404=allow_404
        )
        if response is None:
            return None
        if not isinstance(response, dict):
            raise ReleaseRollbackError(f"GET {url} returned a JSON value other than an object")
        return response

    def _github_json_list(self, method: str, url: str) -> list[dict[str, Any]]:
        """Request a complete GitHub JSON list, rejecting malformed members."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self._request_json(method, url, headers=headers)
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise ReleaseRollbackError(f"{method} {url} returned a malformed JSON list")
        return response

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """Perform one HTTP request and convert only expected 404s to absence."""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = dict(headers)
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with _open_without_redirects(request, timeout=self.timeout) as response:
                raw_response = response.read()
        except urllib.error.HTTPError as error:
            if allow_404 and error.code == 404:
                return None
            raise ReleaseRollbackError(f"{method} {url} failed with HTTP {error.code}") from error
        except (TimeoutError, urllib.error.URLError) as error:
            raise ReleaseRollbackError(f"{method} {url} failed: {error}") from error

        try:
            decoded = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReleaseRollbackError(f"{method} {url} returned malformed JSON") from error
        if not isinstance(decoded, (dict, list)):
            raise ReleaseRollbackError(f"{method} {url} returned an unsupported JSON value")
        return decoded


class ReleaseNotesClient(Protocol):
    """The narrow client contract needed to apply and verify an advisory."""

    def get_release_by_tag(self, tag: str) -> dict[str, Any] | None:
        """Return the release identified by *tag*, or ``None`` when absent."""
        ...

    def update_release_notes(self, release_id: int, body: str) -> None:
        """Replace only the editable release body for *release_id*."""
        ...


def assert_preflight_clear(state: ReleaseState, tag: str) -> None:
    """Raise unless every immutable-publication target is empty for *tag*."""
    validate_tag(tag)
    conflicts: list[str] = []
    if not state.tag_exists:
        conflicts.append(f"tag {tag} does not exist")
    if state.testpypi_exists:
        conflicts.append("version already exists on TestPyPI")
    if state.pypi_exists:
        conflicts.append("version already exists on PyPI")
    if state.github_release is not None:
        conflicts.append("tag already has a GitHub release or draft")
    if conflicts:
        raise ReleaseRollbackError("; ".join(conflicts))


def require_immutable_release(state: ReleaseState) -> dict[str, Any]:
    """Return the immutable release record or reject incomplete release state."""
    release = state.github_release
    if release is None:
        raise ReleaseRollbackError("GitHub release is required for withdrawal")
    if release.get("immutable") is not True:
        raise ReleaseRollbackError("GitHub release must be immutable before withdrawal")
    release_id = release.get("id")
    if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id < 1:
        raise ReleaseRollbackError("immutable GitHub release has an invalid ID")
    return release


def asset_fingerprints(release: dict[str, Any]) -> tuple[AssetFingerprint, ...]:
    """Return a stable fingerprint tuple for every immutable release asset."""
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ReleaseRollbackError("immutable GitHub release has no complete asset list")

    fingerprints: list[AssetFingerprint] = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseRollbackError("immutable GitHub release has a malformed asset")
        asset_id = asset.get("id")
        name = asset.get("name")
        size = asset.get("size")
        digest = asset.get("digest")
        if isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id < 1:
            raise ReleaseRollbackError("immutable GitHub release asset has an invalid ID")
        if not isinstance(name, str) or not name:
            raise ReleaseRollbackError("immutable GitHub release asset has an invalid name")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ReleaseRollbackError("immutable GitHub release asset has an invalid size")
        if digest is not None and not isinstance(digest, str):
            raise ReleaseRollbackError("immutable GitHub release asset has an invalid digest")
        fingerprints.append(AssetFingerprint(asset_id, name, size, digest))
    return tuple(fingerprints)


def prepend_withdrawal_advisory(body: str, tag: str, reason: str) -> str:
    """Prepend one idempotent withdrawal advisory while preserving release notes."""
    validate_tag(tag)
    clean_reason = reason.strip()
    if not clean_reason:
        raise ReleaseRollbackError("withdrawal reason must be non-empty")
    if WITHDRAWN_MARKER in body:
        return body
    advisory = (
        f"{WITHDRAWN_MARKER}\n"
        f"> **Withdrawal advisory for `{tag}`**\n>\n"
        f"> This release has been withdrawn: {clean_reason}\n>\n"
        "> Use a corrected forward release instead."
    )
    return f"{advisory}\n\n{body}" if body else advisory


def validate_withdrawal_state(state: ReleaseState) -> dict[str, Any]:
    """Return the release only when PyPI withdrawal and immutable state are complete."""
    release = require_immutable_release(state)
    if not state.pypi_exists or not state.pypi_yanked:
        raise ReleaseRollbackError("PyPI release must be yanked before applying the advisory")
    return release


def apply_withdrawal_advisory(
    client: ReleaseNotesClient,
    state: ReleaseState,
    *,
    tag: str,
    reason: str,
) -> None:
    """Persist one withdrawal advisory and verify immutable assets did not change."""
    release = validate_withdrawal_state(state)
    before = asset_fingerprints(release)
    existing_body = release.get("body")
    if existing_body is not None and not isinstance(existing_body, str):
        raise ReleaseRollbackError("immutable GitHub release has malformed notes")
    body = prepend_withdrawal_advisory(existing_body or "", tag, reason)
    if body == existing_body:
        return

    client.update_release_notes(int(release["id"]), body)
    after = client.get_release_by_tag(tag)
    if after is None or WITHDRAWN_MARKER not in str(after.get("body", "")):
        raise ReleaseRollbackError("withdrawal advisory was not persisted")
    if asset_fingerprints(after) != before:
        raise ReleaseRollbackError("immutable release assets changed during withdrawal")


def _normalize_api_base(api_base: str, label: str) -> str:
    """Validate an HTTP(S) API base and remove its trailing slash."""
    parsed = urllib.parse.urlparse(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ReleaseRollbackError(f"{label} must be an absolute HTTP(S) URL")
    return api_base.rstrip("/")


def _open_without_redirects(request: urllib.request.Request, *, timeout: float) -> Any:
    """Open one request while converting every HTTP redirect into a failure."""
    opener = urllib.request.build_opener(_NoRedirectHandler())
    return opener.open(request, timeout=timeout)


def _pypi_release_is_yanked(release: dict[str, Any]) -> bool:
    """Return whether every returned PyPI file is yanked, rejecting partial data."""
    urls = release.get("urls")
    if not isinstance(urls, list) or not urls:
        raise ReleaseRollbackError("PyPI release has no complete file list")
    yanked: list[bool] = []
    for file_data in urls:
        if not isinstance(file_data, dict) or not isinstance(file_data.get("filename"), str):
            raise ReleaseRollbackError("PyPI release has a malformed file record")
        if "yanked" not in file_data:
            raise ReleaseRollbackError("PyPI release file is missing yank status")
        yanked.append(bool(file_data["yanked"]))
    return all(yanked)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser shared by the checked-in script wrapper."""
    parser = argparse.ArgumentParser(
        description="Fail-closed preflight and withdrawal advisory for immutable releases."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "inspect", "rollback"):
        subparser = subcommands.add_parser(command)
        _add_common_arguments(subparser)
        if command == "rollback":
            subparser.add_argument(
                "--reason", required=True, help="Incident and forward-fix reason"
            )
            subparser.add_argument(
                "--apply",
                action="store_true",
                help="Apply the release-note advisory after checks",
            )
            subparser.add_argument(
                "--confirm-tag", help="Must exactly match --tag before an advisory is applied"
            )
    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Add API and identity arguments shared by every rollback subcommand."""
    parser.add_argument("--tag", required=True, help="Exact vX.Y.Z release tag")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY, help="GitHub owner/repository")
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help="PyPI project name")
    parser.add_argument("--github-api-base", default=DEFAULT_GITHUB_API_BASE)
    parser.add_argument("--pypi-api-base", default=DEFAULT_PYPI_API_BASE)
    parser.add_argument("--testpypi-api-base", default=None)
    parser.add_argument("--timeout", type=float, default=10.0)


def _client_from_args(args: argparse.Namespace) -> ReleaseApiClient:
    """Create a typed client from parsed CLI arguments without requiring a token yet."""
    return ReleaseApiClient(
        repository=args.repository,
        package=args.package,
        github_api_base=args.github_api_base,
        pypi_api_base=args.pypi_api_base,
        testpypi_api_base=args.testpypi_api_base,
        token=os.environ.get("GH_TOKEN"),
        timeout=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    """Run a release preflight, inspection, or confirmed withdrawal advisory."""
    args = build_parser().parse_args(argv)
    try:
        tag = validate_tag(args.tag)
        client = _client_from_args(args)
        if args.command == "preflight":
            assert_preflight_clear(client.get_state(tag), tag)
            print(f"Preflight clear for {tag}.")
            return 0
        if args.command == "inspect":
            print(json.dumps(asdict(client.get_state(tag)), indent=2, sort_keys=True))
            return 0

        state = client.get_state(tag, include_testpypi=False)
        if not state.tag_exists:
            raise ReleaseRollbackError(f"tag {tag} does not exist")
        validate_withdrawal_state(state)
        if not args.apply:
            print("Dry run completed; pass --apply and --confirm-tag to update release notes.")
            return 0
        if args.confirm_tag != tag:
            raise ReleaseRollbackError("--confirm-tag must exactly match --tag before applying")
        if not args.reason.strip():
            raise ReleaseRollbackError("withdrawal reason must be non-empty")
        if not client.token:
            raise ReleaseRollbackError("GH_TOKEN is required with --apply")
        apply_withdrawal_advisory(client, state, tag=tag, reason=args.reason)
        print(f"Withdrawal advisory applied to {tag}.")
        return 0
    except ReleaseRollbackError as error:
        print(f"release rollback error: {error}", file=sys.stderr)
        return 1
