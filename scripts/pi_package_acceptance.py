#!/usr/bin/env python3
"""Collect reproducible acceptance evidence for Athena's native Pi package.

The collector performs only GitHub reads. It validates the exact commit-pinned
catalog, clean local checkouts, upstream release receipts, Athena's deterministic
archive, and a clean Pi RPC discovery run before generating untracked artifacts
beneath ``build/``.

Usage:
    uv run python scripts/pi_package_acceptance.py collect \
        --athena-checkout /path/to/Athena --implementation-pr 123 \
        --pi-bin /path/to/pi --output-dir build/pi-acceptance
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from hephaestus.github.client import gh_call

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPOSITORY_ROOT / "hephaestus" / "agents" / "pi_package_catalog.json"
ATHENA_REPOSITORY = "HomericIntelligence/Athena"
HEPHAESTUS_REPOSITORY = "HomericIntelligence/Hephaestus"
ATHENA_REMOTE = "https://github.com/HomericIntelligence/Athena.git"
HEPHAESTUS_REMOTE = "https://github.com/HomericIntelligence/Hephaestus.git"
ATHENA_ISSUE_URL = "https://github.com/HomericIntelligence/Athena/issues/61"
REQUIRED_COMMANDS = ("skill:advise", "skill:learn", "skill:pr-review")
PI_RESOURCE_FIELD = "".join(("p", "i"))
PI_TEST_DIRECTORY = ("tests", PI_RESOURCE_FIELD)
FORBIDDEN_MANIFEST_FIELDS = frozenset(
    {"dependencies", "optionalDependencies", "peerDependencies", "scripts"}
)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_ALLOWED_CHILD_ENV = (
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
)


class GitHubTransport(Protocol):
    """Minimal injectable GitHub REST transport."""

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Return the decoded response for one REST request."""


class GhGitHubTransport:
    """GitHub transport routed through Hephaestus's resilient ``gh`` adapter."""

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Execute one REST request and decode its JSON response."""
        arguments = ["api", "--method", method.upper(), path]
        for name, value in (body or {}).items():
            if not isinstance(value, str):
                raise TypeError(f"GitHub field {name!r} must be a string")
            arguments.extend(["--raw-field", f"{name}={value}"])
        result = gh_call(arguments)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"GitHub returned malformed JSON for {method} {path}") from exc


@dataclass(frozen=True)
class PackageIdentity:
    """Commit-pinned Athena package identity."""

    source: str
    version: str
    ref: str


@dataclass(frozen=True)
class Compatibility:
    """Explicit, separately installed Pi capability versions."""

    pi: str
    delegation: str
    web_access: str


@dataclass(frozen=True)
class UpstreamIdentity:
    """Athena publication receipts required by Hephaestus."""

    issue: str
    pull_request: str
    release_tag: str
    required_check: str


@dataclass(frozen=True)
class PackageCatalog:
    """Validated downstream Athena Pi package catalog."""

    schema_version: int
    package: PackageIdentity
    compatibility: Compatibility
    upstream: UpstreamIdentity

    @property
    def install_spec(self) -> str:
        """Return the immutable Pi installation specification."""
        return f"{self.package.source}@{self.package.ref}"


@dataclass(frozen=True)
class DiscoveryEvidence:
    """Observed clean-install Pi command inventory."""

    installed_commit: str
    commands: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveEvidence:
    """Observed Athena archive boundary."""

    sha256: str
    members: int


@dataclass(frozen=True)
class RemoteReceipts:
    """Validated GitHub facts bound to the accepted source revisions."""

    implementation_head: str
    implementation_url: str
    check_url: str


@dataclass(frozen=True)
class AcceptanceEvidence:
    """Complete observed acceptance payload written below ``build/``."""

    schema_version: int
    catalog_sha256: str
    package: dict[str, str]
    compatibility: dict[str, str]
    upstream: dict[str, str]
    implementation: dict[str, str | int]
    discovery: dict[str, str | list[str]]
    archive: dict[str, str | int]
    required_check: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable evidence document."""
        return asdict(self)


