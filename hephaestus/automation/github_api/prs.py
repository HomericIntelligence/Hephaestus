"""Pull-request lifecycle helpers."""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
from typing import Any, cast

import hephaestus.automation.github_api as _api
from hephaestus.github.auto_merge import defer_auto_merge
from hephaestus.utils.helpers import strip_null_bytes

_ACCEPTABLE_SIG_STATUSES = frozenset({"G", "U"})


def _gh_commit_is_verified(oid: str) -> bool:
    """Return True if GitHub reports *oid*'s signature as verified.

    The local ``git log --format=%G?`` check returns ``N`` (no signature) for a
    commit that is actually **SSH-signed** when the local checkout has no
    ``gpg.ssh.allowedSignersFile`` configured — git cannot verify SSH signatures
    without it. GitHub, however, validates the signature server-side and exposes
    the result at ``repos/{owner}/{repo}/commits/{sha}`` under
    ``.commit.verification.verified``. That flag is the source of truth at PR
    time (the same rationale that makes ``U`` acceptable above), so we consult
    it before declaring a policy violation. Any lookup failure returns False so
    the caller falls back to the strict local verdict (fail safe).
    """
    try:
        owner, name = _api.get_repo_info()
        result = _api._gh_call(
            [
                "api",
                f"repos/{owner}/{name}/commits/{oid}",
                "--jq",
                ".commit.verification.verified",
            ],
        )
        return (result.stdout or "").strip().lower() == "true"
    except Exception as exc:  # logged, treated as unverified
        _api.logger.warning("Could not confirm GitHub signature for %s: %s", oid[:10], exc)
        return False


def _assert_branch_commits_signed(branch: str, base: str = "main") -> None:
    """Raise if any commit on *branch* (since *base*) is unsigned or invalid.

    Uses ``git log --format='%H %G?'`` to enumerate commits and their signature
    status. The base ref is fetched first to ensure the range is meaningful in
    detached/shallow clones; failure to fetch is non-fatal because the existing
    local ref is sufficient when present.

    A commit whose local status is *not* acceptable (e.g. ``N`` for an
    SSH-signed commit the local checkout can't verify without
    ``gpg.ssh.allowedSignersFile``) is re-checked against GitHub's commit
    verification API before it is flagged — GitHub's ``verified`` flag is
    authoritative at PR time. Only commits that fail BOTH the local check and
    the API check are treated as policy violations.
    """
    # Best-effort fetch of the base ref. Don't fail signing checks just because
    # the operator is offline — the local base is usually fresh enough.
    with contextlib.suppress(Exception):
        _api.run(
            ["git", "fetch", "origin", base, "--quiet"],
            check=False,
            timeout=_api.gh_cli_timeout(),
        )

    # Enumerate commits over the branch range. At PR-create time the branch
    # typically exists only as origin/<branch> (its checkout lives in a separate
    # worktree), so vary BOTH the base and branch sides and take the first range
    # that resolves. A bare local <branch> is not guaranteed to exist in the
    # coordinator's CWD (#2108, same class as #1795/#2047).
    candidate_ranges = (
        f"origin/{base}..origin/{branch}",
        f"origin/{base}..{branch}",
        f"{base}..origin/{branch}",
        f"{base}..{branch}",
    )
    result = None
    for rev_range in candidate_ranges:
        attempt = _api.run(
            ["git", "log", "--format=%H %G?", rev_range],
            check=False,
            timeout=_api.gh_cli_timeout(),
        )
        if attempt.returncode == 0:
            result = attempt
            break

    if result is None:
        # No range resolved locally — the branch ref is unavailable in this CWD
        # (e.g. pushed to origin but checked out in a separate worktree). This is
        # a resolution failure, NOT a policy violation: never crash create_pr and
        # strand already-pushed, signed work. GitHub's server-side signature
        # verification and the pr-policy gate remain the backstop. (#2108)
        _api.logger.warning(
            "Could not resolve any commit range for branch %r (vs %s); "
            "skipping local sign check and deferring to GitHub verification.",
            branch,
            base,
        )
        return

    bad: list[tuple[str, str]] = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        oid, status = parts[0], parts[1].strip()
        if status not in _ACCEPTABLE_SIG_STATUSES:
            # Local git couldn't bless it — but it may be SSH-signed and simply
            # unverifiable here. Defer to GitHub's authoritative verdict before
            # flagging it as a policy violation.
            if _api._gh_commit_is_verified(oid):
                continue
            bad.append((oid, status))

    if bad:
        bad_str = ", ".join(f"{oid[:10]}={status!r}" for oid, status in bad)
        raise ValueError(
            f"Unsigned or invalid commits on branch {branch!r} (vs {base}): {bad_str}. "
            "Every commit MUST be cryptographically signed per repo policy."
        )


