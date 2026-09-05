"""Validate and match repository-selected branch-ruleset conditions."""

from __future__ import annotations

from fnmatch import fnmatchcase

_REPOSITORY_SELECTORS = frozenset({"repository_name", "repository_id", "repository_property"})
_ORGANIZATION_SELECTORS = frozenset(
    {"organization_name", "organization_id", "organization_property"}
)


def required_app_id(value: object) -> int | None:
    """Return one normalized required-check GitHub App binding."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        if value == -1:
            return None
        if value > 0:
            return value
    raise ValueError("GitHub App ID is malformed")


def _string_patterns(value: object, *, protected: bool = False) -> None:
    """Validate one name-selector object."""
    if not isinstance(value, dict):
        raise ValueError("ruleset name selector is malformed")
    allowed = {"include", "exclude", "protected"} if protected else {"include", "exclude"}
    if not set(value).issubset(allowed) or "include" not in value:
        raise ValueError("ruleset name selector is malformed")
    for key in ("include", "exclude"):
        patterns = value.get(key, [])
        if not isinstance(patterns, list) or not all(
            isinstance(pattern, str) and pattern for pattern in patterns
        ):
            raise ValueError("ruleset name selector is malformed")
    if "protected" in value and not isinstance(value["protected"], bool):
        raise ValueError("ruleset repository-name selector is malformed")


def _id_selector(value: object, key: str) -> None:
    """Validate one repository- or organization-ID selector."""
    if not isinstance(value, dict) or set(value) != {key}:
        raise ValueError("ruleset ID selector is malformed")
    ids = value[key]
    if (
        not isinstance(ids, list)
        or not ids
        or not all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in ids
        )
    ):
        raise ValueError("ruleset ID selector is malformed")


def _property_records(value: object) -> None:
    """Validate one repository- or organization-property selector."""
    if not isinstance(value, dict) or not set(value).issubset({"include", "exclude"}):
        raise ValueError("ruleset property selector is malformed")
    if "include" not in value:
        raise ValueError("ruleset property selector is malformed")
    for key in ("include", "exclude"):
        records = value.get(key, [])
        if not isinstance(records, list):
            raise ValueError("ruleset property selector is malformed")
        for record in records:
            if not isinstance(record, dict) or not {"name", "property_values"}.issubset(record):
                raise ValueError("ruleset property selector is malformed")
            if not set(record).issubset({"name", "property_values", "source"}):
                raise ValueError("ruleset property selector is malformed")
            if not isinstance(record["name"], str) or not record["name"]:
                raise ValueError("ruleset property selector is malformed")
            values = record["property_values"]
            if not isinstance(values, list) or not all(
                isinstance(item, str) and item for item in values
            ):
                raise ValueError("ruleset property selector is malformed")
            if record.get("source", "custom") not in {"custom", "system"}:
                raise ValueError("ruleset property selector is malformed")


def _validate_selector(name: str, value: object) -> None:
    """Validate one documented parent-ruleset scope selector."""
    if name in {"repository_name", "organization_name"}:
        _string_patterns(value, protected=name == "repository_name")
    elif name == "repository_id":
        _id_selector(value, "repository_ids")
    elif name == "organization_id":
        _id_selector(value, "organization_ids")
    elif name in {"repository_property", "organization_property"}:
        _property_records(value)
    else:  # pragma: no cover - callers constrain this name.
        raise ValueError("ruleset selector is unsupported")


def _ref_patterns(conditions: dict[str, object]) -> tuple[list[str], list[str]]:
    """Validate and return branch-ref include and exclude patterns."""
    ref_name = conditions.get("ref_name")
    if not isinstance(ref_name, dict) or set(ref_name) != {"include", "exclude"}:
        raise ValueError("ruleset ref-name conditions are malformed")
    includes = ref_name["include"]
    excludes = ref_name["exclude"]
    if not isinstance(includes, list) or not isinstance(excludes, list):
        raise ValueError("ruleset ref-name patterns are malformed")
    if not all(isinstance(pattern, str) and pattern for pattern in [*includes, *excludes]):
        raise ValueError("ruleset ref-name pattern is malformed")
    return includes, excludes


def _validate_scope(ruleset: dict[str, object], conditions: dict[str, object]) -> None:
    """Validate conditions that the repository-scoped endpoint already selected."""
    source_type = ruleset.get("source_type")
    keys = set(conditions) - {"ref_name"}
    if source_type == "Repository":
        if keys:
            raise ValueError("repository ruleset has parent-only conditions")
        return
    if source_type == "Organization":
        if len(keys) != 1 or not keys.issubset(_REPOSITORY_SELECTORS):
            raise ValueError("organization ruleset repository selector is malformed")
    elif source_type == "Enterprise":
        repository = keys & _REPOSITORY_SELECTORS
        organization = keys & _ORGANIZATION_SELECTORS
        if len(repository) != 1 or len(organization) != 1 or keys != repository | organization:
            raise ValueError("enterprise ruleset selectors are malformed")
    else:
        raise ValueError("ruleset source type is unsupported")
    for key in keys:
        _validate_selector(key, conditions[key])


def ruleset_applies(
    ruleset: dict[str, object],
    base_branch: str,
    default_branch: str,
) -> bool:
    """Return whether one repository-selected ruleset applies to the base ref."""
    conditions = ruleset.get("conditions")
    if not isinstance(conditions, dict) or "ref_name" not in conditions:
        raise ValueError("ruleset branch conditions are malformed")
    includes, excludes = _ref_patterns(conditions)
    _validate_scope(ruleset, conditions)
    ref = f"refs/heads/{base_branch}"
    default_ref = f"refs/heads/{default_branch}"

    def matches(pattern: str) -> bool:
        if pattern == "~DEFAULT_BRANCH":
            return ref == default_ref
        if pattern == "~ALL":
            return True
        return fnmatchcase(ref, pattern)

    return any(matches(pattern) for pattern in includes) and not any(
        matches(pattern) for pattern in excludes
    )
