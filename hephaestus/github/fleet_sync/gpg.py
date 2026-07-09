"""GPG signing identity resolution for fleet sync."""

from __future__ import annotations

import os
import shlex
import subprocess

from hephaestus.github.git_ops import git_config_get
from hephaestus.utils.helpers import METADATA_TIMEOUT


def _signing_key_uid_emails() -> list[str] | None:
    """Return the email addresses on the configured GPG signing key, lowercased.

    Reads ``git config user.signingkey`` and lists the UID emails on that key
    via ``gpg --list-keys --with-colons``. Returns ``None`` when the key cannot
    be determined, and an empty list when the key exposes no UID emails.
    """
    signing_key = git_config_get("user.signingkey")
    if not signing_key:
        return None

    try:
        gpg_result = subprocess.run(
            ["gpg", "--list-keys", "--with-colons", signing_key],
            capture_output=True,
            text=True,
            check=False,
            timeout=METADATA_TIMEOUT,
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


def _validate_resign_email(email: str) -> str:
    """Validate ``email`` matches the GPG signing key, then return it."""
    if os.environ.get("FLEET_SKIP_EMAIL_KEY_CHECK", "").strip():
        return email
    key_emails = _signing_key_uid_emails()
    if key_emails is None:
        return email
    if email.lower() not in key_emails:
        raise RuntimeError(
            f"fleet_sync: resign email {email!r} is not a UID on the configured "
            f"GPG signing key (key UIDs: {key_emails or 'none'}). Re-signing with "
            "this email would produce commits GitHub marks unverified, failing the "
            "pr-policy 'every commit is signed' check at merge. Set FLEET_GIT_EMAIL "
            "(or git config user.email) to an address on the signing key, or set "
            "FLEET_SKIP_EMAIL_KEY_CHECK=1 to bypass."
        )
    return email


def get_resign_email() -> str:
    """Return the email address used to re-sign rebased commits.

    Raises:
        RuntimeError: If no signing email is configured.

    """
    env = os.environ.get("FLEET_GIT_EMAIL", "").strip()
    if env:
        return _validate_resign_email(env)
    for global_ in (True, False):
        email = git_config_get("user.email", global_=global_)
        if email:
            return _validate_resign_email(email)
    raise RuntimeError(
        "fleet_sync: no resign email configured. Set FLEET_GIT_EMAIL=<address> "
        "or `git config --global user.email <address>` before running."
    )


def get_resign_exec() -> str:
    """Return the ``git commit --amend`` shell command used as ``rebase --exec``."""
    email = shlex.quote(get_resign_email())
    return f"git -c user.email={email} commit --amend --no-edit -S -s --reset-author"