def _require_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a JSON object")
    return cast(dict[str, Any], value)


def _require_string(document: dict[str, Any], name: str, context: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}.{name} must be a non-empty string")
    return value


def load_catalog(path: Path) -> PackageCatalog:
    """Load and strictly validate the accepted Athena package catalog."""
    try:
        root = _require_object(json.loads(path.read_text(encoding="utf-8")), "catalog")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load package catalog {path}: {exc}") from exc
    if root.get("schema_version") != 1:
        raise ValueError("catalog.schema_version must equal 1")
    if "packages" in root:
        packages_data = _require_object(root.get("packages"), "catalog.packages")
        athena = _require_object(packages_data.get("athena"), "catalog.packages.athena")
        package = PackageIdentity(
            source=f"git:{_require_string(athena, 'repository', 'catalog.packages.athena')}",
            version=_require_string(athena, "version", "catalog.packages.athena"),
            ref=_require_string(athena, "commit", "catalog.packages.athena"),
        )
        if tuple(athena.get("commands", ())) != REQUIRED_COMMANDS:
            raise ValueError("catalog Athena commands must preserve the accepted raw identifiers")
    else:
        # Temporary compatibility for pre-#2516 acceptance evidence fixtures.
        package_data = _require_object(root.get("package"), "catalog.package")
        package = PackageIdentity(
            source=_require_string(package_data, "source", "catalog.package"),
            version=_require_string(package_data, "version", "catalog.package"),
            ref=_require_string(package_data, "ref", "catalog.package"),
        )
    if package.source != "git:github.com/HomericIntelligence/Athena":
        raise ValueError("catalog package source must be the canonical Athena Git source")
    if package.version != "v0.4.0":
        raise ValueError("catalog package version must be v0.4.0")
    if _COMMIT_RE.fullmatch(package.ref) is None:
        raise ValueError("catalog package ref must be a 40-character lowercase commit")

    compatibility_data = _require_object(root.get("compatibility"), "catalog.compatibility")
    if "packages" in root:
        pi_data = _require_object(compatibility_data.get("pi"), "catalog.compatibility.pi")
        packages_data = _require_object(root.get("packages"), "catalog.packages")
        delegation = _require_object(
            packages_data.get("pi-subagents"), "catalog.packages.pi-subagents"
        )
        web_access = _require_object(
            packages_data.get("pi-web-access"), "catalog.packages.pi-web-access"
        )
        compatibility = Compatibility(
            pi=(
                f"{_require_string(pi_data, 'npm_name', 'catalog.compatibility.pi')}@"
                f"{_require_string(pi_data, 'version', 'catalog.compatibility.pi')}"
            ),
            delegation=(
                f"{_require_string(delegation, 'name', 'catalog.packages.pi-subagents')}@"
                f"{_require_string(delegation, 'version', 'catalog.packages.pi-subagents')}"
            ),
            web_access=(
                f"{_require_string(web_access, 'name', 'catalog.packages.pi-web-access')}@"
                f"{_require_string(web_access, 'version', 'catalog.packages.pi-web-access')}"
            ),
        )
    else:
        compatibility = Compatibility(
            pi=_require_string(compatibility_data, "pi", "catalog.compatibility"),
            delegation=_require_string(compatibility_data, "delegation", "catalog.compatibility"),
            web_access=_require_string(compatibility_data, "web_access", "catalog.compatibility"),
        )
    expected_compatibility = Compatibility(
        pi="@earendil-works/pi-coding-agent@0.80.2",
        delegation="pi-subagents@0.37.2",
        web_access="pi-web-access@0.15.0",
    )
    if compatibility != expected_compatibility:
        raise ValueError("catalog compatibility pins do not match ADR-0019")

    upstream_data = _require_object(root.get("upstream"), "catalog.upstream")
    upstream = UpstreamIdentity(
        issue=_require_string(upstream_data, "issue", "catalog.upstream"),
        pull_request=_require_string(upstream_data, "pull_request", "catalog.upstream"),
        release_tag=_require_string(upstream_data, "release_tag", "catalog.upstream"),
        required_check=_require_string(upstream_data, "required_check", "catalog.upstream"),
    )
    if upstream.issue != ATHENA_ISSUE_URL:
        raise ValueError("catalog upstream issue must be Athena #61")
    if _pull_request_number(upstream.pull_request, ATHENA_REPOSITORY) <= 0:
        raise ValueError("catalog upstream pull request URL is invalid")
    if upstream.release_tag != package.version or upstream.required_check != "package":
        raise ValueError("catalog upstream release metadata is inconsistent")
    return PackageCatalog(1, package, compatibility, upstream)


