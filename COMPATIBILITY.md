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

ProjectHephaestus ships 19 subpackages with different maturity levels. Only the
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
  Emits a `DeprecationWarning` when called. Scheduled for removal no earlier than
  the next major version after 1.0.

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
| `get_config_value` | 0.2.0 | High-level config lookup with env overlay |
| `get_setting` | 0.1.0 | Dot-notation access to nested config dict |
| `load_config` | 0.1.0 | Load YAML/JSON config file |
| `merge_configs` | 0.1.0 | Deep-merge multiple config dicts |
| `merge_with_env` | 0.2.0 | Overlay env vars onto config |

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
| `human_readable_size` | 0.1.0 | Format byte count as human-readable string |
| `retry_with_backoff` | 0.1.0 | Exponential backoff retry decorator |
| `run_subprocess` | 0.1.0 | Execute shell commands with error handling |
| `slugify` | 0.1.0 | Convert text to URL-friendly slug |

## Deprecation Policy

1. Deprecated symbols are announced at least one minor version before removal.
2. Deprecated symbols emit a `DeprecationWarning` when called.
3. Deprecated symbols are documented in the GitHub release notes for the version that introduced the deprecation.
4. Symbols are never removed in a patch release.

## Non-Public API

Anything not listed above (including private functions prefixed with `_`,
internal modules, and non-`__all__` symbols) may change without notice between
any versions and is **not** covered by this policy.
