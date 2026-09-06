# Migration Guide

> **Release status:** The latest signed `vX.Y.Z` git tag is the release-status source of truth (tag-driven via hatch-vcs). See [GitHub Releases](https://github.com/HomericIntelligence/Hephaestus/releases) for published releases. **1.0 has not been released yet** — the section below is forthcoming 1.0 migration guidance. The package remains on the 0.x release line until a signed `v1.0.0` tag is published.

## Current main (unreleased)

The operational readiness wait described below is part of the released 0.10.2
behavior.

Before a request, `merge_wait` may wait for operational GitHub readiness without
spending a merge attempt. The `--poll-max-wait` option controls this wait. Its
default is 1,200 seconds (20 minutes). Readiness is not authorization, and the
exact-head and label admission checks repeat before every request.

### Model effort selection

Put an effort in the applicable model reference as `MODEL:EFFORT`. Remove these
separate options from automation commands:

| Removed option | Replacement |
|---|---|
| `--planner-reasoning-effort EFFORT` | `--planner-model MODEL:EFFORT` |
| `--reviewer-reasoning-effort EFFORT` | `--reviewer-model MODEL:EFFORT` |
| `--implementer-reasoning-effort EFFORT` | `--implementer-model MODEL:EFFORT` |

Programmatic `LoopConfig` and `PipelineConfig` callers must make the same
change. Move each removed `*_reasoning_effort` field into the matching
`*_model` value.

The public `MODEL_REASONING_EFFORTS` export is removed. Use
`parse_model_selection()` to separate the model and effort. The final nonempty
colon segment is the effort. For a model ID that contains a colon, add
`:default` to use the provider default.

`PI_THINKING_LEVELS` remains available. Pi uses it to validate
`defaultThinkingLevel` in its trusted settings file. Claude uses only the base
model. Codex can retry one time without an unsupported effort before it starts
work. OpenCode and Pi use their native error or fallback behavior.

## 0.x → 1.0 (forthcoming — not yet released)

### Summary

Version 1.0 intentionally removes ambient Hephaestus environment configuration.
Every `HEPH_*` and `HEPHAESTUS_*` runtime setting is replaced by an explicit
command-line option, typed configuration field, or internal job value. Old
variables are not read for compatibility or warnings; setting one has no effect.

The stable `merge_with_env()` and `get_proj_root()` APIs are also removed. This
is a major-version break: configuration is now resolved once at a CLI boundary
and passed explicitly through library and automation calls.

### Upgrade checklist

1. **Widen your version pin.** If you pinned `homericintelligence-hephaestus` with an
   upper bound of `<1`, change it to `<2` so you receive 1.x releases:

   ```toml
   # pyproject.toml or pyproject.toml
   "homericintelligence-hephaestus>=1.0,<2"
   ```

2. Replace `merge_with_env()` with `load_config()` plus `merge_configs()` using
   explicitly parsed command-line overrides.

3. Replace `get_proj_root(name)` with `get_repo_root()` for the current checkout,
   an owning command's `--repo-root` option, or an explicit `Path` argument.

4. Replace every `HEPH_*` or `HEPHAESTUS_*` setting with the corresponding CLI
   option where the operation still has an owning CLI boundary. There is no
   compatibility precedence: every old variable is ignored. The former advise,
   learn, follow-up, and outer plan-stage model/timeout variables have no modern
   queue replacement: advise/learn are host-owned Mnemosyne operations, while
   the follow-up and outer-plan subprocess paths are disconnected legacy code.

5. If you invoke live contract tests, pass `--run-contract-tests`,
   `--run-contract-agent`, `--contract-repo`, and `--contract-model` to pytest.
   CI enforcement uses `--require-cli` and `--require-pi-package-smoke`.

6. Re-run your test suite and the environment-policy validator.

### Behavioral changes to be aware of

These are bug fixes, not API changes — signatures are unchanged — but the runtime
behavior is now correct where it previously was not. If you depended on the buggy
behavior (you should not have), review these:

| Area | 0.x behavior | 1.0 behavior |
|------|--------------|--------------|
| `hephaestus.__version__` | Resolved to `"unknown"` for installed users (wrong distribution-name lookup) | Resolves to the real installed version |
| `hephaestus.io.safe_write` | Not atomic — an interrupted write could leave a partial file | Atomic: writes via a temp file + `os.replace` |
| `hephaestus.io.write_secure` | Restrictive permissions but non-atomic write | Atomic **and** `0o600`-permissioned |
| `hephaestus.github.wait_until` | Raised `ValueError` when called from a worker thread | Safe to call from any thread |

### Removed deprecated symbols

The following previously-deprecated symbols have been **removed**. Imports such as
`from hephaestus.config import get_config_value`, `from hephaestus.utils import
retry_with_jitter`, `hephaestus.get_config_value`, and `hephaestus.retry_with_jitter`
will now raise `ImportError`/`AttributeError`. Migrate to the canonical replacements:

| Removed symbol | Replacement |
|----------------|-------------|
| `get_config_value` | `load_config()`, `merge_configs()`, then `get_setting()` |
| `merge_with_env` | `load_config()` plus `merge_configs()` with explicit CLI values |
| `get_proj_root` | `get_repo_root()`, `--repo-root`, or an explicit `Path` |
| `retry_with_jitter` | `retry_with_backoff(jitter=True, max_delay=...)` |

### Questions

If an upgrade surfaces an unexpected change in documented public-API behavior, please
[open an issue](https://github.com/HomericIntelligence/Hephaestus/issues).
