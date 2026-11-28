# Codex interactive attention observations

The Codex attention adapter reports unmanaged interactive sessions to the
Odysseus Fleet Attention view. It does not admit work, create a task, attach to
a conversation, approve a tool call, or send input to Codex. Agamemnon and the
contained Fleet worker retain their existing authority.

## Boundary

Two local processes form the adapter:

1. A Codex command hook reads one hook payload from standard input. It keeps
   only approved metadata and sends that metadata to a private Unix socket.
   The hook writes no standard output and returns no Codex control decision.
2. One foreground observer owns session state. It writes sanitized full-state
   NDJSON observations to standard output immediately after each event and at
   the configured heartbeat interval.

Run both processes in the same allocation and security context as Codex. Do not
put the socket or observer on a login node as a permanent service. The observer
has no durable store, replay cursor, NATS credential, scheduler, or control
channel. A trusted transport can attach its standard output to the Odysseus
observation input.

The state directory must be absolute, owned by the current user, and mode
`0700`. The observer owns a mode `0600` socket named `observation.sock` in that
directory. Socket input and NDJSON output have fixed size limits. A failed hook
delivery produces a generic warning and does not block Codex. The missed event
can leave the displayed state incomplete until another event arrives.

## Supported Codex contract

The adapter targets the stable hook interface in Codex 0.172.0. Confirm the
installed version and enabled hook feature before deployment. The supported
hooks are:

- `SessionStart` and `SessionEnd`;
- `UserPromptSubmit`, `Stop`, and `Interrupt`;
- `PreModelRequest` and `PostModelRequest`;
- `PreToolUse` and `PostToolUse`;
- `PermissionRequest`;
- `PreCompact` and `PostCompact`; and
- `SubagentStart` and `SubagentStop`.

A normal `Stop` event means that the turn is idle, not that the session ended.
`SessionEnd` reports `disconnected` with `session_end` evidence. Codex can delay
that event until session cleanup or idle timeout. The adapter does not infer
Slurm allocation termination. A separate trusted source must corroborate the
exact allocation before an observation uses `slurm_job_end`.

## Configure an isolated observer

Use the Python environment that contains this Hephaestus version. The generated
hook configuration embeds its absolute Python path and the absolute state
path.

```bash
python -m hephaestus.fleet.codex_attention_cli print-hooks \
  --state-dir /private/allocation/attention \
  > "$CODEX_HOME/fleet-attention-hooks.json"
```

Merge the reviewed definitions into `$CODEX_HOME/hooks.json`, or use a dedicated
`CODEX_HOME` that contains only the reviewed configuration. Do not redirect
this output over an existing `hooks.json`: it can erase unrelated hooks.
Review the exact hook definitions through the Codex `/hooks` interface before
you trust them. Review them again after an upgrade changes the definitions.
Do not use `--dangerously-bypass-hook-trust` for normal operation. That option
is limited to a separate, controlled probe of hooks that the operator already
reviewed.

Start the observer before Codex:

```bash
python -m hephaestus.fleet.codex_attention_cli serve \
  --state-dir /private/allocation/attention \
  --cluster cluster-a \
  --account-tier admin \
  --unix-user service-admin \
  --node cn042 \
  --slurm-job-id 88421 \
  --harness-version 0.172.0
```

Use the real identity values, not these examples. `--slurm-job-id` is optional
identity metadata; it does not prove allocation state. The default heartbeat
is 30 seconds. `--heartbeat-seconds` and `--max-sessions` bound observer
behavior. Stop the observer with `SIGINT` or `SIGTERM` so that it removes its
socket.

Each NDJSON line has this envelope:

```json
{
  "type": "observation",
  "subject": "hi.fleet.observations.interactive.v1.cluster-a.admin.codex.session-token",
  "observation": {
    "source": "hephaestus",
    "target": "odysseus",
    "operation": "snapshot"
  }
}
```

The complete observation uses `hi/fleet-interactive/v1`. It includes the
session, agent, adapter, epoch, sequence, activity, source health, and redacted
summary required by the Odysseus consumer. Session tokens are hashes, not raw
Codex session identifiers in the NATS subject.

## Privacy and state limits

The hook drops prompts, transcripts, working directories, model names, tool
arguments, tool output, assistant messages, error bodies, and token usage. It
can retain only a bounded tool name for a fixed summary such as `Codex tool
started: Bash`. The observer never reads the Codex transcript.

Every observation is a full state snapshot. Sequence numbers increase within a
session epoch. A heartbeat advances the sequence and observation time but does
not invent work progress. A new session start creates a new epoch. The observer
keeps a bounded number of active sessions and ended-session tombstones in
memory. Ended identities reject late events only inside that bounded window.
Restarting the observer discards that state; the next event starts a new epoch.
Consumers must expose staleness and sequence gaps instead of treating the
observer as durable history.

## Verification

Run the focused checks with locked dependencies:

```bash
uv run --locked pytest tests/unit/fleet/test_codex_attention.py \
  --override-ini="addopts=" -v --strict-markers
uv run --locked ruff check hephaestus/fleet/codex_attention.py \
  hephaestus/fleet/codex_attention_cli.py \
  tests/unit/fleet/test_codex_attention.py
uv run --locked mypy hephaestus/fleet/codex_attention.py \
  hephaestus/fleet/codex_attention_cli.py \
  tests/unit/fleet/test_codex_attention.py
```

An isolated probe can use a temporary `CODEX_HOME`, generated hooks, and a local
mock Responses provider. That probe does not need provider credentials or a
real model. It verifies only the installed CLI and emitted hook lifecycle. It
does not verify an interactive terminal, tool approval, subagents, a Slurm
allocation, authenticated NATS transport, or an Odysseus deployment. Record
those boundaries with any deployment evidence.
