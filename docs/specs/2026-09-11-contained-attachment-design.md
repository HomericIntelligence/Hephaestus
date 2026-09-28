# Contained Codex attachment

This follow-up starts from Hephaestus `19ba7b8`. PR 3190 remains frozen for its
independent review. The work preserves five Codex provider runtimes, each with
independent logical conversations. It does not enable Fleet session admission.

## Authority and transport

Reuse `ContainedExecSupervisor` as the container owner. Direct provider launches
through Podman omit that owner's checks. A separate per-session supervisor would
duplicate journal ownership and recovery. Instead, one supervisor owns the pool
and provides one private Unix attachment socket for each lease.

The provider's program transport runs the installed Python attachment module.
Its handshake identifies a lease and a digest of the immutable assignment,
workspace, image, container, resource budget, and engine context. The attachment
client accepts no engine command. The server checks the current retained binding
and calls `ContainedExecSupervisor.start`. It makes the tool stream available
only after the actual engine and kernel checks return.

Stream buffers have a fixed limit. Stream content is not written to a journal,
event, or diagnostic. EOF closes the attachment input. EOF does not confirm that
detached children stopped. The lease remains reserved until the supervisor has
causal disposal evidence. A second attachment or uncertain restart requires
reconciliation; it cannot restart a retained endpoint implicitly.

One operation lock serializes supervisor reads and mutations from its attachment
threads. The journal still has one process owner. Private runtime, authentication,
and spool roots must be supplied as protected roots before admission. Every new
workspace and every unresolved workspace loaded at restart must be disjoint from
those roots. Engine authority remains separately protected by the engine adapter.

## Paths and provider configuration

Each lease binds one canonical host workspace to `/workspace` in its contained
endpoint. Codex thread cwd, workspace roots, filesystem grants, and tool HOME/XDG
paths must use the contained path. Host authority paths must not be projected as
tool paths. Filesystem preparation remains on the host workspace through the
supervisor's explicit mount contract.

`environments.toml` must contain `include_local=false` and `default="none"`.
Every thread and turn start selects exactly its assigned environment. The
registry must refuse a missing supervisor attachment. It must never fall back to
a direct engine command or a local tool environment. The existing non-Fleet
provider integration is outside this change.

## Remaining execution gates

The published standalone supervisor probe established direct contained
exec-server transport and causal disposal. It did not establish normal Codex
thread startup or model-to-tool routing.

The pinned 0.153.4 schema accepts a named permission profile at `thread/start`.
The current restricted profile can require nested filesystem sandboxing during
remote AGENTS instruction reads. The public `externalSandbox` setting exists at
`turn/start`, after that startup operation. It cannot establish that earlier
startup works. The native and shared runtime permission profiles remain unchanged.

First prove the new attachment with fixed pipe processes and no account access.
Then use the engine owner's bounded Linux slot to check actual remote-only
startup, explicit path interpretation, and disabled local selection. Keep any
startup failure as a failed gate. An actual model-to-tool turn is a separate
operator-authorized gate after containment and startup checks pass. No metadata
file, test report, or configuration flag can grant admission by itself.

## Validation

Tests use the real journal and supervisor, actual Unix sockets, and a fixed echo
process. Only the external engine and kernel observation boundaries are test
substitutes. Cases cover wrong bindings, changed generation, failed kernel
observation, private socket ownership, full-duplex EOF, and retained reservations.
Additional cases cover future private-root overlap, restart overlap, explicit
contained paths, and refusal of raw engine configuration. These tests do not
claim container enforcement, authentication, or provider model execution.

The engine operator can run `just fleet-contained-startup-probe NEW_ROOT
ENGINE_PROGRAM PRIVATE_ENGINE_SOCKET CODEX_PROGRAM IMMUTABLE_IMAGE` in the same
Linux host as the engine. All authority directories are new and empty. The
provider wrapper accepts only initialization, environment status, and restricted
thread startup. It refuses model turns, account operations, and direct tool
calls. The operator must apply a whole-run deadline and preserve the journal if
disposal remains uncertain. This harness has not been executed in this follow-up.
