# Fleet source snapshots

## Scope and ownership

`hephaestus.automation.fleet_snapshot` supplies an optional Python API for
local source export, verification, and checked restore. Use it through the
`HomericIntelligence-Hephaestus[automation]` profile. It produces source for a
later subordinate build. Agamemnon retains admission and the parent issue
writer. This API does not start a build, fetch a reference, or register work.

The caller must hold the exclusive source and construction leases. Output
parents must be canonical directories owned by the current user, with mode
`0700`. Keep other writers out during capture, verification, and transfer.
Retain the restored workspace lease through its later use. Descriptor and
inode checks do not exclude another process that can replace a trusted parent.
These functions do not create a lease or a new ownership authority.

The existing recovery API retains its `index_sha256`, `worktree_sha256`, and
`untracked_sha256` contract. Transferable snapshots reuse supported capture
bindings from `worktree_snapshot`; they do not replace recovery digests or
change recovery behavior.

## Public API

The snapshot module exposes `SnapshotPolicy`, `SnapshotError`, and these calls:

| Call | Successful result |
| --- | --- |
| `export_snapshot(source, artifact, *, reference, policy, timeout=30)` | The six-field commitment for the written artifact. |
| `verify_snapshot(artifact, *, commitment, policy, timeout=30)` | `None`, after the actual artifact bytes pass verification. |
| `restore_snapshot(artifact, destination, *, commitment, policy, timeout=30)` | The new destination path, after its bytes and modes pass verification. |

Pass absolute canonical `Path` objects. Export and restore require new output
paths outside their source or input artifact. They do not overwrite existing
caller directories. Verification creates no files or temporary restored tree.
It uses the same bounded artifact decoder as restore.

`SnapshotPolicy(max_members, max_bytes, private_paths=())` supplies explicit
operator limits and private relative paths. These values are not requester
overrides. Runtime configuration does not come from `HEPH_*` variables.

## Regular-file profile and admission

Capture current tracked bytes and untracked files that Git does not ignore.
The current index defines which files are tracked. After index removal, Git
ignore rules apply to any file that remains on disk.
Staged and unstaged changes both use the current working file. A tracked
deletion is absent from the effective inventory. Include empty files when the
total source byte count is positive. Compare the base commit, index, selected
paths, and content identity during capture. Reject changed source.

The native profile requires POSIX descriptor-relative filesystem operations.
It refuses an unavailable filesystem capability or trusted Git executable.
Bounded configuration inspection uses the existing unsafe-Git-configuration
classification and does not follow configuration includes. Unsafe or
unsupported repository configuration prevents capture. An isolated child
environment alone does not establish repository safety.

Read regular files without following links. Reject selected symbolic links,
hard links, submodules, special files, and special permission bits. Preserve
allowed regular-file permission bits, including executable bits. Member names
must be canonical relative POSIX paths with portable ASCII components. Reject
case-fold collisions and names that the archive cannot represent. Check each
implicit directory as well as each file. A name cannot identify both a file
and a directory, and shared directories must use the same spelling.

Directories are implicit, with mode `0700`. Empty directories, ownership,
timestamps, and extended attributes are not source members. The snapshot file
policy is separate from recovery's supported link and content-identity rules.

The versioned policy excludes Git administration, `.fleet-runtime`, provider
runtime roots, and known credential paths. Declare other private relative
paths through `private_paths`. Excluded untracked paths are omitted before
their contents are read. An excluded tracked path causes failure; it cannot
silently produce a partial tracked tree. This path policy cannot identify a
secret in an ordinary source file. The caller must select source suitable for
transfer and keep the separate credential-scanning gate.

## Bounds and deadlines

| Input or operation | First-profile bound |
| --- | --- |
| `max_members` | Positive integer, at most 10,000 regular files. |
| `max_bytes` | Positive integer, at most 64 MiB of source payload. |
| `private_paths` | At most 100 unique canonical relative paths. |
| Member path | At most 240 bytes; portable ASCII only. |
| Filesystem scan | At most 40,000 entries. |
| Manifest and each captured Git output | At most 4 MiB each. |
| `timeout` | Finite number greater than zero and at most 300 seconds; default 30. |

Boolean values are invalid limits or timeouts. The archive has a separate
bound derived from member count, payload bytes, and fixed archive overhead.
These bounds do not grant build capacity or replace registered recipe limits.

