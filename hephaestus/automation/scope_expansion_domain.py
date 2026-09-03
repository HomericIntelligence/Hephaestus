"""Validated scope-expansion metadata shared by PR review and GitHub jobs."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TypeGuard

SCOPE_EXPANSION_MARKER_PREFIX = "<!-- hephaestus-scope-expansion:"
_SCOPE_EXPANSION_DOMAIN_SEPARATOR = "hephaestus-scope-expansion-v1"
_TITLE_MAX_CHARS = 120
_REASON_MAX_CHARS = 1_000
_PATH_MAX_CHARS = 240
_SOURCE_LINE_MAX = 1_000_000
_MAX_EXPANSIONS = 8
_MAX_REQUIRED_PATHS = 32
_MAX_ACCEPTANCE_CRITERIA = 16
_MAX_ACCEPTANCE_CHARS = 4_000
_MAX_CRITERION_CHARS = 500
_MAX_CANONICAL_BYTES = 16 * 1024
_RESERVED_CONTROL_RE = re.compile(
    r"(?i)"
    r"<!--|--!?>|"
    r"\bstate\s*:\s*implementation-(?:no-)?go\b|"
    r"\bstate\s*:\s*skip\b|"
    r"\bverdict\s*:\s*[^\r\n]*|"
    r"\bdecision\s*:\s*[^\r\n]*|"
    r"\bapproval\s*:\s*[^\r\n]*|"
    r"\brejection\s*:\s*[^\r\n]*|"
    r"\bimplementation[\s_-]+(?:approval|rejection)\b[^\r\n]*|"
    r"\bimplementation\s+(?:is\s+)?(?:approved|rejected|go|no[-\s]?go|nogo)\b|"
    r"\bcloses\s+#\d+\b|"
    r"\bfixes\s+#\d+\b|"
    r"\bresolves\s+#\d+\b|"
    r"\bblocks\s+pr\s+#\d+\b|"
    r"\bblocks\s+#\d+\b",
)


def _has_unicode_control(value: str) -> bool:
    """Return whether text contains a control or format code point."""
    return any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)


@dataclass(frozen=True, slots=True)
class ScopeExpansion:
    """Validated reviewer-discovered prerequisite that should ship separately."""

    title: str
    reason: str
    source_path: str
    source_line: int
    required_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Return one canonical JSON-safe snapshot of this expansion."""
        return {
            "title": self.title,
            "reason": self.reason,
            "source_path": self.source_path,
            "source_line": self.source_line,
            "required_paths": list(self.required_paths),
            "acceptance_criteria": list(self.acceptance_criteria),
        }


def _normalize_text(value: object, *, field_name: str, max_chars: int) -> str | None:
    """Normalize one reviewer-controlled text field and reject control tokens."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value).strip()
    if not text or len(text) > max_chars:
        return None
    if _has_unicode_control(text) or _RESERVED_CONTROL_RE.search(text):
        return None
    return text


def _normalize_single_path(value: object) -> str | None:
    """Return one safe repository-relative POSIX path."""
    if not isinstance(value, str):
        return None
    path = unicodedata.normalize("NFC", value).strip()
    if (
        not path
        or len(path) > _PATH_MAX_CHARS
        or path in {".", ".."}
        or path.startswith(("/", "./", ":"))
        or "\\" in path
        or "`" in path
        or _has_unicode_control(path)
        or _RESERVED_CONTROL_RE.search(path) is not None
    ):
        return None
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute() or "." in pure_path.parts or ".." in pure_path.parts:
        return None
    return path


def is_safe_scope_expansion_path(path: object) -> TypeGuard[str]:
    """Return whether *path* is safe to publish in a scope-expansion record."""
    return _normalize_single_path(path) is not None


def normalize_scope_expansion_paths(value: object) -> tuple[str, ...] | None:
    """Validate one non-empty safe path manifest and return a stable tuple."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    normalized = [_normalize_single_path(path) for path in value]
    if any(path is None for path in normalized):
        return None
    # The guard above narrows the runtime values but not the list element type
    # for static checkers, so retain the safe strings explicitly.
    safe_paths = [path for path in normalized if path is not None]
    unique = sorted(set(safe_paths))
    if len(unique) != len(normalized) or len(unique) > _MAX_REQUIRED_PATHS:
        return None
    return tuple(unique)


