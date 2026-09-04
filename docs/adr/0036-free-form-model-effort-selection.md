# ADR-0036: Free-form model effort selection

- Status: Accepted
- Date: 2026-09-04
- Tracks: GPT-6 Astra and provider-defined reasoning effort
- Supersedes: ADR-0035 effort selection and ADR-0020 reasoning-selector rows

## Context

ADR-0035 permits only a fixed list of effort suffixes. The automation loop also
has separate planner, reviewer, and implementer effort options. Each new
provider value needs a Hephaestus code change even when the provider accepts
the value. The two inputs also need a precedence rule.

GPT-6 Astra adds another Codex model. Future models can add effort values that
the current Hephaestus list does not contain.

## Decision

All model options use `MODEL[:EFFORT]`. The final nonempty colon segment is the
effort. Hephaestus accepts any effort string and does not keep an allowlist.
The model reference is the only CLI input for effort. Remove the separate
planner, reviewer, and implementer effort options and their configuration
fields.

Codex receives the effort as `model_reasoning_effort`. OpenCode receives it as
`--variant`. Pi receives it as `--thinking`. The value `default` selects the
applicable provider default. Claude receives only the base model because it has
no effort transport.

When Codex rejects an effort before it emits model output or a work item,
Hephaestus retries one time without an explicit effort. A lifecycle-only
`turn.started` event does not prevent this retry. The retry requires an exact
structured `invalid_request_error` that identifies the reasoning-effort field
and an unsupported value or parameter. A wrapper can omit the HTTP status when
it supplies these exact fields. A tagged compatibility shape must include HTTP
status 400. Other errors do not cause this retry. OpenCode and Pi own fallback
for unknown effort values, so Hephaestus does not repeat their turns.

Add `astra` as an alias for the official `gpt-6-astra` Codex model. The alias
and the exact model ID use `xhigh` when the reference has no effort. This
addition does not change the global default model.

Durable provider model metadata stores the free-form effort string. It checks
the data type, not a Hephaestus value list.

## Alternatives considered

- **Keep a fixed list.** Rejected because each provider addition needs a
  Hephaestus release.
- **Keep the separate role options.** Rejected because two effort inputs need a
  precedence rule and duplicate state across the pipeline.
- **Retry all provider errors without effort.** Rejected because this can hide
  authentication, model, quota, transport, and tool failures. It can also
  repeat a turn that started work.
- **Retry OpenCode and Pi in Hephaestus.** Rejected because these providers
  already apply native fallback. A second retry can repeat external work.

## Consequences

- New provider effort values do not need a Hephaestus code change.
- A model ID that contains a colon is ambiguous. The final nonempty segment is
  always the effort.
- Codex can make two provider requests only after the exact pre-work effort
  rejection. All other Codex failures keep their current behavior.
- OpenCode and Pi fallback behavior follows their installed provider versions.
- Existing commands that use separate role-effort options must move the value
  into the applicable model reference.
- `LoopConfig` and `PipelineConfig` callers must move each removed
  `*_reasoning_effort` value into the matching `*_model` reference.
- The public fixed-list exports `MODEL_REASONING_EFFORTS` and
  `PI_THINKING_LEVELS` are removed. Callers must parse the model reference and
  let the selected provider validate the effort.
