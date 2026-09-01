# ADR-0036: Codex implementation process boundary

- Status: Accepted
- Date: 2026-08-31
- Tracks: #2887

## Context

An implementation agent can change source files in its bound worktree. Its
provider process also needs an authentication bridge and the admitted Athena
plugin. A disposable `CODEX_HOME` alone does not prevent a command from reading
other user files or Git worktree metadata.

## Decision

Codex implementation starts only with two independent boundaries.

The library-owned process-isolation module applies a macOS Seatbelt profile
before the provider starts. The profile denies access by default. It permits
read and write access only to the bound worktree and the temporary Codex
profile. It does not grant the Git common directory or other worktrees. The
provider transport can use the network. An unsupported host or an unavailable
Seatbelt executable stops the job before process creation.

The temporary `config.toml` selects the named automation Codex permission
profile. It extends `:workspace`, denies command network access,
admits only the required runtime and Athena artifact, and denies the copied
`auth.json` bridge. The implementation command does not pass legacy
`--sandbox` or `--add-dir` flags because those settings do not compose with
Codex permission profiles.

The profile is private, owner-only, and removed after the process exits. The
last-message file is inside that profile. Athena source hashing excludes only
generated `__pycache__` files; all source, metadata, mode, and link checks
remain part of the fixed artifact digest.

Codex session resume remains disabled for automation. An ordinary follow-up
starts a fresh isolated session. A resume-required request fails before the
provider process starts.

## Alternatives considered

- Trust a disposable `CODEX_HOME`: rejected because it does not deny host reads.
- Keep legacy workspace-write sandbox flags: rejected because they can add the
  Git common directory and override permission profiles.
- Create a new external isolation service: rejected because the existing
  Seatbelt mechanism gives the required host boundary on supported hosts.
- Permit an unsandboxed fallback: rejected because it would weaken the
  implementation authority boundary.

## Consequences

Codex implementation is intentionally unavailable where the host cannot prove
the required process boundary. The provider process receives a narrower and
deterministic filesystem view. Commands cannot read the authentication bridge,
shared Codex history, or unrelated worktrees. The host remains the authority
for plan admission, commit, and push validation.
