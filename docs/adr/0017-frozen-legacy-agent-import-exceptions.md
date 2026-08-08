# ADR-0017: Frozen legacy agent-import exceptions

- Status: Accepted
- Date: 2026-07-28
- Tracks: #2233
- Supersedes: ADR-0005 (direct-import prohibition only)

## Context

ADR-0005 records the runtime abstraction's intended endpoint: automation code
uses `hephaestus.agents.runtime` rather than Claude-specific helpers. The
current migration has not reached that endpoint. Existing automation
compatibility seams still directly import `claude_invoke` and `claude_models`.

ADR-0005 is Accepted and therefore immutable. Its direct-import consequence is
not an accurate description of the current migration state, so it must be
superseded rather than rewritten.

## Decision

The existing automation consumer/module pairs for `claude_invoke` and
`claude_models` are frozen migration exceptions. No direct
`claude_timeouts` consumer is approved. The exception is deliberately scoped
to whole consumer/module pairs; it does not freeze individual imported symbols
or call sites within an existing compatibility seam.

`tests/unit/validation/test_adr_0017_legacy_agent_import_pairs.py` derives all
direct Python import statements from ASTs and requires exact equality with this
frozen set. It resolves relative imports against the importing package, so an
equivalent relative spelling cannot bypass the policy. Dynamic import behavior
is not a direct-import exception and is deliberately outside this ADR's narrow,
syntactically enforceable policy. Each completed migration removes its source
import and its corresponding baseline entry together.

## Alternatives considered

- **Rewrite ADR-0005.** Rejected because Accepted ADRs are immutable historical
  records.
- **Freeze imported symbols and call counts.** Rejected because the migration
  exception is intentionally defined at the consumer/module boundary; a more
  granular policy would add brittle implementation detail without a current
  architectural need.
- **Rely on prose assertions.** Rejected because executable AST behavior is the
  single policy authority, while documentation structure and links have their
  own validation.

## Consequences

- The frozen consumer/module pair set may only shrink unless a future ADR
  changes this migration policy.
- New direct automation import pairs for a legacy module fail the executable
  guard, regardless of absolute or relative spelling.
- The automation-to-library dependency direction remains unchanged.