def _find_open_pr_for_head(branch: str, base: str) -> int | None:
    """Return the number of the single OPEN PR from ``branch`` into ``base``.

    Used by :func:`gh_pr_create` as an idempotency guard so a re-run on a
    branch that already has an open PR reuses it rather than creating a
    duplicate (issue #1018). A query or parse failure raises: treating an
    unknown result as no PR could leave a pre-existing auto-merge arm uncontained.

    Args:
        branch: Head branch name to look up.
        base: Required base branch for the PR.

    Returns:
        The PR number of the matching OPEN PR, or None.

    """
    try:
        result = _api._gh_call(
            [
                "pr",
                "list",
                "--head",
                branch,
                "--base",
                base,
                "--json",
                "number,state,baseRefName",
                "--limit",
                "10",
            ]
        )
        prs = json.loads(result.stdout or "[]")
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError) as e:
        raise RuntimeError(f"could not verify existing PR state for head {branch!r}") from e
    if not isinstance(prs, list):
        raise RuntimeError(f"could not verify existing PR state for head {branch!r}")
    open_pr_numbers: list[int] = []
    for pr in prs:
        if not isinstance(pr, dict):
            raise RuntimeError(f"could not verify existing PR state for head {branch!r}")
        state = pr.get("state")
        base_ref_name = pr.get("baseRefName")
        if not isinstance(state, str) or not isinstance(base_ref_name, str):
            raise RuntimeError(f"could not verify existing PR state for head {branch!r}")
        if base_ref_name != base:
            raise RuntimeError(f"could not verify existing PR state for head {branch!r}")
        if state.upper() == "OPEN":
            number = pr.get("number")
            if not isinstance(number, int) or number <= 0:
                raise RuntimeError(f"could not verify existing PR state for head {branch!r}")
            open_pr_numbers.append(number)
    if len(open_pr_numbers) > 1:
        raise RuntimeError(f"could not verify existing PR state for head {branch!r}")
    return open_pr_numbers[0] if open_pr_numbers else None


def gh_pr_create(
    branch: str,
    title: str,
    body: str,
    auto_merge: bool = False,
    base: str = "main",
) -> int:
    """Create a pull request.

    Enforces PR body and signing policy at creation time:

    1. *body* must contain a literal ``Closes #N`` line.
    2. Every commit on *branch* (vs *base*) must be cryptographically signed.

    ``auto_merge`` is retained for API compatibility but ignored during #2054's
    fail-closed bootstrap. #2055 restores automatic arming only after a
    head-bound strict-review proof.

    The CI gate (``.github/workflows/_required.yml`` job ``pr-policy``) and the
    PR review prompt re-check the same three properties, so a slip past one
    layer will surface at the next.

    Args:
        branch: Branch name
        title: PR title
        body: PR description
        auto_merge: Deprecated compatibility flag; ignored while #2054 is active.
        base: Base branch to compare against for signed-commit validation

    Returns:
        PR number

    Raises:
        ValueError: If *body* lacks ``Closes #N`` or *branch* has unsigned commits.
        RuntimeError: If the underlying ``gh`` CLI call fails.

    """
    # Policy gate #1: PR body must reference the closing issue.
    _api._assert_body_has_closes(body)

    # Policy gate #2: every commit on the branch must be signed.
    _api._assert_branch_commits_signed(branch, base=base)

    # Idempotency guard: if an OPEN PR already exists on this head, reuse it
    # instead of opening a duplicate. This is the single chokepoint that all
    # PR-creation callers funnel through, so it prevents the duplicate-PR
    # failure observed on issue #768 (issue #1018). A closed/merged-only head
    # still gets a fresh PR — the issue may legitimately need new work, and the
    # worktree manager already extends the remote branch's history.
    existing_open_pr = _api._find_open_pr_for_head(branch, base)
    if existing_open_pr is not None:
        _api.logger.info("Reusing existing open PR #%s on head %s", existing_open_pr, branch)
        if not defer_auto_merge(existing_open_pr, lambda args: _api._gh_call(args, check=False)):
            raise RuntimeError(
                f"could not verify auto-merge disabled for existing PR #{existing_open_pr}"
            )
        return existing_open_pr

    try:
        # Create PR
        with _api._body_file(body) as body_path:
            result = _api._gh_call(
                [
                    "pr",
                    "create",
                    "--head",
                    branch,
                    "--base",
                    base,
                    "--title",
                    # NUL in argv → ``ValueError: embedded null byte`` from gh's
                    # subprocess call before the child runs (#1661).
                    strip_null_bytes(title),
                    "--body-file",
                    body_path,
                ]
            )

        # Extract PR number from URL in output
        output = result.stdout.strip()
        try:
            # Try to extract number from URL (e.g., https://github.com/owner/repo/pull/123)
            match = re.search(r"/pull/(\d+)", output)
            pr_number = int(match.group(1)) if match else int(output.split("/")[-1])
        except (ValueError, IndexError) as e:
            raise RuntimeError(f"Failed to parse PR number from output: {output}") from e

        _api.logger.info("Created PR #%s", pr_number)

        if auto_merge:
            _api.logger.warning(
                "Ignoring auto_merge=True for PR #%s while the strict-review gate is unavailable",
                pr_number,
            )
        if not defer_auto_merge(pr_number, lambda args: _api._gh_call(args, check=False)):
            raise RuntimeError(f"could not verify auto-merge disabled for new PR #{pr_number}")

        return pr_number

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to create PR: {e}") from e


