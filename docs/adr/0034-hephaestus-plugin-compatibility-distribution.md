# ADR-0034: Hephaestus nested compatibility distribution stays package data only

- Status: Accepted
- Date: 2026-08-28
- Tracks: #2770

## Context

The Hephaestus repository used to ship a nested `plugins/hephaestus/`
compatibility package with 23 skill manifests, shared partials, and plugin
metadata. PR #2063 removed that tree when the runtime skills moved to the
Athena marketplace. Issue #2770 restores the nested tree for marketplace
compatibility and scanning, but it does not change the runtime skill source of
truth.

The repository already uses `.claude/settings.json` for runtime skill enablement
and Athena is the configured marketplace. The restored nested tree must stay as
packaged data only. It must not become a second runtime source, and it must not
be imported by the library package.

## Decision

Restore `plugins/hephaestus/` as a regular-file compatibility distribution.
Keep a package-local `README.md`, `LICENSE`, and `.codexignore` file. Keep the
historical 23 skill manifests and their shared resources, and add SPDX
`license:` frontmatter to each manifest.

Treat the nested tree as documentation and package content only:

- leave runtime skill enablement in `.claude/settings.json`;
- keep Athena as the only runtime marketplace;
- keep the nested tree out of the base `hephaestus` import surface; and
- keep the topology tests and package tests focused on package content, not on
  runtime discovery.

## Alternatives considered

- Leave the nested tree removed. Rejected because CodeQL still reports the
  marketplace-compliance alerts that this issue targets.
- Restore the nested tree and also enable it at runtime. Rejected because that
  would create a second source of truth for skills.
- Recreate the nested tree with symlinks to runtime files. Rejected because the
  package must stay self-contained and reproducible.

## Consequences

The repository keeps a compatibility copy of the historical skill payload for
scanners, package consumers, and archival validation. Runtime ownership stays
with Athena. The tree has its own metadata and ignore rules, so future removal
is a single boundary change: delete `plugins/hephaestus/`, remove this ADR, and
drop the topology exception that allows the nested package.
