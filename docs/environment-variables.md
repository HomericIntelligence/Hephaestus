# Environment-variable policy

Hephaestus runtime configuration is explicit. Production Python code does not
read `HEPH_*` or `HEPHAESTUS_*` variables, and old names are not retained as
warning or compatibility shims. Setting one has no effect.

[`hephaestus/config/environment_registry.py`](../hephaestus/config/environment_registry.py)
is the canonical typed registry for every admitted parent read and generated
child value. [`environment-variables.toml`](environment-variables.toml) is its
exact ambient-reader projection for the governed runtime package. The
`hephaestus-check-environment-variables` command scans
`hephaestus/**/*.py` (excluding generated `_version.py`) and requires exact
agreement between source readers, the TOML registry, and the generated table
below.

## Enforcement boundary

The validator fails closed on:

- an unregistered variable or stale registered reader;
- a dynamic variable name, prefix scan, environment iteration, or full copy;
- aliases of `os`, `os.environ`, or `os.getenv`, including reads, membership,
  mutation, deletion, and passing the ambient mapping elsewhere;
- malformed Python, malformed registry data, duplicate names/readers,
  wildcards, path escapes, or missing rationale; and
- drift between the ambient-reader manifest and this document.

Its intentionally narrow executable scope is the runtime package. Scripts,
tests, Slurm templates, and workflows were migrated in the same breaking
change, but shell/YAML semantics are not claimed by the AST gate. A local shell
variable, a GitHub file channel, and an ambient product setting are not
equivalent just because they share `$NAME` syntax.

A deliberately constructed child `env=` mapping is allowed. Reading a parent
value into it is ambient input and therefore requires an exact registry entry.
Child-only constants such as `GIT_TERMINAL_PROMPT=0` do not require an input
entry.

## Approved runtime inventory

This table is generated from the ambient-reader projection. “Reader” is a stable
`path:qualified-name:access` identity; line numbers are deliberately excluded.