def catalog_digest(path: Path) -> str:
    """Return the SHA-256 digest of the exact catalog bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pull_request_number(url: str, repository: str) -> int:
    parsed = urlparse(url)
    expected_path = f"/{repository}/pull/"
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        return -1
    if not parsed.path.startswith(expected_path):
        return -1
    suffix = parsed.path.removeprefix(expected_path)
    return int(suffix) if suffix.isdigit() else -1


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded child command without a shell and require success."""
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no diagnostic").strip()
        raise RuntimeError(f"command failed ({command[0]}): {detail[:1000]}")
    return result


def _normalize_remote(value: str) -> str:
    return value.strip().removesuffix("/")


def validate_checkout(path: Path, expected_remote: str, expected_head: str) -> None:
    """Require a clean Git checkout at the expected remote and exact commit."""
    if not path.is_dir():
        raise ValueError(f"checkout does not exist: {path}")
    remote = _run_command(["git", "-C", str(path), "remote", "get-url", "origin"]).stdout.strip()
    if _normalize_remote(remote) != _normalize_remote(expected_remote):
        raise ValueError(f"checkout origin does not match {expected_remote}")
    head = _run_command(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
    if head != expected_head:
        raise ValueError(f"checkout HEAD {head!r} does not match {expected_head}")
    status = _run_command(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"]
    ).stdout
    if status.strip():
        raise ValueError(f"checkout is not clean: {path}")


def _child_environment(agent_directory: Path) -> dict[str, str]:
    environment = {name: os.environ[name] for name in _ALLOWED_CHILD_ENV if name in os.environ}
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "PI_CODING_AGENT_DIR": str(agent_directory),
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }
    )
    return environment


def _find_installed_package(agent_directory: Path) -> Path:
    git_root = agent_directory / "git"
    matches: list[Path] = []
    if git_root.is_dir():
        for manifest_path in git_root.rglob("package.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict) and manifest.get(PI_RESOURCE_FIELD) == {
                "skills": ["./skills"]
            }:
                matches.append(manifest_path.parent.resolve())
    if len(matches) != 1:
        raise ValueError(f"clean Pi installation exposed {len(matches)} Athena package roots")
    return matches[0]


def _parse_rpc_commands(output: str, package_root: Path) -> tuple[str, ...]:
    response: dict[str, Any] | None = None
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("Pi RPC output contains malformed JSONL") from exc
        if (
            isinstance(entry, dict)
            and entry.get("id") == "skills"
            and entry.get("type") == "response"
            and entry.get("command") == "get_commands"
            and entry.get("success") is True
        ):
            response = entry
    if response is None:
        raise ValueError("Pi RPC did not return a successful get_commands response")
    data = _require_object(response.get("data"), "Pi RPC response.data")
    commands = data.get("commands")
    if not isinstance(commands, list):
        raise ValueError("Pi RPC response.data.commands must be a list")
    discovered: set[str] = set()
    for command in commands:
        if not isinstance(command, dict):
            raise ValueError("Pi RPC command entry must be an object")
        source_info = command.get("sourceInfo")
        if not isinstance(source_info, dict):
            continue
        base_dir = source_info.get("baseDir")
        if (
            command.get("source") == "skill"
            and source_info.get("origin") == "package"
            and isinstance(base_dir, str)
            and Path(base_dir).resolve() == package_root.resolve()
            and isinstance(command.get("name"), str)
        ):
            discovered.add(command["name"])
    missing = sorted(set(REQUIRED_COMMANDS).difference(discovered))
    if missing:
        raise ValueError(f"Pi package discovery is missing: {', '.join(missing)}")
    return tuple(sorted(set(REQUIRED_COMMANDS)))


