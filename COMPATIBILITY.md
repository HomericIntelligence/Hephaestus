# Compatibility Policy

ProjectHephaestus follows [Semantic Versioning](https://semver.org/).

- **MAJOR** version: backwards-incompatible API changes
- **MINOR** version: new backwards-compatible functionality
- **PATCH** version: backwards-compatible bug fixes

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
3. Deprecated symbols are documented in `CHANGELOG.md`.
4. Symbols are never removed in a patch release.

## Non-Public API

Anything not listed above (including private functions prefixed with `_`,
internal modules, and non-`__all__` symbols) may change without notice between
any versions and is **not** covered by this policy.
