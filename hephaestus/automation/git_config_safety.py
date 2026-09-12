"""Classify repository-local Git configuration that can change trusted commands."""

from __future__ import annotations


def unsafe_local_git_config_key(config: str) -> str | None:  # noqa: C901
    """Return an unsafe repository or worktree config key, if one exists."""
    for entry in config.split("\0"):
        if not entry:
            continue
        key, _separator, _value = entry.partition("\n")
        normalized = key.lower()
        if normalized in {
            "core.askpass",
            "core.attributesfile",
            "core.excludesfile",
            "core.fsmonitor",
            "core.gitproxy",
            "core.hookspath",
            "core.pager",
            "core.sshcommand",
            "core.worktree",
        }:
            return key
        if normalized in {"diff.external", "interactive.difffilter"}:
            return key
        if normalized.startswith("diff.") and normalized.rsplit(".", 1)[-1] in {
            "command",
            "textconv",
        }:
            return key
        if normalized == "credential.helper" or (
            normalized.startswith("credential.") and normalized.endswith(".helper")
        ):
            return key
        if normalized.startswith("remote.") and normalized.rsplit(".", 1)[-1] in {
            "proxy",
            "proxyauthmethod",
            "pushurl",
            "receivepack",
            "uploadpack",
        }:
            return key
        if normalized in {"fetch.recursesubmodules", "submodule.recurse"}:
            return key
        if normalized.startswith(("include.", "includeif.")):
            return key
        if normalized.startswith("filter.") and normalized.rsplit(".", 1)[-1] in {
            "clean",
            "process",
            "smudge",
        }:
            return key
        if normalized.startswith("merge.") and normalized.endswith(".driver"):
            return key
        # A checkout-specific URL rewrite can transform the validated literal
        # GitHub origin when it is later passed to ``git fetch``. Any local HTTP
        # configuration can proxy traffic or change TLS verification and trust.
        if normalized.startswith(("http.", "url.")):
            return key
    return None
