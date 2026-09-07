# ADR-0042: Fail-closed Codex implementation adapter

- Status: Accepted
- Date: 2026-09-06
- Tracks: #3019, #2887
- Extends: ADR-0005, ADR-0006, ADR-0029

## Context

A Codex implementation job must not have access to shared operator state. The
provider needs network access for its transport, but a command that the
provider starts must not have this access. A local macOS Seatbelt profile
cannot supply both controls. macOS rejects a second nested profile. A process
group is also not a lifecycle owner because a descendant can create a new
session and escape the group.

The base package needs a stable integration contract for an external operating
system adapter. The contract must not state that a production adapter exists.
It must keep a stock installation closed until an operator supplies reviewed
deployment evidence.

## Decision

Hephaestus uses only the accepted version 1 field sets for the request, the
prepared result, and the final result. These record fields are distinct from
the three adapter operations: `prepare`, `invoke`, and `destroy`. Automation
requires a version 1 deployment lock and adapter. It does not change or replace
a version 1 field.

The existing request fields bind the operation authority. The command and
policy bind the sandbox, tool grant, network rule, and file-system mounts. The
session field contains canonical JSON that binds the lifecycle and the optional
provider session ID. The host also validates these values against its frozen
execution request. A resume operation is not valid without the same provider
session ID. The private profile and durable session record keep that ID for the
next operation.

Canonical SHA-256 digests and a new host nonce bind each implementation request.
`prepare(request)` starts the pinned guest without authentication and proves the
exact Linux Codex executable. After the host validates that record,
`invoke(prepared, auth_path)` uses the same guest, token, and executable
identity. The adapter output contains the actual Codex provider session ID. The
host validates the final result and this ID before it accepts output.

The host sets a deadline outside each adapter call. If `invoke` does not stop,
the host calls `destroy(prepared)` through a separate control request. It waits
for the invoke and destroy calls to stop. It records a typed internal terminal
receipt after both calls stop. If `prepare` returns after its deadline, the host
validates and destroys the late prepared guest. An incomplete stop is a stable
failure. The isolated helper can process the terminal control request while an
invoke request is active.

Only the Codex implementation role uses this contract. Planning, review,
learning, and public direct Codex calls keep their current behavior. The base
package does not contain, enumerate, or automatically select an adapter. It
does not fall back to direct execution. An operator must give an exact entry
point name, an absolute detached deployment-lock path, and the expected
lowercase SHA-256 digest through explicit configuration.

The automation product layer admits the deployment before it imports adapter
code. It verifies the retained wheel, installed tree, signed guest image,
Codex 0.153.4 AArch64 Linux artifact, Sigstore bundle, trusted root, and Rekor
evidence against one owner-controlled detached lock. Runtime verification is
offline. It verifies the real retained Codex release bundle and Rekor proof
against the retained production trusted root. It accepts the release's legacy
bundle only after it constructs the current Sigstore bundle from the locked
certificate, signature, log entry, proof, and checkpoint. Sigstore is an
automation-only dependency and does not enter the base library import surface.
The importer keeps the verified module bytes in memory. It runs the adapter in
an isolated Python helper with a closed module finder and a standard-library
path. The helper gives adapter modules guarded `importlib`, `builtins`, and
`sys` views. It also rejects a direct source loader and a changed module path.
Adapter code cannot select an ambient package during module import, factory
execution, or an adapter operation. The worker closes the helper on every
initialization and execution result.

The required artifact lane uses a host-owned offline fixture store. A tracked
manifest fixes the official GitHub asset IDs, names, sizes, and SHA-256
digests. The lane provisions the archive, bundle, and extracted ELF before it
starts the test container. The repository retains only the small bundle,
trusted root, Rekor key, checkpoint, proof, and manifest. The test container
has no network and mounts the provisioned fixture read-only. A missing fixture
is a test failure. The test changes one byte in each retained object and
requires admission to fail before adapter import.