def fetch_open_prs() -> list[dict[str, Any]]:
    """Return every open PR's metadata via ``gh pr list`` (no row limit).

    Uses ``--limit 2147483647`` (INT_MAX) to honor the audit reviewer's
    'ALL open PRs' contract on repos with >200 open PRs. The gh CLI
    does not support a true no-cap sentinel; INT_MAX avoids pagination
    overhead while accommodating any realistic repo size.
    """
    result = _api._gh_call(
        [
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,headRefName,url,isDraft",
            "--limit",
            "2147483647",
        ]
    )
    return cast(list[dict[str, Any]], json.loads(result.stdout or "[]"))


def gh_pr_label_names(pr_number: int) -> list[str]:
    """Return the label names on a PR by number, best-effort (read-only).

    Fetches ``gh pr view <n> --json labels`` and normalizes the ``labels``
    array (each entry is a ``{"name": ...}`` dict) to a flat list of names.
    Any subprocess or JSON failure yields an empty list so callers can treat a
    fetch error as "no labels" without raising — mirroring
    ``pr_manager._pr_label_names`` (the ``_review_existing_pr`` seam) so
    pipeline seeding's ``--prs`` mapping shares its semantics without
    importing the ``pr_manager`` product module.

    Args:
        pr_number: GitHub PR number.

    Returns:
        The PR's label names, or an empty list on any fetch failure.

    """
    try:
        result = _api._gh_call(["pr", "view", str(pr_number), "--json", "labels"], check=False)
        pr = cast(dict[str, Any], json.loads(result.stdout or "{}"))
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as exc:
        _api.logger.warning("Could not fetch PR #%s labels: %s", pr_number, exc)
        return []
    labels = pr.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def gh_pr_state(pr_number: int) -> dict[str, Any] | None:
    """Return a PR's lifecycle state by number, best-effort (read-only).

    Fetches ``gh pr view <n> --json state,mergedAt`` so callers can
    distinguish merged/closed/open without importing the stage-runtime
    ``PipelineGitHub.gh_pr_state``. Mirrors that method's read exactly so
    pipeline seeding's ``--prs`` mapping shares its terminal-state semantics
    (see ``_terminal_pr_outcome``, ``pipeline/stages/base.py``).

    Args:
        pr_number: GitHub PR number.

    Returns:
        ``{"state": ..., "mergedAt": ...}`` on success, ``None`` on any
        fetch failure.

    """
    try:
        result = _api._gh_call(
            ["pr", "view", str(pr_number), "--json", "state,mergedAt"], check=False
        )
        data = cast(dict[str, Any], json.loads(result.stdout or "{}"))
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as exc:
        _api.logger.warning("Could not fetch PR #%s state: %s", pr_number, exc)
        return None
    return data if isinstance(data, dict) else None


def gh_current_login() -> str | None:
    """Return the authenticated GitHub login for the current ``gh`` token."""
    try:
        result = _api._gh_call(["api", "user", "--jq", ".login"], check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        _api.logger.warning("Could not determine current GitHub login: %s", exc)
        return None
    if result.returncode != 0:
        _api.logger.warning("Could not determine current GitHub login: %s", result.stderr or "")
        return None
    login = (result.stdout or "").strip()
    return login or None
