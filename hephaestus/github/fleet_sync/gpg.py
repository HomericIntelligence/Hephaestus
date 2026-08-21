"""GPG signing identity resolution for fleet sync."""

from __future__ import annotations

import shlex
import subprocess

from hephaestus.config.child_environments import build_git_signing_env
from hephaestus.github.git_ops import git_config_get

DEFAULT_METADATA_TIMEOUT = 10


def _signing_key_uid_emails(*, metadata_timeout: int | None = None) -> list[str] | None:
    """Return the email addresses on the configured GPG signing key, lowercased.

    Reads ``git config user.signingkey`` and lists the UID emails on that key
    via ``gpg --list-keys --with-colons``. Returns ``None`` when the key cannot
    be determined, and an empty list when the key exposes no UID emails.
    """
    signing_key = (
        git_config_get("user.signingkey", timeout=metadata_timeout)
        if metadata_timeout is not None
        else git_config_get("user.signingkey")
    )
    if not signing_key:
        return None

    try:
        gpg_result = subprocess.run(
            ["gpg", "--list-keys", "--with-colons", signing_key],
            capture_output=True,
            text=True,
            check=False,
            timeout=(DEFAULT_METADATA_TIMEOUT if metadata_timeout is None else metadata_timeout),
            env=build_git_signing_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if gpg_result.returncode != 0:
        return None

    emails: list[str] = []
    for line in gpg_result.stdout.splitlines():
        fields = line.split(":")
        if not fields or fields[0] != "uid" or len(fields) < 10:
            continue
        uid = fields[9]
        start = uid.find("<")
        end = uid.find(">", start + 1)
        if start != -1 and end != -1:
            emails.append(uid[start + 1 : end].strip().lower())
    return emails


def _validate_resign_email(
    email: str,
    *,
    metadata_timeout: int | None = None,
    skip_email_key_check: bool = False,
) -> str:
    """Validate ``email`` matches the GPG signing key, then return it."""
    if skip_email_key_check:
        return email
    key_emails = _signing_key_uid_emails(metadata_timeout=metadata_timeout)
    if key_emails is None:
        return email
    if email.lower() not in key_emails:
        raise RuntimeError(
            f"fleet_sync: resign email {email!r} is not a UID on the configured "
            f"GPG signing key (key UIDs: {key_emails or 'none'}). Re-signing with "
            "this email would produce commits GitHub marks unverified, failing the "
            "`homeric-main-baseline` required-signatures rule at merge. Pass "
            "--resign-email with an address on the signing key, or use the explicit "
            "--skip-email-key-check override."
        )
    return email


def get_resign_email(
    *,
    resign_email: str | None = None,
    skip_email_key_check: bool = False,
    metadata_timeout: int | None = None,
) -> str:
    """Return the email address used to re-sign rebased commits.

    Raises:
        RuntimeError: If no signing email is configured.

    """
    if resign_email and resign_email.strip():
        return _validate_resign_email(
            resign_email.strip(),
            metadata_timeout=metadata_timeout,
            skip_email_key_check=skip_email_key_check,
        )
    for global_ in (True, False):
        email = (
            git_config_get("user.email", global_=global_, timeout=metadata_timeout)
            if metadata_timeout is not None
            else git_config_get("user.email", global_=global_)
        )
        if email:
            return _validate_resign_email(
                email,
                metadata_timeout=metadata_timeout,
                skip_email_key_check=skip_email_key_check,
            )
    raise RuntimeError(
        "fleet_sync: no resign email configured. Pass --resign-email <address> "
        "or configure git user.email before running."
    )


def get_resign_exec(
    *,
    resign_email: str | None = None,
    skip_email_key_check: bool = False,
    metadata_timeout: int | None = None,
) -> str:
    """Return the ``git commit --amend`` shell command used as ``rebase --exec``."""
    email = shlex.quote(
        get_resign_email(
            resign_email=resign_email,
            skip_email_key_check=skip_email_key_check,
            metadata_timeout=metadata_timeout,
        )
    )
    return f"git -c user.email={email} commit --amend --no-edit -S -s --reset-author"
