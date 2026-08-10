# ADR-0025: Provider-neutral Athena and Mnemosyne semantics

- Status: Accepted
- Date: 2026-08-09
- Tracks: #2517
- Supersedes details in: ADR-0019, ADR-0020

## Context

ADR-0019 defined Mnemosyne as a repository dependency at
`~/.agent_brain/knowledge`, not as a Pi package. ADR-0020 identified the
pipeline stages that need Athena `advise` and `learn`, but the implementation
still submitted those paths as ordinary agent prompts and used legacy
marketplace and text-derived evidence.

Hephaestus needs Athena-equivalent behavior without broadening provider authority:
the host must bind Athena's pinned contract, resolve and validate
Mnemosyne through Athena's dependency-resolution rules, read selected corpus
entries as untrusted content, and treat learning as successful only when a
host-owned Mnemosyne pull request is read back.

Athena `advise` and `learn` are host-owned operations. They are not agent
prompts and do not execute through Claude, Codex, Pi, or another harness. The
Athena contract therefore must not depend on provider discovery, package
inventory, provider preflight, tool grants, session state, or OS-isolation
admission. Those checks apply only when a later job executes through its
selected provider.

## Decision

Hephaestus binds a provider-neutral packaged Athena manifest with an
`AthenaContractReceipt` containing the package repository, commit, and SHA-256
hashes for `skills/advise/SKILL.md`, `skills/learn/SKILL.md`, and
`docs/dependency-resolution.md`.

Loading this receipt requires no Athena checkout and no agent harness. An
explicit Athena checkout can be checked against the packaged hashes for audit
work, but this optional check is not a runtime precondition for `advise` or
`learn`. Pi keeps its separate package catalog and preflight contract for work
that explicitly executes through `--agent pi`; that catalog is not the trust
source for host-owned Athena operations.

Mnemosyne resolution uses `HOMERIC_INTELLIGENCE_MNEMOSYNE_OWNER` for explicit
owner trust, accepts same-owner forks only through maintained organization fork
gates, otherwise selects `HomericIntelligence/Mnemosyne`, and fails on API or
authentication ambiguity. The bound checkout path is `$HOME/.agent_brain/knowledge`.
Before any read or write, the binding rejects symlinks, dirty state, wrong
origin, unsafe Git configuration, non-fast-forward drift, and revision mismatch.

Selected advice corpus comes only from committed flat `skills/*.md` blobs at
the bound SHA. Notes files, subdirectories, symlinks, non-blobs, duplicates,
and more than five selected entries fail closed. Selected entries are read
completely and presented as untrusted context.

Learning success is a host-owned `LearnDeliveryReceipt`: signed and
DCO-attested commit, lease-protected push, PR URL/number, readback head SHA,
validation evidence, and final disposition. Local-only edits and model prose
are not success evidence.

Pipeline stages submit `AthenaSkillJob` for `advise` and `learn`. Workers route
that job only to an injected host executor and return typed receipts. An
`AthenaSkillJob` never reaches generic agent dispatch, even if the surrounding
pipeline selected Pi. Mnemosyne is never modeled or installed as a provider
package.

## Alternatives considered

- Keep prompt aliases for `advise` and `learn`. Rejected because prompt text
  cannot prove repository trust, corpus selection, or delivery.
- Require a Pi package preflight before host-owned Athena work. Rejected
  because it couples a provider-neutral host contract to an optional harness
  and blocks Claude or Codex automation when Pi is absent or not admitted.
- Treat Mnemosyne as a Pi package. Rejected by ADR-0019 and by Athena's
  dependency-resolution contract.
- Accept PR-looking model text as learning evidence. Rejected because durable
  learning must be host-owned and read back from the resolved repository.

## Consequences

The pipeline can pass structured Athena receipts between stages and fail closed
when contract, checkout, corpus, or delivery evidence is missing. Athena host
work remains available when every agent harness is unavailable. Provider
preflight failures affect only jobs that execute through that provider. Merge
and planning state depend on typed receipts rather than model prose.
