# ADR-0061: Filesystem mode and ownership are host policy

- Status: Accepted
- Date: 2026-11-02
- Tracks: #3564

## Context

The automation loop stopped when a filesystem value was not current, not owned
by this process, or not proven by a host receipt. Each stop stalled a work
item. The checks read `st_uid` and permission bits in the checkout, the state
directory, the receipt store, the Git toolchain, the event-log ancestry, the
Pyxis quota root, the Mnemosyne snapshot base, and the Pi settings reader.

A check of this kind reads a value that the host controls, not the value that
the process produced. The agent contract says: do not add a provenance or
attestation layer for ordinary inputs; trust a value that came from your own
process and validate it at the boundary where it enters. A group-writable
file, a shared parent directory, or an operator umask does not make a value
untrusted. It is a host layout decision.

The loop also refused a planning comment that another author wrote, and it
refused a duplicate marker alias. Both conditions left the item parked with a
recovery instruction that only an operator could follow. The host keeps no
second record of who wrote a planning comment, so the ownership test was
self-referential in the same way that ADR-0060 describes.

## Decision

1. Hephaestus does not refuse an operation because of a filesystem mode or an
   owner. The checks that only read `st_mode` permission bits or `st_uid` for
   this purpose are removed.
2. The guards that stay are the ones that answer a question about the value
   itself: the entry is a regular file or a directory, the size is within its
   cap, the file handle names the same object that was opened, and the path
   resolves inside the directory the operator named.
3. Comment identity is last-write-wins. The newest comment that carries one
   role marker is the pointer, whatever author wrote it. A duplicate warns and
   patches in place. No comment is deleted. When another author owns the
   pointer, the host leaves it in place and writes no duplicate.
4. Two refusals stay, because each one would corrupt the write that follows: a
   planning marker inside an immutable history artifact, and one outgoing body
   that carries both the plan and the review marker.
5. `gh` resolves from `PATH`, with an optional `--gh-extra-path-root` that is
   tried first.

The checks that gate destructive cleanup and the Codex isolation boundary keep
their owner test. Those checks decide whether the host may remove or hand over
another process's work, which is a question about the work, not about a mode.

## Alternatives considered

- Keep the checks and warn instead of refusing. Rejected: the warning has no
  recovery step that an operator can take, because the host layout is not the
  operator's fault. It adds noise to every run and changes no outcome.
- Keep the checks behind an opt-in strict flag. Rejected: no workflow needs it,
  and the flag would carry the same unverifiable assumption.
- Delete the guards entirely and rely on the operating system. Rejected for
  the bounded-regular-file and size checks, which stop a read that the caller
  cannot handle. Those checks stay.

## Consequences

- A shared check-out, a team umask, and a group-writable parent directory no
  longer stop the loop.
- The Pi settings reader no longer reads ownership or mode. A settings file
  that another account can write is read. The host operator owns that risk.
- The event-log and diagnostics probe asks Git instead of reading `.git`. When
  Git cannot answer, the probe logs a warning and treats the path as outside a
  worktree.
- `hephaestus/` holds no `0o022` check. A new mode check needs this ADR to
  change.
- A test does not refuse on a host filesystem mode or owner. A test may
  still confirm that the host mode is left unchanged.
