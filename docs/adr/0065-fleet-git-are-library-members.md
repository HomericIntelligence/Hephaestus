# ADR-0065: hephaestus.fleet and hephaestus.git are library members

- Status: Accepted
- Date: 2026-11-08
- Tracks: #3519, #3504

## Context

ADR-0001 makes `hephaestus.automation` the opt-in product layer. It also says
that library subpackages cannot import `hephaestus.automation`. ADR-0002 keeps
`import hephaestus` cheap with PEP 562 lazy imports.

The Fleet contained runtime, the build controller, and the worker entry points
were part of `hephaestus.automation`. Git execution, bounded Git capture, and
Git configuration classification were also in that package. These modules were
not tied to the queue pipeline. Fleet code needed the Git helpers, and the
automation pipeline also used the Git helpers. No import edge pointed from the
Git helpers into the product layer.

## Decision

1. `hephaestus/fleet/` holds the contained runtime, the build controller, the
   build artifacts, and the two console scripts `hephaestus-fleet-worker` and
   `hephaestus-fleet-build-artifacts`. It is a library member and does not
   import `hephaestus.automation`.
2. `hephaestus/git/` owns the Git execution helpers, the bounded capture
   helpers, and the Git configuration safety check. The product layer imports
   them, and the library layer can also import them.
3. The dependency arrows point only to the library:
   `automation → git`, `automation → fleet`, and `fleet → git`.
4. `hephaestus/fleet/__init__.py` holds a docstring only. Both packages stay
   out of the eager path, so `import hephaestus` loads neither package.
5. `tests/unit/validation/test_import_surface.py` and
   `tests/unit/git/test_git_package.py` guard the lazy import. The pdoc target
   list in `justfile` names `./hephaestus/fleet`.

## Consequences

- The Fleet runtime and the build controller remain available in the base
  install. Their third-party dependencies stay function-local, so the base
  import surface does not change.
- The two Fleet console scripts keep their names. Their entry points resolve
  into `hephaestus/fleet/`.
- A new library module must not import `hephaestus.automation`. The boundary
  guard in `tests/unit/validation/test_automation_boundary.py` reports a
  violation.
- Rollback is one revert of the move.
