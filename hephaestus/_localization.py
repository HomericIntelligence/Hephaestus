"""Internal stdlib-only localization state and rendering.

English source templates are catalog keys and therefore remain the complete
fallback. Machine-readable values must not be passed through this module.
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
    [#0\- +]*
    (?P<width>\*|\d+)?
    (?:\.(?P<precision>\*|\d+))?
    [hlL]?
    (?P<conversion>[diouxXeEfFgGcrsa%])
    """,
    re.VERBOSE,
)


def _placeholder_signature(
    template: str,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Return ordered positional and order-independent named placeholders."""
    positional: list[str] = []
    named: list[tuple[str, str]] = []
    cursor = 0
    while cursor < len(template):
        percent = template.find("%", cursor)
        if percent < 0:
            break
        match = _PERCENT_PLACEHOLDER.match(template, percent)
        if match is None:
            cursor = percent + 1
            continue
        cursor = match.end()
        conversion = match.group("conversion")
        if conversion != "%":
            if match.group("width") == "*":
                positional.append("*")
            if match.group("precision") == "*":
                positional.append("*")
            name = match.group("name")
            if name is None:
                positional.append(conversion)
            else:
                named.append((name, conversion))
    return tuple(positional), tuple(sorted(named))


def _validate_catalog(catalog: Mapping[str, str]) -> None:
    """Validate catalog key/value types and formatting compatibility."""
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
        """Create a localizer from a defensively copied catalog."""
        copied = dict(catalog or {})
        _validate_catalog(copied)
        object.__setattr__(self, "_catalog", MappingProxyType(copied))

    def template(self, source: str, /) -> str:
        """Return the translated template or its English source fallback."""
        return self._catalog.get(source, source)

    def text(self, source: str, /, *args: object, **values: object) -> str:
        """Translate and optionally interpolate a user-facing template."""
        translated = self.template(source)
        if args and values:
            raise TypeError("cannot mix positional and named formatting values")
        if args:
            return translated % args
        if values:
            return translated % values
        return translated


_ENGLISH = Localizer()
_ACTIVE_LOCALIZER: ContextVar[Localizer] = ContextVar("hephaestus_localizer", default=_ENGLISH)


def get_localizer() -> Localizer:
    """Return the active context-local localizer."""
    return _ACTIVE_LOCALIZER.get()


def text(source: str, /, *args: object, **values: object) -> str:
    """Render a Hephaestus-authored user-facing source template."""
    return get_localizer().text(source, *args, **values)


@contextmanager
def using_localizer(localizer: Localizer | Mapping[str, str]) -> Iterator[Localizer]:
    """Temporarily select a localizer for the current execution context."""
    selected = localizer if isinstance(localizer, Localizer) else Localizer(localizer)
    token = _ACTIVE_LOCALIZER.set(selected)
    try:
        yield selected
    finally:
        _ACTIVE_LOCALIZER.reset(token)
