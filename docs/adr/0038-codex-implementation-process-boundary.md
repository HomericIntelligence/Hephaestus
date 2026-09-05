# ADR-0038: Codex implementation process boundary

- Status: Accepted
- Date: 2026-09-04
- Tracks: #2887
- Extends: ADR-0028

## Context

A Codex implementation turn used the operator's shared Codex home directory.
The provider could read unrelated conversation history and job state. A turn
for issue #2472 then changed unrelated CI files before the host committed the
worktree. The host preserved the uncommitted worktree. No invalid commit was
created.

The automation loop must give a Codex writer only its bound worktree, the
approved plan, and an admitted Athena plugin. The provider requires a short
lived authentication bridge for its transport. A model command must not read
that bridge. The host must also stop a change outside the approved scope before
it commits or pushes it.

## Decision

Use a two-layer boundary for a Codex implementation turn.

```text
host -> Seatbelt process boundary -> Codex provider -> permission profile -> model command
        read: worktree, profile       network: provider only              auth.json: deny
        write: worktree, profile      write: bound worktree only           network: deny
```

1. Create an owner-only disposable profile for each turn. It contains the
   provider output, job-owned home and temporary roots, one copied regular
   authentication file, and the digest-pinned Athena plugin. Do not copy
   history, sessions, other plugins, MCP servers, connectors, applications,
   command launchers, or environment forwarding.
2. On macOS, start the exact non-link Codex executable through a deny-default
   Seatbelt profile. Permit reads and writes only for the bound worktree and
   disposable profile. Permit provider transport network access. Do not grant
   the Git common directory, the shared Codex home, another worktree, or a
   general user path. A host without this boundary fails before process start.
3. Generate one named Codex permission profile that extends `:workspace`.
   Deny command network access and deny command access to the exact copied
   authentication file. Do not combine this profile with legacy sandbox
   settings.
4. Start every Codex follow-up as a new isolated turn. Reject a resume-required
   request and skip Codex compaction. Do not add a session or workspace schema.
5. Freeze the canonical plan file claims at admission. A Codex job with no
   approved, non-empty file manifest fails before its writer starts. Before
   commit and push, the host checks unstaged, staged, untracked, and committed
   paths against that manifest and any host-accepted remediation paths.

The library owns child environments and the reusable macOS process boundary.
The automation product owns plan identity, path claims, remediation authority,
and publication checks. This preserves the dependency direction in ADR-0001.

## Alternatives considered

- **Use the shared Codex home.** Rejected: it exposes unrelated history and
  state to a bound automation turn.
- **Use only a Codex writable-root setting.** Rejected: it does not deny reads
  from the shared home or other user paths.
- **Use prompt instructions as the scope control.** Rejected: a prompt does
  not enforce provider or command access.
- **Resume a stored Codex session.** Rejected: the stored session is outside
  the disposable profile authority.
- **Use an unsupported host with a weaker fallback.** Rejected: the read
  boundary would not be proven.

## Consequences

- Codex implementation is available only on a macOS host that can prove both
  boundaries. Other providers keep their current behavior.
- A changed Codex executable, unsupported permission profile, changed Athena
  artifact, malformed plan manifest, or boundary failure stops the job before
  it can edit the worktree.
- The authentication bridge has a bounded lifetime and is removed with the
  disposable profile. Diagnostics must not include its contents or private
  profile paths.
- Rollout requires the isolated-profile and publication-scope test suites.
  Rollback disables Codex implementation dispatch. It must not restore shared
  history, unbound resume, command-readable credentials, or an unguarded host
  publication path.