def install_and_discover(catalog: PackageCatalog, pi_bin: Path) -> DiscoveryEvidence:
    """Install the exact package ref in a clean Pi directory and query RPC commands."""
    agent_directory = Path(tempfile.mkdtemp(prefix="hephaestus-athena-pi-acceptance-"))
    try:
        agent_directory.mkdir(parents=True, exist_ok=True)
        environment = _child_environment(agent_directory)
        _run_command(
            [str(pi_bin), "install", catalog.install_spec, "--no-approve"],
            env=environment,
        )
        package_root = _find_installed_package(agent_directory)
        installed_commit = _run_command(
            ["git", "-C", str(package_root), "rev-parse", "HEAD"], env=environment
        ).stdout.strip()
        if installed_commit != catalog.package.ref:
            raise ValueError("installed Athena checkout does not match the catalog commit")
        rpc = _run_command(
            [
                str(pi_bin),
                "--mode",
                "rpc",
                "--offline",
                "--no-session",
                "--no-context-files",
                "--no-approve",
            ],
            env=environment,
            input_text='{"id":"skills","type":"get_commands"}\n',
        )
        commands = _parse_rpc_commands(rpc.stdout, package_root)
        return DiscoveryEvidence(installed_commit=installed_commit, commands=commands)
    finally:
        shutil.rmtree(agent_directory, ignore_errors=True)


def validate_remote_receipts(
    catalog: PackageCatalog,
    implementation_pr: int,
    transport: GitHubTransport,
) -> RemoteReceipts:
    """Validate fixed GitHub identities and bind all receipts to exact commits."""
    if implementation_pr <= 0:
        raise ValueError("implementation PR number must be positive")
    issue = _require_object(
        transport.request("GET", f"/repos/{ATHENA_REPOSITORY}/issues/61"),
        "Athena issue response",
    )
    if issue.get("state") != "closed":
        raise ValueError("Athena issue #61 is not closed")

    upstream_pr_number = _pull_request_number(catalog.upstream.pull_request, ATHENA_REPOSITORY)
    upstream_pr = _require_object(
        transport.request("GET", f"/repos/{ATHENA_REPOSITORY}/pulls/{upstream_pr_number}"),
        "Athena pull request response",
    )
    if upstream_pr.get("merged") is not True:
        raise ValueError("Athena package pull request is not merged")
    if upstream_pr.get("merge_commit_sha") != catalog.package.ref:
        raise ValueError("Athena pull request merge commit does not match catalog ref")

    release_commit = _require_object(
        transport.request(
            "GET",
            f"/repos/{ATHENA_REPOSITORY}/commits/{catalog.upstream.release_tag}",
        ),
        "Athena release tag response",
    )
    if release_commit.get("sha") != catalog.package.ref:
        raise ValueError("Athena release tag does not peel to the catalog ref")

    checks = _require_object(
        transport.request(
            "GET",
            f"/repos/{ATHENA_REPOSITORY}/commits/{catalog.package.ref}/check-runs?per_page=100",
        ),
        "Athena check-runs response",
    )
    check_runs = checks.get("check_runs")
    if not isinstance(check_runs, list):
        raise ValueError("Athena check-runs response must contain a list")
    matching = [
        check
        for check in check_runs
        if isinstance(check, dict)
        and check.get("name") == catalog.upstream.required_check
        and check.get("head_sha") == catalog.package.ref
        and check.get("status") == "completed"
        and check.get("conclusion") == "success"
        and isinstance(check.get("html_url"), str)
    ]
    if len(matching) != 1:
        raise ValueError("Athena package check-runs do not contain one successful package check")

    implementation = _require_object(
        transport.request("GET", f"/repos/{HEPHAESTUS_REPOSITORY}/pulls/{implementation_pr}"),
        "Hephaestus pull request response",
    )
    body = implementation.get("body")
    if not isinstance(body, str) or ATHENA_ISSUE_URL not in body:
        raise ValueError("Hephaestus pull request does not link Athena #61")
    if "Closes #2515" not in body.splitlines():
        raise ValueError("Hephaestus pull request lacks the literal Closes #2515 line")
    head = _require_object(implementation.get("head"), "Hephaestus pull request head")
    head_sha = _require_string(head, "sha", "Hephaestus pull request head")
    if _COMMIT_RE.fullmatch(head_sha) is None:
        raise ValueError("Hephaestus pull request head is not a full commit")
    implementation_url = _require_string(
        implementation, "html_url", "Hephaestus pull request response"
    )
    return RemoteReceipts(
        implementation_head=head_sha,
        implementation_url=implementation_url,
        check_url=cast(str, matching[0]["html_url"]),
    )


