"""Shared secret-file detection constants for the automation package.

These patterns are used to identify files that should never be staged or
committed during automated workflows.  Centralising them here prevents
divergence between the modules that perform file-filtering (e.g.
``pr_manager``) and any future modules that need the same check.
"""

from __future__ import annotations

# Exact basenames that are always considered secrets regardless of extension.
SECRET_FILE_NAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".secret",
        "credentials.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)

# File extensions whose presence indicates a cryptographic key or certificate.
SECRET_FILE_EXTENSIONS: frozenset[str] = frozenset({".key", ".pem", ".pfx", ".p12"})
