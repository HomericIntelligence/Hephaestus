# ADR-0035: Provider-aware IFM model selection

- Status: Accepted
- Date: 2026-09-03
- Tracks: IFM model support
- Supersedes: ADR-0020 model-resolution rows only

## Context

Hephaestus used Claude role defaults for provider-neutral jobs. This behavior
also supplied a Claude model ID to OpenCode and Pi when an operator did not
select a model. OpenCode has a native configured default. Pi automation must
continue to use an explicit private model and a session-bound fingerprint.

IFM publishes K2-Horizon and earlier model families through OpenAI-compatible
servers. K2-Horizon supports explicit reasoning and tool-call parser controls.

## Decision

Hephaestus keeps a static registry of the current non-GGUF IFM model
repositories. It provides aliases for the six primary K2-Horizon checkpoints.
An exact IFM model or alias can include one supported reasoning suffix.

OpenCode uses its configured default when the operator does not set a model.
For an explicit IFM reasoning value, the runtime uses OpenCode's `--variant`
option.

For Pi, the runtime reads only the operator-global `settings.json` below the
selected Pi directory. If a model is omitted, `defaultProvider` and
`defaultModel` are mandatory. The optional `defaultThinkingLevel` selects the
thinking level. The runtime then passes an explicit model and optional
`--thinking` value. The private session fingerprint covers both effective
values. A changed value prevents session resume.

The runtime does not read project `.pi/settings.json`. Pi package preflight,
private-value redaction, execution policy, and external isolation admission
remain mandatory.

Each provider-neutral agent-resolution boundary supplies its pending model
references. These references include the role reasoning option after precedence
resolution. For Pi, the runtime validates these references before admission or
authentication can start a child process. The durable plan-review record binds
the provider, model, and model-selection format as one identity. Recovery rejects
a mismatched identity.

## Alternatives considered

- Continue to pass Claude role defaults to all providers. Rejected because the
  model ID does not identify the configured OpenCode or Pi model.
- Let Pi select an ambient default. Rejected because the session binding could
  not prove the effective model and thinking level.
- Add an IFM server launcher. Rejected because server lifecycle and hardware
  policy belong to the operator.

## Consequences

- OpenCode and Pi keep their operator-selected defaults when model options are
  absent.
- Pi default configuration errors stop execution before the provider starts.
- The IFM registry requires a reviewed update when IFM publishes a new model
  repository.
- GGUF format repositories are not separate model selections in Hephaestus.