def inspect_athena_archive(athena_checkout: Path) -> ArchiveEvidence:
    """Build and inspect Athena's deterministic release archive."""
    package_script = athena_checkout / "scripts" / "package_plugin.py"
    _run_command(
        [sys.executable, str(package_script), "--root", str(athena_checkout)],
        cwd=athena_checkout,
    )
    archives = sorted((athena_checkout / "dist").glob("athena-plugin-0.4.0.tar.gz"))
    if len(archives) != 1:
        raise ValueError("Athena package build did not produce one v0.4.0 archive")
    archive_path = archives[0]
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            manifest_member = archive.extractfile("package.json")
            if manifest_member is None:
                raise ValueError("Athena archive lacks package.json")
            manifest = _require_object(json.load(manifest_member), "archived package.json")
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot inspect Athena archive: {exc}") from exc
    required_members = {
        "package.json",
        "skills/advise/SKILL.md",
        "skills/learn/SKILL.md",
        "skills/pr-review/SKILL.md",
    }
    if len(names) != len(set(names)):
        raise ValueError("Athena archive contains duplicate members")
    if not required_members.issubset(names):
        raise ValueError("Athena archive lacks required native Pi skill resources")
    if manifest.get("name") != "@homericintelligence/athena":
        raise ValueError("Athena archive package name is invalid")
    if manifest.get("version") != "0.4.0":
        raise ValueError("Athena archive package version is invalid")
    if manifest.get(PI_RESOURCE_FIELD) != {"skills": ["./skills"]}:
        raise ValueError("Athena archive does not expose only canonical skills")
    forbidden_fields = sorted(FORBIDDEN_MANIFEST_FIELDS.intersection(manifest))
    if forbidden_fields:
        raise ValueError(
            f"Athena archive manifest bundles forbidden fields: {', '.join(forbidden_fields)}"
        )
    for member in members:
        name = member.name
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Athena archive contains an unsafe path: {name}")
        if member.issym() or member.islnk():
            raise ValueError(f"Athena archive contains a link: {name}")
        if not member.isfile() and not member.isdir():
            raise ValueError(f"Athena archive contains a special member: {name}")
        if "node_modules" in path.parts or path.parts[:2] == PI_TEST_DIRECTORY:
            raise ValueError(f"Athena archive contains a companion/test package: {name}")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    return ArchiveEvidence(sha256=digest, members=len(members))


