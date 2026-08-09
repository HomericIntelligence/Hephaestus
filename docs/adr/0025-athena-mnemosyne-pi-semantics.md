# ADR-0025: Athena/Mnemosyne Pi semantics

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

Pi needs Athena-equivalent behavior without broadening provider authority:
Hephaestus must bind Athena's pinned package contract, resolve and validate
Mnemosyne through Athena's dependency-resolution rules, read selected corpus
entries as untrusted content, and treat learning as successful only when a
host-owned Mnemosyne pull request is read back.

## Decision

Hephaestus binds the catalog-pinned Athena package with an
`AthenaContractReceipt` containing the package repository, commit, and SHA-256
hashes for `skills/advise/SKILL.md`, `skills/learn/SKILL.md`, and
`docs/dependency-resolution.md`.

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
that job to an injected host executor before generic agent dispatch and return
typed receipts. Pi grants for Athena skills are explicit: base read/search
tools plus the receipt-proven `skill:advise` or `skill:learn` command. Mnemosyne
is never modeled or installed as a Pi package.

## Alternatives considered

- Keep prompt aliases for `advise` and `learn`. Rejected because prompt text
  cannot prove repository trust, corpus selection, delivery, or Pi capability
  boundaries.
- Treat Mnemosyne as a Pi package. Rejected by ADR-0019 and by Athena's
  dependency-resolution contract.
- Accept PR-looking model text as learning evidence. Rejected because durable
  learning must be host-owned and read back from the resolved repository.

## Consequences

The pipeline can pass structured Athena receipts between stages and fail closed
when contract, checkout, corpus, delivery, or Pi capability evidence is missing.
Legacy prompt-based paths remain compatibility surfaces, but merge and planning
state now depend on typed receipts rather than model prose.