One operation deadline covers admission, Git capture, reads, archive work, and
final checks. Pass the remaining caller budget. An inherited operation
deadline can shorten the supplied timeout. A phase must not reset that budget.
Inherited cancellation also applies while a Git child waits to exit after
its output pipes close.
Verification does not renew a submission deadline; the caller must check its
remaining time before the next operation.

## Artifact and commitment

The private artifact contains exactly `manifest.json` and `source.tar`.
Membership checks read entries incrementally under the operation deadline.
They stop at the first unexpected entry and read at most three entries.
The manifest has exactly `schema`, `baseCommit`, `policyDigest`, and `files`.
Its schema is `hi/hephaestus/source-snapshot/v1`. Each file entry has exactly
`path`, `mode`, `size`, and `sha256`, sorted by encoded path.

Canonical JSON uses sorted keys, compact separators, UTF-8, and one final line
feed. Counts, sizes, and modes are integers; booleans and floating-point values
are invalid. The versioned policy uses
`hi/hephaestus/source-snapshot-policy/v1`; its complete canonical document is
the input to `policyDigest`.

The archive is uncompressed USTAR. It contains regular members in manifest
order, zero timestamps and numeric owner IDs, and empty owner names. It has no
links or extension records. Identical eligible source, base commit, and policy
produce identical artifact bytes.

| Commitment field | Meaning |
| --- | --- |
| `reference` | Opaque private object ID; not a path or URL. |
| `manifestDigest` | SHA-256 of the complete canonical manifest bytes. |
| `baseCommit` | Captured source HEAD, as a lowercase 40-character Git commit ID. |
| `members` | Positive actual count of regular source files. |
| `bytes` | Positive actual sum of file sizes, without archive overhead. |
| `policyDigest` | SHA-256 of the complete canonical snapshot policy. |

The commitment has exactly these six fields. Verify both artifact files
against the expected commitment and policy. Check actual member content,
modes, names, hashes, totals, and schema. Reject malformed bytes, unsupported
fields, duplicates, extra members, and changed artifacts. Restore then checks
the newly written bytes and modes before it returns.

An external trusted caller binds `reference` to the artifact. The manifest
contains no reference field. Local verification cannot prove that external
registration or current-source equality after export. Metadata alone does not
prove source content or grant execution authority.

## Caller sequence and failures

1. Select source suitable for transfer. Set the limits and all private paths.
   Acquire the source and private output-parent leases.
2. Call `export_snapshot` with a new artifact path and the opaque reference.
   Retain its actual commitment and both artifact files under the lease.
3. Call `verify_snapshot` with that commitment and policy before submission.
   Keep writers excluded through transfer. Do not substitute an old successful
   verification for a check of the bytes being transferred.
4. At the receiver, call `restore_snapshot` with the expected commitment, the
   same policy, and a new private destination. The receiver must verify the
   bytes it receives.
5. Retain the restored source lease for the consuming operation. Obtain any
   build authorization through the separate controller contract.

`SnapshotError` reports a failed operation. Failures include policy or
admission refusal, unsupported capability, source mutation, artifact mismatch,
resource or deadline exhaustion, and output or cleanup failure. Retain useful
causes without recording private content or credentials. A failed export or
restore cannot return a successful commitment or destination.

Failed construction removes only the objects owned by that operation. It must
preserve pre-existing caller data and foreign replacements. If cleanup cannot
be confirmed, retain the lease and diagnostics for the owner. Do not declare
the path removed, reuse it, or delete an unverified replacement.

## Separate integration gates

Controller admission, authenticated grants, recipe and toolchain checks,
transport, process isolation, cancellation, output collection, and independent
receipt verification remain separate contracts. Snapshot export does not
produce a build result or set `collectionVerified`.

The [private retained-log service](fleet-build-artifacts.md) reads independently
retained terminal output. Snapshot export does not publish logs, register a
bundle, or enable controller log reads. BuildTestJob integration and a future
optional Fleet MCP submission/status/cancel adapter remain separate work. This
snapshot API has no MCP runtime dependency.

The API contract does not establish local CI, hosted CI, real-worker execution,
cluster acceptance, or deployment. Those claims require their own actual,
source-bound evidence.
