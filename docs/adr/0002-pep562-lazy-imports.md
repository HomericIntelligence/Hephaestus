# ADR-0002: PEP 562 lazy imports back the library/product boundary

- Status: Accepted
- Date: 2026-06-30
- Tracks: #1452

## Context

`import hephaestus` is the entry point every HomericIntelligence project pays
for. The library/product boundary established by
[ADR-0001](0001-automation-library-boundary.md) requires that the base import
surface stay cheap and MUST NOT transitively pull `curses`, `fcntl`,
`pydantic`, or any `hephaestus.automation.*` module into `sys.modules`.

Eagerly importing the eighteen library subpackages at the top of
`hephaestus/__init__.py` would defeat that invariant: a single subpackage that
imports `pydantic` (or any heavy/optional dependency) would bloat the base
install and break the boundary ADR-0001 depends on. At the same time, a
flat `import hephaestus; hephaestus.slugify(...)` convenience surface is part
of the library's POLA contract and cannot simply be dropped.

## Decision

Resolve the public symbol surface **lazily** via PEP 562 module-level hooks in
`hephaestus/__init__.py`:

1. `_LAZY_IMPORTS` (`hephaestus/__init__.py:34`) maps the full set of
   lazily-loaded public symbols (28 total) to their `(module, attribute)`
   origin. None of those modules is imported at package-load time.
2. `__getattr__` (`hephaestus/__init__.py:100`) resolves a name on first
   access, importing the backing module on demand and caching the symbol on
   the package namespace.
3. `__dir__` (`hephaestus/__init__.py:118`) exposes the lazy symbols to
   `dir()` / tab-completion so introspection tools see the full surface.
4. `_DEPRECATED_LAZY` (`hephaestus/__init__.py:91`) provides a parallel
   warning path: a deprecated symbol still resolves through `__getattr__` but
   emits a `DeprecationWarning` first (see `COMPATIBILITY.md`).

## Alternatives considered

- **Eager top-level imports.** Rejected: importing every subpackage at load
  time pulls `curses`/`fcntl`/`pydantic` into the base surface and breaks the
  ADR-0001 boundary that `tests/unit/test_import_surface.py` enforces.
- **Submodule-only access (`from hephaestus.utils import slugify`).** Rejected
  on POLA grounds: it removes the flat convenience surface that existing
  consumers rely on, with no boundary benefit over the lazy approach.

## Consequences

- `import hephaestus` stays fast and never imports the product layer; this is
  locked in by `tests/unit/test_import_surface.py`.
- Adding a new public symbol means adding one row to `_LAZY_IMPORTS`, not a new
  top-level import.
- Deprecations flow through `_DEPRECATED_LAZY` so removal is a documented,
  warned migration rather than a hard break.