def normalize_scope_expansion_criteria(value: object) -> tuple[str, ...] | None:
    """Validate a bounded non-empty acceptance-criteria manifest."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        criterion = _normalize_text(
            item, field_name="acceptance_criteria", max_chars=_MAX_CRITERION_CHARS
        )
        if criterion is None or criterion in seen:
            return None
        seen.add(criterion)
        normalized.append(criterion)
    if len(normalized) > _MAX_ACCEPTANCE_CRITERIA:
        return None
    if sum(len(item) for item in normalized) > _MAX_ACCEPTANCE_CHARS:
        return None
    return tuple(sorted(normalized))


def normalize_scope_expansion(value: object) -> ScopeExpansion | None:
    """Validate and normalize one expansion mapping."""
    if not isinstance(value, Mapping):
        return None
    keys = set(value)
    required_keys = {"title", "reason", "required_paths", "acceptance_criteria"}
    path_keys = keys & {"source_path", "path"}
    line_keys = keys & {"source_line", "line"}
    if (
        not all(isinstance(key, str) for key in keys)
        or not required_keys.issubset(keys)
        or len(path_keys) != 1
        or len(line_keys) != 1
        or keys != required_keys | path_keys | line_keys
    ):
        return None
    title = _normalize_text(value.get("title"), field_name="title", max_chars=_TITLE_MAX_CHARS)
    reason = _normalize_text(value.get("reason"), field_name="reason", max_chars=_REASON_MAX_CHARS)
    source_path = _normalize_single_path(
        value.get("source_path") if value.get("source_path") is not None else value.get("path")
    )
    source_line = (
        value.get("source_line") if value.get("source_line") is not None else value.get("line")
    )
    required_paths = normalize_scope_expansion_paths(value.get("required_paths"))
    acceptance_criteria = normalize_scope_expansion_criteria(value.get("acceptance_criteria"))
    if (
        title is None
        or reason is None
        or source_path is None
        or isinstance(source_line, bool)
        or not isinstance(source_line, int)
        or not 1 <= source_line <= _SOURCE_LINE_MAX
        or required_paths is None
        or acceptance_criteria is None
    ):
        return None
    expansion = ScopeExpansion(
        title=title,
        reason=reason,
        source_path=source_path,
        source_line=source_line,
        required_paths=required_paths,
        acceptance_criteria=acceptance_criteria,
    )
    canonical = json.dumps(
        expansion.as_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return expansion if len(canonical.encode("utf-8")) <= _MAX_CANONICAL_BYTES else None


def normalize_scope_expansions(value: object) -> tuple[ScopeExpansion, ...] | None:
    """Validate a bounded list of independent scope expansions."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        return None
    if len(value) > _MAX_EXPANSIONS:
        return None
    normalized: list[ScopeExpansion] = []
    seen: set[tuple[object, ...]] = set()
    for item in value:
        expansion = normalize_scope_expansion(item)
        if expansion is None:
            return None
        key = (
            expansion.title,
            expansion.reason,
            expansion.source_path,
            expansion.source_line,
            expansion.required_paths,
            expansion.acceptance_criteria,
        )
        if key in seen:
            return None
        seen.add(key)
        normalized.append(expansion)
    return tuple(sorted(normalized, key=_expansion_sort_key))


def _expansion_sort_key(expansion: ScopeExpansion) -> tuple[object, ...]:
    """Return the stable sort key used for canonicalization and digests."""
    return (
        expansion.title,
        expansion.reason,
        expansion.source_path,
        expansion.source_line,
        expansion.required_paths,
        expansion.acceptance_criteria,
    )


def scope_expansion_canonical_json(
    repository: str, parent_issue: int, expansion: ScopeExpansion
) -> str:
    """Return the canonical JSON payload used for stable identity."""
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("repository must be a non-empty string")
    if isinstance(parent_issue, bool) or not isinstance(parent_issue, int) or parent_issue <= 0:
        raise ValueError("parent_issue must be a positive integer")
    payload = {
        "acceptance_criteria": list(expansion.acceptance_criteria),
        "parent_issue": parent_issue,
        "reason": expansion.reason,
        "required_paths": list(expansion.required_paths),
        "repository": repository.strip().lower(),
        "source_line": expansion.source_line,
        "source_path": expansion.source_path,
        "title": expansion.title,
    }
    return json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)


def scope_expansion_digest(repository: str, parent_issue: int, expansion: ScopeExpansion) -> str:
    """Return the deterministic identifier for one expansion."""
    canonical = scope_expansion_canonical_json(repository, parent_issue, expansion)
    seed = f"{_SCOPE_EXPANSION_DOMAIN_SEPARATOR}:{canonical}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def scope_expansion_marker(repository: str, parent_issue: int, expansion: ScopeExpansion) -> str:
    """Return the hidden HTML marker used to recover one expansion record."""
    digest = scope_expansion_digest(repository, parent_issue, expansion)
    return f"{SCOPE_EXPANSION_MARKER_PREFIX}{digest} -->"
