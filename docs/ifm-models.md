# IFM Model Configuration

Hephaestus recognizes the current non-GGUF models in the
[IFM model catalog](https://huggingface.co/IFM/models). This registry includes
the six [K2-Horizon](https://ifm.ai/blog/k2/) checkpoints. It also includes the
0.9B checkpoint.

Model recognition does not start or manage an inference server. Configure an
OpenAI-compatible server before you use an IFM model with OpenCode or Pi.

## Model selection

Use an exact model ID or a K2-Horizon alias. These examples select the same
model and reasoning effort:

```text
--reviewer-model k2-horizon-0.9:high
--reviewer-model IFM/K2-Horizon-0.9B:high
```

The available aliases are:

| Alias | Model ID |
|---|---|
| `k2-horizon-0.9` | `IFM/K2-Horizon-0.9B` |
| `k2-horizon-3.7` | `IFM/K2-Horizon-3.7B` |
| `k2-horizon-7` | `IFM/K2-Horizon-7B` |
| `k2-horizon-32` | `IFM/K2-Horizon-32B` |
| `k2-horizon-36` | `IFM/K2-Horizon-MoVA-36B-A4B` |
| `k2-horizon-375` | `IFM/K2-Horizon-375B-A23B` |

The reasoning values are `default`, `low`, `medium`, `high`, and `xhigh`.
A role option has priority over an inline value. For example,
`--reviewer-reasoning-effort high` replaces `:low` in the reviewer model.

## OpenCode

Add an operator-local custom provider to `opencode.json`. Use the provider ID
`IFM` so its full model references match the Hephaestus registry. Replace the
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
Hephaestus passes an explicit reasoning value through `--variant`.

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
