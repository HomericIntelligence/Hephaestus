# ADR-0042: Independent tool and model selection

- Status: Accepted
- Date: 2026-09-07
- Tracks: Operator request for arbitrary model names and role tool selection

## Context

Model catalogs and alias translation require code changes for new model names.
A global tool also prevents different pipeline roles from using different tools.

## Decision

Select the tool and model independently for each role. A role tool overrides
the global tool. Otherwise, retain tool detection. A role model overrides the
global model. Otherwise, use the selected tool default. A changed role tool
still inherits the global model. Accept literal model strings without catalogs,
alias translation, or automatic model tiers. Keep `MODEL[:EFFORT]` and each
provider's effort transport. Use a fallback model only when explicitly supplied.

Implementation helpers inherit the implementation selection. Session records
must retain compatible tool and model identity. Preserve provider admission,
unsupported-operation checks, and host ownership of advice and learning.

This decision supersedes model catalogs and alias translation in ADR-0035 and
ADR-0036. Their effort transport decisions remain applicable.

## Alternatives considered

Maintaining a larger catalog still requires changes for new models. Translating
models when a role changes tools can silently change the operator's selection.

## Consequences

New model names require no Hephaestus changes. New tools still require adapters.
Operators must replace former aliases with IDs their selected tool accepts.
Provider errors remain the authority for unavailable models. Durable review
sessions need an explicit reset when their tool or model selection changes.

A durable review cannot switch models on a quota failure, including its first
request. The failure propagates so that the recorded selection stays valid.

The migration removes four legacy model-shim imports from implementation,
comment classification, post-merge work, and PR review. The ADR-0017 import
allowlist is reduced by these four entries.
