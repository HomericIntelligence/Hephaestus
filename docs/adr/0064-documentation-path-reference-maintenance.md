# ADR-0064: Living documents follow the paths they name

- Status: Proposed
- Date: 2026-11-09
- Tracks: #3525, #3504

## Context

The restructure moves several modules and subpackages, and the living
documents (`AGENTS.md` and the `docs/` tree) name those paths. A move that
leaves a stale path in a living document misleads readers and agents. Today
no rule says who updates a document when a path moves, and no guard checks a
planned path in prose.

## Decision

1. A move updates every living document that names the moved path in the
   same change.
2. `AGENTS.md` structure-map rows come from the `hephaestus/` subpackage
   directories on disk. A row names a planned child only together with the
   issue that owns the move.
3. A future path (a path that does not exist yet) may appear in prose only
   when the same sentence names the owning issue, and the path is not a
   Markdown link.
4. The path-reference guard is AST-based. Issue #3505 owns it. The
   document-content guards for #3525 are prose-level checks in
   `tests/unit/docs/test_restructure_docs.py`.

## Alternatives considered

- Generate the `AGENTS.md` structure block. Rejected: the fence is
  hand-written prose with editorial notes, and a generator cannot produce
  the planned-child annotations.
- Run a periodic documentation audit. Rejected: drift persists between
  audits.

## Consequences

- Each move issue carries its own document edits.
- #3525 carries the cross-cutting ADRs, the structure map, the key-file
  list, and the document guards.
- A prose reference to a planned path without its owning issue fails the
  document guard.
