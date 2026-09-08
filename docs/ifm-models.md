# Model Configuration

Hephaestus accepts model names as strings. It does not maintain a model catalog
or translate model aliases. Configure an external inference service before you
use a model that requires one.

## Tool and model selection

Use `--agent` to select `claude`, `codex`, `pi`, or `opencode`. Use `--model`
to supply the global model. The role options `--planner-agent`,
`--implementer-agent`, and `--reviewer-agent` override the global tool.
The corresponding role model option overrides the global model independently.
If you change a role's tool, the role still inherits the global model unless
you also supply its model option.

```text
--agent codex --model gpt-6-astra:max
--agent codex --model gpt-6-astra:max --reviewer-agent claude --reviewer-model My-Review-Model
--agent opencode --model IFM/K2-Horizon-0.9B:high
```

If you omit the tool, Hephaestus uses its existing tool detection. Claude has
preference when available. Pi requires explicit selection and admission.
If you omit both global and role model options, the selected tool uses its
configured model default. Supply `--fallback-model` to enable an explicit
fallback model. The global model does not supply the fallback. Calls with a session lifecycle
retain their recorded model and do not use quota fallback.

Model spelling and case are preserved after whitespace handling. The final
nonempty colon segment in `MODEL[:EFFORT]` is the effort. Thus, a colon in a
model ID is ambiguous under this format. Use the `:default` suffix to select the applicable
tool default. The provider owns effort validation. Claude uses only the base
model; Codex receives reasoning effort; OpenCode receives a variant; Pi receives
thinking effort. The bounded Codex unsupported-effort retry remains available.

## Codex implementation

When an isolation adapter is selected, Codex implementation uses private tool
configuration. Omitted model and effort settings use its defaults, without
reading ambient Codex configuration. Without an adapter, the native runner
uses its configured defaults. Set model and effort explicitly when both paths
must use the same selection. See
[ADR-0042](adr/0042-codex-implementation-process-boundary.md) for isolation,
[ADR-0043](adr/0043-optional-codex-adapter-until-production-ready.md) for optional
adapter selection, and
[ADR-0044](adr/0044-independent-tool-model-selection.md) for model selection.

## Migration from aliases

Former Hephaestus aliases are now literal model names. Replace `astra` with
`gpt-6-astra` when that is the required model. Replace other former aliases,
including `terra` and `k2-horizon-0.9`, with the exact IDs that your provider
accepts. Hephaestus does not translate names across providers or supply a model
from a difficulty label. Model names in the examples below are operator choices.

## OpenCode

Add an operator-local custom provider to `opencode.json`. Use the provider ID
`IFM` for the full model references in this example. Replace the
placeholders with private operator values.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "IFM/K2-Horizon-0.9B",
  "provider": {
    "IFM": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "IFM",
      "options": {
        "baseURL": "<private-provider-url>"
      },
      "models": {
        "K2-Horizon-0.9B": {
          "name": "K2-Horizon 0.9B",
          "variants": {
            "low": {"reasoningEffort": "low"},
            "medium": {"reasoningEffort": "medium"},
            "high": {"reasoningEffort": "high"},
            "xhigh": {"reasoningEffort": "xhigh"}
          }
        }
      }
    }
  }
}
```

If you omit all Hephaestus model options, OpenCode uses its configured model.
Hephaestus passes an explicit effort through `--variant`. OpenCode v1 applies
the variant only when its resolved model configuration defines that name. For
another value, OpenCode uses the base model options. Add a model variant to
`opencode.json` before you use a new provider effort through OpenCode.

## Pi

Add the provider and model to the operator-global
`~/.pi/agent/models.json` file. Do not put private provider configuration in
the repository.

```json
{
  "providers": {
    "IFM": {
      "baseUrl": "<private-provider-url>",
      "api": "openai-completions",
      "apiKey": "$IFM_API_KEY",
      "models": [
        {
          "id": "K2-Horizon-0.9B",
          "name": "K2-Horizon 0.9B",
          "reasoning": true,
          "contextWindow": 131072,
          "maxTokens": 32768,
          "thinkingLevelMap": {
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh"
          }
        }
      ]
    }
  }
}
```

Set the operator-global default in `~/.pi/agent/settings.json`:

```json
{
  "defaultProvider": "IFM",
  "defaultModel": "K2-Horizon-0.9B",
  "defaultThinkingLevel": "high"
}
```

If you omit all Hephaestus model options, Hephaestus reads these three fields.
It passes an explicit `IFM/K2-Horizon-0.9B` model and `high` thinking level to
Pi. It also binds both values to the private session fingerprint. Hephaestus
does not read project `.pi/settings.json` files.

Hephaestus passes an inline effort through `--thinking`. Pi owns validation,
fallback, and clamping for this value.

Pi automation still requires its package preflight and an external isolation
adapter. See [Private Pi Provider Setup](pi-private-provider.md).

## Server parser settings

K2-Horizon agent use requires the `k2_horizon` reasoning parser and tool-call
parser. For vLLM, also enable automatic tool choice. A typical external vLLM
command for the 0.9B model is:

```bash
vllm serve IFM/K2-Horizon-0.9B \
  --revision 9b9ec1f7e17f62ed218df542687a144116219d84 \
  --code-revision 9b9ec1f7e17f62ed218df542687a144116219d84 \
  --trust-remote-code \
  --dtype bfloat16 \
  --max-model-len 131072 \
  --reasoning-parser k2_horizon \
  --enable-auto-tool-choice \
  --tool-call-parser k2_horizon
```

The revision options pin the model files and remote code to one reviewed
commit. Review a new commit before you change both pins. Do not use a branch
name for these options.

The 0.9B checkpoint has a 128K context limit. Configure other limits from the
applicable model card and the available server resources.