The worker holds no-follow descriptors for the linked-worktree `.git` pointer,
Git directories, index, repository configuration, and worktree configuration.
It rejects links, path escape, includes, hooks, and file-system monitors. The
guest gets fixed Git environment values. The operation policy selects a
read-only or read-write worktree mount. The `.git` pointer and all Git metadata
are operating-system read-only for each operation. The worker compares path and
descriptor identities before launch and after return. It also compares bound
content digests after return.

The host copies only the locked `aarch64-unknown-linux-musl` ELF bytes through
a held descriptor to an owner-only staged file. It creates a private profile
that contains only the validated Athena package and a transient authentication
bridge. It creates authentication only after all other checks and prepared
result validation. It closes and removes the authentication bridge, flushes
its parent, and proves absence for every `BaseException`.

The external production topology is a separate package. It uses an ephemeral
AArch64 Linux virtual machine through Apple Virtualization.framework. The
virtual machine has no general network interface. Split virtiofs mounts
separate the writable worktree from protected Git metadata. A provider-only
relay gives Codex its pinned transport. The Linux command sandbox denies
command network access. A delegated cgroup v2 leaf and a forced virtual-machine
stop own all descendants. The adapter must return two consecutive complete and
empty descendant inventories after bounded TERM, KILL, and pipe cleanup. The
host accepts these inventories only when their timestamps are in its observed
invoke interval and after the pipe-close timestamp. The final destroy call must
confirm that the virtual machine stopped.

The stable failure codes are `codex_adapter_not_selected`,
`codex_adapter_not_installed`, `codex_adapter_ambiguous`,
`codex_adapter_initialization_failed`, `codex_adapter_protocol_mismatch`,
`codex_adapter_request_mismatch`, `codex_adapter_launch_failed`,
`codex_adapter_timeout`, `codex_adapter_pipe_cleanup_failed`,
`codex_adapter_inventory_uncertain`, `codex_adapter_descendants_remain`, and
`codex_adapter_result_invalid`. Diagnostics do not contain adapter exception
text, authentication data, prompt text, private paths, or raw child output.

Host publication stays separate from adapter execution. A successful result
does not authorize a commit or push. The queue must first validate cleanup,
the Git receipt, frozen plan claims, the accepted head-bound remediation
scope, and all dirty and committed paths.

## Alternatives considered

- Use one local Seatbelt profile. Rejected because it cannot give network
  access to the provider and deny that access to all command descendants.
- Add a nested Seatbelt profile. Rejected because macOS rejects this profile
  for the supported Codex release.
- Use a process group as the lifecycle boundary. Rejected because a descendant
  can call `setsid`, fork again, and keep provider pipes open.
- Bundle or automatically select an adapter. Rejected because installation
  must not silently activate external security-boundary code.
- Accept adapter-reported digests as deployment evidence. Rejected because an
  adapter self-attestation is not an independent trust anchor.

## Consequences

Stock Hephaestus installations fail closed for Codex implementation jobs. A
deployment can activate an adapter only with explicit selection and complete
offline evidence. Failure, timeout, uncertain cleanup, protocol mismatch, or
changed Git identity blocks publication but never skips authentication
removal.

Issue #3019 supplies only the base contract. It does not approve a production
adapter and does not close #2887. Rollback removes the explicit adapter
selection or restores the earlier base package; either action keeps Codex
implementation closed. A future adapter release must pass this exact
non-skipping test on a real Apple Silicon macOS runner:

```text
tests/integration/test_codex_macos_vm_boundary.py::test_real_vm_uses_bound_linux_codex_identity
```

The test must fail when the framework, signed image, adapter wheel, locked
Linux artifact, offline Sigstore evidence, or required relay is absent. It must
prove the same guest and executable identity, provider transport, Athena
access, split file-system grants, command-network denial, double-fork cleanup,
pipe cleanup, two empty inventories, and authentication absence after every
result.
