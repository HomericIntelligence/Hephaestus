"""Provide internal, standard-library-only localization state and rendering.

English source templates are catalog keys and are the complete fallback.
Machine-readable values must not use this module.
"""

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType

_PERCENT_PLACEHOLDER = re.compile(
    r"""
    %
    (?:\((?P<name>[^)]+)\))?
    (?P<flags>[#0\- +]*)
    (?P<width>\*|\d+)?
    (?:\.(?P<precision>\*|\d+))?
    [hlL]?
    (?P<conversion>[diouxXeEfFgGcrsa])
    """,
    re.VERBOSE,
)


def _placeholder_signature(
    template: str,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Return the positional and named placeholder signatures."""
    positional: list[str] = []
    named: list[tuple[str, str]] = []
    cursor = 0
    while cursor < len(template):
        percent = template.find("%", cursor)
        if percent < 0:
            break
        if template.startswith("%%", percent):
            cursor = percent + 2
            continue
        match = _PERCENT_PLACEHOLDER.match(template, percent)
        if match is None:
            malformed = re.match(
                r"%(?:\([^)]+\))?[#0\-+]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?[A-Za-z]",
                template[percent:],
            )
            if malformed is not None or template.startswith("%(", percent):
                raise ValueError(f"invalid percent placeholder in template {template!r}")
            cursor = percent + 1
            continue
        cursor = match.end()
        conversion = match.group("conversion")
        for specifier in ("width", "precision"):
            if match.group(specifier) == "*":
                positional.append("*")
        name = match.group("name")
        if name is None:
            positional.append(conversion)
        else:
            named.append((name, conversion))
    if positional and named:
        raise ValueError("cannot mix positional and named placeholders")
    return tuple(positional), tuple(sorted(named))


def _validate_catalog(catalog: Mapping[str, str]) -> None:
    """Validate catalog value types and placeholder compatibility."""
    for source, translated in catalog.items():
        if not isinstance(source, str) or not isinstance(translated, str):
            raise TypeError("localization catalog keys and values must be strings")
        if _placeholder_signature(source) != _placeholder_signature(translated):
            raise ValueError(f"translation placeholder mismatch for {source!r}")


@dataclass(frozen=True, init=False)
class Localizer:
    """Translate English source templates through an immutable catalog."""

    _catalog: Mapping[str, str] = field(repr=False)

    def __init__(self, catalog: Mapping[str, str] | None = None) -> None:
        """Create a localizer from a defensive copy of a catalog."""
        if catalog is None:
            copied: dict[str, str] = {}
        elif not isinstance(catalog, Mapping):
            raise TypeError("localization catalog must be a mapping")
        else:
            copied = dict(catalog)
        _validate_catalog(copied)
        object.__setattr__(self, "_catalog", MappingProxyType(copied))

    def template(self, source: str, /) -> str:
        """Return a translated template or its English fallback."""
        return self._catalog.get(source, source)

    def text(self, source: str, /, *args: object, **values: object) -> str:
        """Translate and optionally format a user-facing template."""
        translated = self.template(source)
        if args and values:
            raise TypeError("cannot mix positional and named formatting values")
        if args:
            return translated % args
        if values:
            return translated % values
        return translated.replace("%%", "%")


_ENGLISH = Localizer()
_ACTIVE_LOCALIZER: ContextVar[Localizer] = ContextVar(
    "hephaestus_localizer",
    default=_ENGLISH,
)


def get_localizer() -> Localizer:
    """Return the active context-local localizer."""
    return _ACTIVE_LOCALIZER.get()


def text(source: str, /, *args: object, **values: object) -> str:
    """Render a Hephaestus-authored user-facing source template."""
    return get_localizer().text(source, *args, **values)


@contextmanager
def using_localizer(localizer: Localizer | Mapping[str, str]) -> Iterator[Localizer]:
    """Select a localizer temporarily for the current execution context."""
    selected = localizer if isinstance(localizer, Localizer) else Localizer(localizer)
    token = _ACTIVE_LOCALIZER.set(selected)
    try:
        yield selected
    finally:
        _ACTIVE_LOCALIZER.reset(token)
