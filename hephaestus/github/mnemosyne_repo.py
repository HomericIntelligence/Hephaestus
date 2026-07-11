"""Resolve which Mnemosyne repository to clone, push to, and PR against.

Historically every path that touched Mnemosyne hardcoded
``HomericIntelligence/Mnemosyne`` as the remote, which meant only
members of that org could use the ``/advise`` and ``/learn`` skills (and the
automation pipeline) against a knowledge base.

This module makes the target portable. The resolution ladder is:

1. An ``override_owner`` (explicit arg or the ``HEPH_MNEMOSYNE_OWNER`` env var)
   always wins.
2. Otherwise use the ``gh``-authenticated login. If that login *is*
   ``HomericIntelligence`` (the upstream itself), clone upstream directly — you
   cannot fork a repo into its own org. If ``<login>/Mnemosyne`` already
   exists on GitHub, use it. Otherwise fork
   ``HomericIntelligence/Mnemosyne`` into the login's namespace and use
   the resulting fork.
3. If the login cannot be determined, fall back to the upstream slug.

``/learn`` pushes branches and opens PRs against the *resolved* slug (the fork
itself), making each user's knowledge base self-contained.

All ``gh`` invocations route through :func:`hephaestus.github.client.gh_call`
so they stay behind the rate-limit / circuit-breaker adapter. The resolver is
the single source of truth shared by ``hephaestus.automation.advise_runner`` and
mirrored (in bash) by the ``advise``/``learn`` skill definitions.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

from hephaestus.github.client import gh_call
from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT

logger = logging.getLogger(__name__)

UPSTREAM_OWNER = "HomericIntelligence"
MNEMOSYNE_REPO = "Mnemosyne"
UPSTREAM_SLUG = f"{UPSTREAM_OWNER}/{MNEMOSYNE_REPO}"

#: Environment variable that overrides the resolved owner. Mirrored by the
#: skill bash blocks so interactive and automated paths agree.
OWNER_ENV_VAR = "HEPH_MNEMOSYNE_OWNER"


@dataclass(frozen=True)
class MnemosyneTarget:
    """The resolved Mnemosyne repository to clone, push to, and PR against.

    Attributes:
        owner: The resolved owner (the ``gh`` login, an override, or
            ``HomericIntelligence`` when falling back to upstream).
        slug: ``owner/Mnemosyne`` — the clone / push / PR target.
        is_fork_of_upstream: True when ``owner`` is a fork of the upstream repo
            (i.e. not ``HomericIntelligence`` itself).

    """

    owner: str
    slug: str
    is_fork_of_upstream: bool


def _slug_for(owner: str) -> str:
    """Return ``owner/Mnemosyne`` for the given owner."""
    return f"{owner}/{MNEMOSYNE_REPO}"


def gh_authenticated_login(*, timeout: int = METADATA_TIMEOUT) -> str | None:
    """Return the ``gh``-authenticated user's login, or None on failure.

    Args:
        timeout: Per-call timeout for the ``gh api user`` probe.

    Returns:
        The login string (e.g. ``"mvillmow"``), or None if ``gh`` is not
        authenticated or the call fails.

    """
    try:
        result = gh_call(
            ["api", "user", "--jq", ".login"],
            check=False,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
        logger.warning("Failed to determine gh-authenticated login: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "gh api user failed (rc=%s); cannot determine login: %s",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return None
    login = (result.stdout or "").strip()
    return login or None


def remote_repo_exists(slug: str, *, timeout: int = METADATA_TIMEOUT) -> bool:
    """Return True when ``slug`` (``owner/repo``) exists on GitHub.

    Args:
        slug: The ``owner/repo`` slug to probe.
        timeout: Per-call timeout for the ``gh repo view`` probe.

    Returns:
        True if the repo is visible to the authenticated user, else False.

    """
    try:
        result = gh_call(
            ["repo", "view", slug, "--json", "name"],
            check=False,
            log_on_error=False,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
        logger.warning("Failed to check existence of %s: %s", slug, exc)
        return False
    return result.returncode == 0


def fork_upstream(owner: str, *, timeout: int = NETWORK_TIMEOUT) -> bool:
    """Fork ``HomericIntelligence/Mnemosyne`` into the gh user's namespace.

    ``gh repo fork`` always forks into the *authenticated* user's account, so
    ``owner`` is used only for logging/sanity — the caller is expected to pass
    the gh-authenticated login.

    Args:
        owner: The login the fork is expected to land under (for logging).
        timeout: Per-call timeout for the fork operation.

    Returns:
        True if the fork was created (or already existed), else False.

    """
    try:
        logger.info("Forking %s into %s...", UPSTREAM_SLUG, owner)
        result = gh_call(
            ["repo", "fork", UPSTREAM_SLUG, "--clone=false"],
            check=False,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
        logger.warning("Failed to fork %s: %s", UPSTREAM_SLUG, exc)
        return False
    if result.returncode != 0:
        logger.warning(
            "gh repo fork %s failed (rc=%s): %s",
            UPSTREAM_SLUG,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False
    logger.info("Fork of %s available under %s", UPSTREAM_SLUG, owner)
    return True


def _validate_owner(owner: str) -> str | None:
    """Return ``owner`` if it is a plausible GitHub login, else None.

    Guards against an override or login that contains a slash (already a slug)
    or whitespace, which would corrupt the constructed clone/push slug.
    """
    candidate = owner.strip()
    if not candidate or "/" in candidate or any(c.isspace() for c in candidate):
        logger.warning("Ignoring invalid Mnemosyne owner %r", owner)
        return None
    return candidate


def resolve_mnemosyne_target(
    *,
    override_owner: str | None = None,
    allow_fork: bool = True,
) -> MnemosyneTarget:
    """Decide which Mnemosyne repository to clone, push to, and PR against.

    See the module docstring for the full precedence ladder.

    Args:
        override_owner: Explicit owner to use. Falls back to the
            ``HEPH_MNEMOSYNE_OWNER`` env var when None. An override that resolves
            to ``HomericIntelligence`` (or any owner) is used verbatim, with no
            fork attempt.
        allow_fork: When True (default), fork the upstream repo if the gh user
            has no existing ``<login>/Mnemosyne``. When False, skip
            forking and fall back to upstream.

    Returns:
        The resolved :class:`MnemosyneTarget`.

    """
    raw_override = override_owner if override_owner is not None else os.environ.get(OWNER_ENV_VAR)
    if raw_override:
        owner = _validate_owner(raw_override)
        if owner:
            return MnemosyneTarget(
                owner=owner,
                slug=_slug_for(owner),
                is_fork_of_upstream=owner != UPSTREAM_OWNER,
            )

    login = gh_authenticated_login()
    if login:
        login = _validate_owner(login)

    if not login:
        logger.info("gh login unavailable; defaulting Mnemosyne target to %s", UPSTREAM_SLUG)
        return MnemosyneTarget(
            owner=UPSTREAM_OWNER,
            slug=UPSTREAM_SLUG,
            is_fork_of_upstream=False,
        )

    if login == UPSTREAM_OWNER:
        # Cannot fork a repo into its own org; clone upstream directly.
        return MnemosyneTarget(
            owner=UPSTREAM_OWNER,
            slug=UPSTREAM_SLUG,
            is_fork_of_upstream=False,
        )

    user_slug = _slug_for(login)
    if remote_repo_exists(user_slug):
        return MnemosyneTarget(owner=login, slug=user_slug, is_fork_of_upstream=True)

    if allow_fork and fork_upstream(login):
        return MnemosyneTarget(owner=login, slug=user_slug, is_fork_of_upstream=True)

    logger.info(
        "No %s and fork unavailable; defaulting Mnemosyne target to %s",
        user_slug,
        UPSTREAM_SLUG,
    )
    return MnemosyneTarget(
        owner=UPSTREAM_OWNER,
        slug=UPSTREAM_SLUG,
        is_fork_of_upstream=False,
    )
