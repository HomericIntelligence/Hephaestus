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

Hephaestus supplies version 1 frozen request, policy, Git receipt, prepared
result, descendant inventory, and final result records. Canonical SHA-256
digests and a new host nonce bind each implementation request. The adapter has
two phases. `prepare(request)` starts the pinned guest without authentication
and proves the exact Linux Codex executable. After the host validates that
record, `invoke(prepared, auth_path)` uses the same guest, token, and executable
identity. The host validates the final result before it accepts output.

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
offline. Sigstore is an automation-only dependency and does not enter the base
library import surface.

The worker holds no-follow descriptors for the linked-worktree `.git` pointer,
Git directories, index, repository configuration, and worktree configuration.
It rejects links, path escape, includes, hooks, and file-system monitors. The
guest gets fixed Git environment values. The worktree is read-write, but the
`.git` pointer and all Git metadata are operating-system read-only. The worker
compares path and descriptor identities before launch and after return. It
also compares bound content digests after return.

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
empty descendant inventories after bounded TERM, KILL, and pipe cleanup.

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