def prepare_output_directory(repo_root: Path, output_dir: Path) -> Path:
    """Resolve a non-symlink output directory strictly beneath ``build/``."""
    repo_root = repo_root.resolve()
    candidate = output_dir if output_dir.is_absolute() else repo_root / output_dir
    candidate = candidate.absolute()
    build_root = (repo_root / "build").resolve()
    if candidate == build_root or not candidate.is_relative_to(build_root):
        raise ValueError("acceptance output must be beneath build/")
    current = build_root
    for part in candidate.relative_to(build_root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"acceptance output contains a symlink component: {current}")
    resolved = candidate.resolve(strict=False)
    if resolved == build_root or not resolved.is_relative_to(build_root):
        raise ValueError("acceptance output must be beneath build/")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def atomic_write(path: Path, content: str) -> None:
    """Atomically replace one UTF-8 artifact using a temporary sibling file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def render_issue_comment(evidence: dict[str, Any]) -> str:
    """Render the exact issue comment derived from observed evidence."""
    package = _require_object(evidence.get("package"), "evidence.package")
    implementation = _require_object(evidence.get("implementation"), "evidence.implementation")
    discovery = _require_object(evidence.get("discovery"), "evidence.discovery")
    archive = _require_object(evidence.get("archive"), "evidence.archive")
    check = _require_object(evidence.get("required_check"), "evidence.required_check")
    commands = discovery.get("commands")
    if not isinstance(commands, list) or not all(isinstance(command, str) for command in commands):
        raise ValueError("evidence.discovery.commands must be a string list")
    return "\n".join(
        [
            "<!-- hephaestus-pi-package-acceptance:athena-v0.4.0 -->",
            "## Athena native Pi package acceptance",
            "",
            f"- Package: `{package.get('source')}@{package.get('ref')}`",
            f"- Version metadata: `{package.get('version')}`",
            f"- Upstream: {ATHENA_ISSUE_URL}",
            f"- Implementation PR: {implementation.get('url')}",
            f"- Required check: {check.get('url')}",
            f"- Installed commit: `{discovery.get('installed_commit')}`",
            f"- Discovered commands: {', '.join(f'`{command}`' for command in commands)}",
            f"- Archive SHA-256: `{archive.get('sha256')}` ({archive.get('members')} members)",
            "",
            (
                "This comment was generated from observed collector results; "
                + "the JSON evidence remains untracked."
            ),
            "",
        ]
    )


def collect_acceptance(
    *,
    athena_checkout: Path,
    implementation_pr: int,
    output_dir: Path,
    pi_bin: Path,
    transport: GitHubTransport,
) -> AcceptanceEvidence:
    """Collect, validate, and write complete Athena Pi acceptance evidence."""
    catalog = load_catalog(CATALOG_PATH)
    destination = prepare_output_directory(REPOSITORY_ROOT, output_dir)
    receipts = validate_remote_receipts(catalog, implementation_pr, transport)
    validate_checkout(athena_checkout, ATHENA_REMOTE, catalog.package.ref)
    validate_checkout(REPOSITORY_ROOT, HEPHAESTUS_REMOTE, receipts.implementation_head)
    archive = inspect_athena_archive(athena_checkout)
    discovery = install_and_discover(catalog, pi_bin)
    evidence = AcceptanceEvidence(
        schema_version=1,
        catalog_sha256=catalog_digest(CATALOG_PATH),
        package=asdict(catalog.package),
        compatibility=asdict(catalog.compatibility),
        upstream=asdict(catalog.upstream),
        implementation={
            "repository": HEPHAESTUS_REPOSITORY,
            "pull_request": implementation_pr,
            "head": receipts.implementation_head,
            "url": receipts.implementation_url,
        },
        discovery={
            "installed_commit": discovery.installed_commit,
            "commands": list(discovery.commands),
        },
        archive={"sha256": archive.sha256, "members": archive.members},
        required_check={"name": catalog.upstream.required_check, "url": receipts.check_url},
    )
    document = evidence.to_dict()
    atomic_write(
        destination / "acceptance.json",
        json.dumps(document, indent=2, sort_keys=True) + "\n",
    )
    atomic_write(destination / "issue-comment.md", render_issue_comment(document))
    return evidence


def build_parser() -> argparse.ArgumentParser:
    """Build the collector command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect", help="collect acceptance evidence")
    collect.add_argument("--athena-checkout", type=Path, required=True)
    collect.add_argument("--implementation-pr", type=int, required=True)
    collect.add_argument("--pi-bin", type=Path, required=True)
    collect.add_argument("--output-dir", type=Path, default=Path("build/pi-acceptance"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the acceptance collector CLI."""
    arguments = build_parser().parse_args(argv)
    try:
        collect_acceptance(
            athena_checkout=arguments.athena_checkout,
            implementation_pr=arguments.implementation_pr,
            output_dir=arguments.output_dir,
            pi_bin=arguments.pi_bin,
            transport=GhGitHubTransport(),
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote observed acceptance evidence beneath {arguments.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