<!-- BEGIN GENERATED ENVIRONMENT VARIABLE INVENTORY -->
| Variable | Category | Owner | Purpose | Sensitivity | Validation | Direction | Readers |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `APPDATA` | platform | config.child_environments | Windows application configuration | sensitive | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `COMSPEC` | platform | config.child_environments | Windows command processor alias | public | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `ComSpec` | platform | config.child_environments | Windows command processor | public | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `GH_TOKEN` | child-process | config.child_environments | GitHub CLI authentication bridge | secret | non-empty | input | `hephaestus/config/child_environments.py:build_gh_child_env:read` |
| `GITHUB_STEP_SUMMARY` | workflow-input | ci/workflow helpers | GitHub step summary file | sensitive | string | input | `hephaestus/ci/precommit.py:write_step_summary:read` |
| `GITHUB_TOKEN` | child-process | config.child_environments | GitHub authentication bridge | secret | non-empty | input | `hephaestus/config/child_environments.py:build_gh_child_env:read` |
| `GPG_TTY` | child-process | config.child_environments | GPG signing terminal bridge | sensitive | path | input | `hephaestus/config/child_environments.py:build_git_signing_env:read` |
| `HOME` | platform | config.child_environments | CLI home and configuration lookup | sensitive | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `LANG` | platform | config.child_environments | Locale stability | public | string | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `LC_ALL` | platform | config.child_environments | Locale override | public | string | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `LC_CTYPE` | platform | config.child_environments | Character encoding | public | string | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `LOCALAPPDATA` | platform | config.child_environments | Windows application cache | sensitive | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `LOGNAME` | platform | config.child_environments | Host identity hint | public | string | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `PATH` | platform | config.child_environments | Command discovery | public | non-empty | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `PATHEXT` | platform | config.child_environments | Windows executable suffixes | public | string | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `SHELL` | platform | config.child_environments | Interactive shell hint | public | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `SSH_AUTH_SOCK` | child-process | config.child_environments | Git SSH authentication and signing bridge | secret | path | input | `hephaestus/config/child_environments.py:build_git_signing_env:read` |
| `SYSTEMROOT` | platform | config.child_environments | Windows system root | public | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `SystemRoot` | platform | config.child_environments | Windows system root alias | public | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `TEMP` | platform | config.child_environments | Windows temporary directory | sensitive | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `TMP` | platform | config.child_environments | Windows temporary directory | sensitive | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `TMPDIR` | platform | config.child_environments | Temporary and runtime directory | sensitive | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read`<br>`hephaestus/github/rate_limit.py:_runtime_base_dir:read` |
| `TZ` | platform | config.child_environments | Timezone stability | public | string | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `USER` | platform | config.child_environments | Host identity hint | public | string | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `USERPROFILE` | platform | config.child_environments | Windows home directory | sensitive | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `WINDIR` | platform | config.child_environments | Windows system directory | public | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `XDG_CACHE_HOME` | platform | config.child_environments | Unix cache directory | sensitive | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `XDG_CONFIG_HOME` | platform | config.child_environments | Unix configuration directory | sensitive | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
| `XDG_DATA_HOME` | platform | config.child_environments | Unix data directory | sensitive | path | input | `hephaestus/config/child_environments.py:read_approved_parent_env:read` |
<!-- END GENERATED ENVIRONMENT VARIABLE INVENTORY -->

Categories distinguish operator configuration from platform state,
workflow-provided channels, child-process inheritance, and internal protocols.
Secret or sensitive entries approve only the named transport boundary; they do
not authorize logging, persistence, wildcard token names, or propagation to a
different child.

## Removed configuration and replacements

| Removed surface | Explicit replacement |
| --- | --- |
| Planner, implementer, reviewer, and fallback model variables | `--planner-model`, `--implementer-model`, `--reviewer-model`, and `--fallback-model`; role option → `--model` → fixed role default |
| Advise and learn model variables | Retired without a CLI replacement: modern advise/learn work is deterministic, host-owned Mnemosyne execution and does not select an agent provider model |
| Pi provider/model variables | `--pi-alias-config PATH` pointing to an owner-only, non-symlink, mode-`0600` TOML file with exactly `provider` and `model` |
| Pi automation kill switch | `--disable-pi-automation` in the typed runtime policy |
| Active agent and operation timeout variables | Owning command flags such as `--planner-timeout`, `--agent-timeout`, `--gh-timeout`, `--metadata-timeout`, `--network-timeout`, `--rebase-timeout`, `--diff-collect-timeout`, and `--pre-pr-test-timeout` |
| Advise, learn, follow-up, and outer plan-stage timeout variables | Retired from the modern queue CLI because host-owned advise/learn and disconnected legacy follow-up/outer-plan paths have no corresponding queue subprocess; legacy free functions use fixed defaults with injectable typed parameters |
| Rate-guard variables | `--rate-guard` / `--no-rate-guard` and `--rate-guard-threshold` |
| Import-time log format | `--log-format {text,json}`; `--json` remains the independent result-output flag |
| Plugin/repository/project paths | `--plugin-skills-dir`, `--repo-root`, `--projects-dir`, or an explicit `Path` argument |
| Fleet target variables | `--org`, `--repos`, or `.fleet.yml` |
| Coredump target directories | Repeatable `--target-dir` in candidate order |
| Legacy per-thread GitHub pacing | Removed; use `--gh-global-rate` and `--gh-global-burst` |
| Contract and required-smoke gates | Registered pytest options `--run-contract-tests`, `--run-contract-agent`, `--contract-repo`, `--contract-model`, `--require-cli`, and `--require-pi-package-smoke` |
| Parent/child `HEPH_TRUNK_GITHASH` and `HEPH_WORK_REPORT` | Typed pipeline/job fields and explicit result/report paths |
| Generic `HEPHAESTUS_*` config overlay | `load_config()` plus `merge_configs()` with explicitly parsed CLI values |
| `get_proj_root(name)` | `get_repo_root()`, an owning `--repo-root`, or an explicit path |

All timeout flags require positive values except `--phase-timeout`, where zero
or a negative value continues to disable the outer queue-job bound. Established
defaults are preserved; see each command’s `--help` output and the
[migration guide](MIGRATION.md).

## Changing the registry

An exception change must update the canonical typed spec, source reader, TOML
projection and exact reader identity, generated table, and tests atomically.
Wildcards and temporary bulk-access exceptions are not supported. Prefer adding
an explicit CLI option or typed parameter; registry growth requires a concrete
external-tool, platform, authentication, or workflow protocol that cannot be
passed more directly.

