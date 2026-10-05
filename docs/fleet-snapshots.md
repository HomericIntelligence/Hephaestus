# Fleet source snapshots

## Scope

This module supplies local source export, verification and checked restore for
subordinate Fleet builds. Agamemnon owns build admission. A separate supervisor
must own transport, run grants, the fixed recipe, resource limits, output and cleanup.
The snapshot code does not start a build command or fetch an object reference.

The caller must hold an exclusive lease on a supervisor-owned private output
parent before export or restore. The parent must belong to the current user and
have mode `0700`. Public writable or readable parents do not supply this
boundary. The supervisor must exclude concurrent writers until verification
finishes, then hand the verified workspace to the child under its retained
lease. Inode checks do not exclude an arbitrary process with the same user ID
that can replace this trusted parent. This API does not claim that capability
or create a new cross-process lease owner.

## First profile

Export the current contents of tracked files and untracked files that Git does
not ignore. A deleted tracked file is absent from the complete snapshot. Staged
and unstaged changes both use the current working file. Git administration is
never part of the export. Before and after capture, compare the base commit,
index and selected path list. Walk actual filesystem entries so unsupported
special files cannot disappear from Git's untracked listing. Read each file
through descriptor-relative paths
without following links. Reject source changes during capture.

The first profile rejects selected symbolic links, submodules, hard links,
special files, unsafe paths and special permission bits. Preserve regular-file
permission bits. Directories are implicit and private, with mode `0700`; empty
directories, ownership, timestamps and extended attributes are not source
members. Names must be canonical relative POSIX paths with portable ASCII
components. Reject case-fold collisions.

The operator sets positive member and payload-byte limits. The fixed profile
allows at most 10,000 files, 64 MiB of payload, 240 characters per path and
40,000 scanned entries. Manifest and each Git output are limited to 4 MiB.
These limits are part of the policy digest. They are implementation limits,
not permission to run a build or a substitute for registered recipe limits.

The versioned policy excludes Git administration, `.fleet-runtime`, provider
runtime roots and known credential names. The operator can declare additional
private relative paths. Ignore an excluded untracked path without reading it.
Reject an excluded tracked path instead of claiming a complete source export.
This path policy cannot identify a secret placed in an ordinary source file.
The caller must select a source tree suitable for transfer and declare all
private roots. Existing credential scanning remains a separate gate.

## Commitment and bytes

The admission snapshot object has exactly these fields:

| Field | Meaning |
| --- | --- |
| `reference` | Opaque private object ID; not a path or URL |
| `manifestDigest` | SHA256 of the complete canonical manifest bytes |
| `baseCommit` | Lowercase 40-character Git commit ID |
| `members` | Positive number of regular source files |
| `bytes` | Positive sum of source file sizes; excludes archive overhead |
| `policyDigest` | SHA256 of the complete versioned snapshot policy |

The manifest contains exactly `schema`, `baseCommit`, `policyDigest` and
`files`. Its schema is `hi/hephaestus/source-snapshot/v1`. Each file has exactly
`path`, `mode`, `size` and `sha256`. Sort files by encoded path. Encode JSON with
sorted keys, compact separators, UTF-8 and one final line feed. Counts, sizes
and modes are integers; booleans and floating-point values are invalid.

The local artifact contains `manifest.json` and `source.tar`. The archive uses
uncompressed USTAR regular members in manifest order, zero timestamps and
numeric owner IDs, and empty owner names. It has no links or extension records.
The private object store must preserve both files. A reference and digest alone
are untrusted metadata, not proof of content.

Restore accepts an expected commitment and the same policy. It verifies the
manifest, exact member set, paths, modes, sizes and actual content hashes. It
rejects extra members, links, malformed archive records and changed source
artifacts. It writes only a new private destination and does not overwrite a
caller directory. It retains directory descriptors through publication and
removes only its own recorded objects on failure. It checks both artifact files
as one stable input and verifies actual output bytes before it returns.
The caller must retain the destination under its own source
lease after verification; a return value is not a permanent filesystem lock.

## Verification before submission

Call `verify_snapshot(artifact, commitment=commitment, policy=policy, timeout=30)`
to check an existing local artifact without creating a destination. The
function returns `None` when the actual manifest and archive bytes match the
retained commitment. It uses the same bounded reads, stable artifact checks
and archive validation as `restore_snapshot`. It does not create temporary
files, publish a workspace, read the current worktree, or submit a build.

The function raises `SnapshotError` for invalid or changed artifacts, malformed
manifest or archive bytes, unsupported filesystem operations, and an expired
time budget. The timeout must be a finite number greater than zero and at most
300 seconds. Boolean values are invalid. The default is 30 seconds. One
monotonic deadline covers all reads and checks. A caller with an operation
deadline must pass only its remaining budget and check that deadline again
before submission. Verification does not renew the operation budget.

The caller owns the artifact lease and must exclude writers through
verification and transfer. A successful return describes the checked bytes;
it does not create a lock, prove current working-source identity, or grant
permission to run a build. The remote receiver must still verify the bytes it
receives. Registration binds the opaque `reference` to its artifact. The
manifest contains no reference field, so this function cannot check that
association.

## Remaining offload gates

Focused local fixtures must prove dirty content, deletion, untracked content,
permissions, deterministic artifacts, exclusions, link and path rejection,
resource bounds, source races and corrupt artifact rejection. The fixtures use
real temporary Git repositories and real archive bytes. They do not prove a
remote build.

Transport, authenticated run grants, fixed-recipe verification, process and
allocation isolation, cancellation, complete output collection, independent
receipt verification, stale local-source detection and MCP/BuildTestJob routing
remain separate consuming work. A snapshot alone does not enable a build policy
or satisfy full local or hosted CI.
