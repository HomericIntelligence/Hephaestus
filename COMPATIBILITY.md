# Compatibility Policy

ProjectHephaestus follows [Semantic Versioning](https://semver.org/).

- **MAJOR** version: backwards-incompatible API changes
- **MINOR** version: new backwards-compatible functionality
- **PATCH** version: backwards-compatible bug fixes

Upgrading across a major version? See the [migration guide](docs/MIGRATION.md).

## Supported Python Versions

ProjectHephaestus supports **Python 3.10+** (`requires-python = ">=3.10"` in
`pyproject.toml`). CI exercises the package on 3.10, 3.11, 3.12, and 3.13. Dropping
support for a Python minor version is treated as a backwards-incompatible change and
follows the deprecation policy below.

## Versioning: Python Package vs Agent Plugins

ProjectHephaestus ships **independently versioned artifacts**:

- **The Python package** (`homericintelligence-hephaestus`) — version is **tag-driven**
  via hatch-vcs (derived from the latest `vX.Y.Z` git tag; see
  [latest release](https://github.com/HomericIntelligence/ProjectHephaestus/releases/latest)).
  This is the version the Semantic Versioning guarantees in this document apply to.
- **The Claude Code plugin** (`hephaestus`, declared in `.claude-plugin/`) — carries its
  own `version` field (declared in
  [`.claude-plugin/plugin.json`](.claude-plugin/plugin.json)) that tracks the
  skill/command surface, not the Python API.
- **The Codex plugin** (`hephaestus`, declared in `.codex-plugin/`) — carries its
  own `version` field (declared in
  [`.codex-plugin/plugin.json`](.codex-plugin/plugin.json)) and exposes the same
  skill surface through the Codex plugin marketplace metadata in
  [`.agents/plugins/marketplace.json`](.agents/plugins/marketplace.json).

These version numbers are **not** coupled and will not match. A plugin version
says nothing about the Python package version and vice versa. See
[`docs/plugin-installation.md`](docs/plugin-installation.md) for plugin installation.

## Stability Tiers

ProjectHephaestus ships 19 documented subpackages with different maturity levels. Only the
**stable** subpackages below are covered by the [deprecation policy](#deprecation-policy);
**provisional** subpackages may change without notice, even across minor versions.

### Stable

The following subpackages are part of the stable public API surface. Public symbols
(those listed in the module's `__all__` and in the per-package tables below) are
covered by the deprecation policy.

- `hephaestus` (top-level lazy re-exports)
- `hephaestus.cli`
- `hephaestus.config`
- `hephaestus.io`
- `hephaestus.logging`
- `hephaestus.system`
- `hephaestus.utils`
- `hephaestus.version`

### Provisional / internal

The following subpackages contain useful code that is **not** covered by the
stability guarantee — call them at your own risk and pin a specific version. They
may change incompatibly in a minor release.

| Subpackage | Why provisional |
|------------|-----------------|
| `hephaestus.agents` | Agent metadata schema still evolving |
| `hephaestus.automation` | Actively-evolving 3-stage issue/PR pipeline (collapsed from the prior 6-phase design in #677/#679); internals still evolving |
| `hephaestus.benchmarks` | Comparison API is exploratory |
| `hephaestus.ci` | CI helpers are project-specific glue, not a general API |
| `hephaestus.datasets` | Downloader URLs and on-disk layout are not contracted |
| `hephaestus.discovery` | Discovery rules for agents/skills still evolving |
| `hephaestus.forensics` | Coredump/gdb helpers depend on host platform conventions |
| `hephaestus.github` | Only `detect_repo_from_remote`/`local_branch_exists` and the `stats` / `rate_limit` helpers are intended as library API; the CLI `main()`s are not |
| `hephaestus.markdown` | Linting/fixing rules track evolving markdown conventions |
| `hephaestus.nats` | NATS subscriber surface is provisional pending real-world use |
| `hephaestus.resilience` | Implemented but not yet wired into production paths (#469) |
| `hephaestus.validation` | Validation rules track CI policy and evolve with it |

## Console-Script Stability Tiers

ProjectHephaestus installs 51 console scripts via `[project.scripts]` in
`pyproject.toml`. Each is classified into one of three tiers:

- **Stable** — covered by the [deprecation policy](#deprecation-policy). CLI
  name, flags, exit codes, and JSON output schema (when `--json` is passed)
  are versioned API. Currently: none. Promotion criteria: dispatches to a
  Stable subpackage AND has a documented JSON output schema AND has external
  consumers outside this repo.
- **Provisional** — useful from outside but may change incompatibly in a
  minor release. Pin a specific version if you depend on one.
- **Internal** — exists for this repo's own CI/automation. May be removed
  without a deprecation notice. Listed for completeness only.

Note: the **Internal** tier applies only to console scripts; the subpackage
stability tier table above uses only Stable/Provisional. Internal CLIs may
still dispatch to a Stable subpackage — the CLI tier reflects the CLI's
own external-consumer contract, not the underlying library code.

The mapping below is the source of truth; the
`hephaestus-check-cli-tier-docs` validator (run in pre-commit and as a unit
test) fails the build if `[project.scripts]` and this table drift apart. To
bypass a misfiring hook locally use
`SKIP=hephaestus-check-cli-tier-docs git commit -S ...` — never
`--no-verify` (it skips signing too).

| CLI | Tier | Notes |
|-----|------|-------|
| `hephaestus-automation-loop` | Provisional | Dispatches to `hephaestus.automation` (provisional subpackage) |
| `hephaestus-plan-issues` | Provisional | Issue-planning stage of the automation pipeline |
| `hephaestus-implement-issues` | Provisional | Issue-implementation stage |
| `hephaestus-review-prs` | Provisional | PR-review stage |
| `hephaestus-audit-prs` | Provisional | PR-audit stage; validates prior review comments were addressed |
| `hephaestus-agent-stage` | Provisional | Single-stage agent runner |
| `hephaestus-ensure-state-labels` | Internal | Used by this repo's CI label bootstrap |
| `hephaestus-gh` | Provisional | Shell-facing wrapper around the shared `gh_call` adapter |
| `hephaestus-merge-prs` | Provisional | Merge helper using the shared `gh_call` adapter |
| `hephaestus-fleet-sync` | Provisional | Fleet-wide repo sync helper |
| `hephaestus-tidy` | Provisional | Local-branch rebase + cleanup helper |
| `hephaestus-label-severity` | Provisional | Reconciles `severity:*` label from issue-form Severity answer |
| `hephaestus-system-info` | Provisional | Dispatches to Stable `hephaestus.system` but CLI flags still evolving |
| `hephaestus-download-dataset` | Provisional | Dataset URL/layout not contracted |
| `hephaestus-check-python-version` | Internal | Repo CI pre-commit hook |
| `hephaestus-check-test-structure` | Internal | Repo CI pre-commit hook |
| `hephaestus-check-coverage` | Internal | Repo CI pre-commit hook |
| `hephaestus-check-complexity` | Internal | Repo CI pre-commit hook |
| `hephaestus-filter-audit` | Provisional | Audit-output filter; useful externally |
| `hephaestus-validate-schemas` | Provisional | JSON-Schema validator |
| `hephaestus-validate-links` | Internal | Repo CI markdown link check |
| `hephaestus-check-readmes` | Internal | Repo CI README validator |
| `hephaestus-check-skill-catalog` | Internal | Repo CI skill-catalog validator |
| `hephaestus-check-type-aliases` | Internal | Repo CI type-alias validator |
| `hephaestus-check-docstrings` | Internal | Repo CI docstring validator |
| `hephaestus-check-tier-labels` | Internal | Repo CI tier-label validator |
| `hephaestus-fix-markdown` | Provisional | Markdown fixer; useful externally |
| `hephaestus-audit-doc-policy` | Internal | Repo CI doc-policy auditor |
| `hephaestus-check-version-consistency` | Internal | Repo CI version-consistency check |
| `hephaestus-coredump-handler` | Provisional | Coredump capture helper |
| `hephaestus-run-under-gdb` | Provisional | gdb post-mortem helper |
| `hephaestus-check-package-versions` | Internal | Repo CI package-version check |
| `hephaestus-bump-version` | Internal | Repo release-management helper |
| `hephaestus-check-doc-config` | Internal | Repo CI doc-config validator |
| `hephaestus-check-stale-scripts` | Internal | Repo CI stale-script detector |
| `hephaestus-mypy-each-file` | Internal | Repo CI per-file mypy runner |
| `hephaestus-check-links` | Internal | Repo CI link checker |
| `hephaestus-validate-anchors` | Internal | Repo CI anchor validator |
| `hephaestus-scaffold-subpackage` | Internal | Dev scaffolding tool for new subpackages |
| `hephaestus-check-dep-sync` | Internal | Repo CI dependency-sync check |
| `hephaestus-sync-requirements` | Internal | Repo CI requirements sync |
| `hephaestus-bench-precommit` | Internal | Repo CI pre-commit benchmark |
| `hephaestus-check-precommit-versions` | Internal | Repo CI pre-commit version check |
| `hephaestus-check-workflow-inventory` | Internal | Repo CI workflow inventory check |
| `hephaestus-validate-workflow-checkout` | Internal | Repo CI workflow checkout validator |
| `hephaestus-github-stats` | Provisional | GitHub repo-stats helper |
| `hephaestus-agent-stats` | Provisional | Agent-stats helper |
| `hephaestus-validate-agents` | Internal | Repo CI agent-frontmatter validator |
| `hephaestus-check-repo-analyze-skills` | Internal | Repo CI repo-analyze skill generator validator |
| `hephaestus-check-cli-tier-docs` | Internal | Enforces this very table; added in #766 |
| `hephaestus-check-api-table-docs` | Internal | Enforces per-symbol `__all__` documentation in COMPATIBILITY.md |

## Public API

The following symbols are part of the stable public API and are covered by
the deprecation policy below.

### Top-level (`hephaestus`)

| Symbol | Added | Notes |
|--------|-------|-------|
| `__version__` | 0.1.0 | Package version string |
| `ContextLogger` | 0.2.0 | Logger adapter with context binding |
| `ensure_directory` | 0.1.0 | Create directory tree |
| `get_logger` | 0.1.0 | Get a configured logger instance |
| `get_system_info` | 0.3.0 | Collect system/environment information |
| `load_config` | 0.1.0 | Load YAML or JSON configuration files |
| `retry_with_backoff` | 0.1.0 | Exponential backoff retry decorator |
| `setup_logging` | 0.1.0 | Configure root logger |
| `slugify` | 0.1.0 | Convert text to URL-friendly slug |

Lazy-loaded symbols (accessible via `hephaestus.<name>`): `add_logging_args`,
`check_coverage`, `check_max_complexity`, `check_python_version_consistency`,
`check_test_structure`, `COMMAND_REGISTRY`, `confirm_action`, `create_parser`,
`detect_rate_limit`, `filter_audit_results`, `flatten_dict`, `format_output`,
`format_system_info`, `format_table`, `get_config_value`, `get_proj_root`,
`get_repo_root`, `get_setting`, `human_readable_size`, `install_package`,
`load_data`, `merge_configs`, `parse_reset_epoch`, `read_file`, `register_command`,
`run_subprocess`, `safe_write`, `save_data`, `wait_until`, `write_file`, `write_secure`.

**Deprecated lazy-loaded symbols** (covered by the deprecation policy until
removal):

- `retry_with_jitter` — superseded by `retry_with_backoff(jitter=True, max_delay=...)`.
  Emits a `DeprecationWarning` both when accessed via `hephaestus.retry_with_jitter`
  and when called. Scheduled for removal no earlier than the next major version after 1.0.

### `hephaestus.logging`

| Symbol | Added | Notes |
|--------|-------|-------|
| `ContextLogger` | 0.2.0 | Logger adapter with context binding |
| `JsonFormatter` | 0.5.0 | Structured JSON log formatter |
| `get_logger` | 0.1.0 | Get a configured logger instance |
| `setup_logging` | 0.1.0 | Configure root logger |

### `hephaestus.config`

| Symbol | Added | Notes |
|--------|-------|-------|
| `check_dep_sync` | 0.3.0 | Check pixi.toml ↔ requirements drift |
| `check_requirements_up_to_date` | 0.3.0 | Verify requirements file is current |
| `generate_requirements_content` | 0.3.0 | Render requirements.txt content from pixi deps |
| `get_config_value` | 0.2.0 | High-level config lookup with env overlay — **(deprecated)**, use `load_config` + `merge_with_env` + `get_setting` |
| `get_setting` | 0.1.0 | Dot-notation access to nested config dict |
| `load_config` | 0.1.0 | Load YAML/JSON config file |
| `load_yaml_config` | 0.1.0 | Load a YAML config file |
| `merge_configs` | 0.1.0 | Deep-merge multiple config dicts |
| `merge_with_env` | 0.2.0 | Overlay env vars onto config |
| `parse_pixi_toml` | 0.3.0 | Parse pixi.toml dependency tables |
| `parse_requirements` | 0.3.0 | Parse a requirements.txt file |
| `sync_requirements` | 0.3.0 | Sync requirements.txt from pixi deps |
| `validate_config` | 0.1.0 | Validate a config dict against a schema |

**Deprecated symbols** (covered by the deprecation policy until removal):

- `get_config_value` — superseded by the explicit pipeline `load_config()` →
  `merge_with_env()` → `get_setting()`. Emits a `DeprecationWarning` when called.
  Scheduled for removal no earlier than the next major version after 1.0.

### `hephaestus.io`

| Symbol | Added | Notes |
|--------|-------|-------|
| `ensure_directory` | 0.1.0 | Create directory tree |
| `load_data` | 0.2.0 | Deserialize JSON/YAML |
| `read_file` | 0.1.0 | Read file content |
| `safe_write` | 0.1.0 | Write with optional backup |
| `save_data` | 0.2.0 | Serialize JSON/YAML |
| `write_file` | 0.1.0 | Write file content |
| `write_secure` | 0.4.0 | Write with restrictive permissions |

### `hephaestus.utils`

| Symbol | Added | Notes |
|--------|-------|-------|
| `flatten_dict` | 0.1.0 | Flatten nested dict |
| `get_proj_root` | 0.1.0 | Locate the project root directory |
| `get_repo_root` | 0.1.0 | Locate the git repository root |
| `human_readable_size` | 0.1.0 | Format byte count as human-readable string |
| `install_package` | 0.2.0 | Install a Python package at runtime via pip |
| `install_signal_handlers` | 0.3.0 | Register terminal-restoring signal handlers |
| `is_network_error` | 0.2.0 | Classify an exception as a transient network error |
| `restore_terminal` | 0.3.0 | Restore terminal state after a raw-mode session |
| `retry_on_network_error` | 0.2.0 | Retry decorator scoped to network errors |
| `retry_with_backoff` | 0.1.0 | Exponential backoff retry decorator |
| `retry_with_jitter` | 0.1.0 | Jittered backoff retry decorator — **(deprecated)**, use `retry_with_backoff(jitter=True, max_delay=...)` |
| `run_subprocess` | 0.1.0 | Execute shell commands with error handling |
| `slugify` | 0.1.0 | Convert text to URL-friendly slug |
| `terminal_guard` | 0.3.0 | Context manager that saves/restores terminal state |

### `hephaestus.cli`

> The `Added` version for pre-1.0 symbols is a best-effort historical anchor
> (inferred from the introducing commit/PR), not an authoritative record.

| Symbol | Added | Notes |
|--------|-------|-------|
| `Colors` | 0.1.0 | ANSI color constants for terminal output |
| `CommandRegistry` | 0.1.0 | Registry type for CLI subcommands |
| `COMMAND_REGISTRY` | 0.1.0 | Default shared `CommandRegistry` instance |
| `DRY_RUN_HELP_CAVEAT` | 0.9.0 | Standard help text appended for dry-run flags |
| `add_dry_run_arg` | 0.9.0 | Add a `--dry-run` flag to a parser |
| `add_github_throttle_args` | 0.9.0 | Add GitHub API throttle flags to a parser |
| `add_json_arg` | 0.6.0 | Add a `--json` output flag to a parser |
| `add_logging_args` | 0.1.0 | Add `--verbose`/`--quiet` logging flags |
| `add_version_arg` | 0.1.0 | Add a `--version` flag to a parser |
| `configure_github_throttle_from_args` | 0.9.0 | Apply parsed throttle args to the GitHub client |
| `confirm_action` | 0.1.0 | Interactive yes/no confirmation prompt |
| `create_parser` | 0.1.0 | Build a standard `ArgumentParser` |
| `emit_json_status` | 0.6.0 | Emit a structured JSON status envelope |
| `format_output` | 0.1.0 | Format a value for human-readable output |
| `format_table` | 0.1.0 | Render rows as an aligned text table |
| `register_command` | 0.1.0 | Decorator registering a CLI subcommand |

### `hephaestus.system`

> The `Added` version for pre-1.0 symbols is a best-effort historical anchor, not an authoritative record.

| Symbol | Added | Notes |
|--------|-------|-------|
| `format_system_info` | 0.3.0 | Format collected system info for display |
| `get_system_info` | 0.3.0 | Collect system/environment information |

### `hephaestus.version`

> The `Added` version for pre-1.0 symbols is a best-effort historical anchor, not an authoritative record.

| Symbol | Added | Notes |
|--------|-------|-------|
| `VersionManager` | 0.1.0 | Read/write/bump the project version |
| `bump_version` | 0.1.0 | Increment a semantic version component |
| `check_package_version_consistency` | 0.1.0 | Verify installed-package versions agree |
| `check_version_consistency` | 0.1.0 | Verify project version files agree |
| `parse_version` | 0.1.0 | Parse a semantic version string |

## Deprecation Policy

1. Deprecated symbols are announced at least one minor version before removal.
2. Deprecated symbols emit a `DeprecationWarning` when called.
3. Deprecated symbols are documented in the GitHub release notes for the version that introduced the deprecation.
4. Symbols are never removed in a patch release.

## Non-Public API

Anything not listed above (including private functions prefixed with `_`,
internal modules, and non-`__all__` symbols) may change without notice between
any versions and is **not** covered by this policy.
