"""Canonical exact registry for process-environment boundary variables."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class EnvVarSpec:
    """Describe one exact approved environment variable and its authority."""

    name: str
    purpose: str
    owner: str
    sensitivity: str
    validation: str
    direction: str
    qualified_readers: tuple[str, ...] = ()
    qualified_writers: tuple[str, ...] = ()


_PARENT_READER = "hephaestus.config.child_environments.read_approved_parent_env"
_GH_READER = "hephaestus.config.child_environments.build_gh_child_env"
_SIGNING_READER = "hephaestus.config.child_environments.build_git_signing_env"


def _parent(
    name: str,
    purpose: str,
    *,
    sensitivity: str = "public",
    validation: str = "string-no-nul",
    readers: tuple[str, ...] = (_PARENT_READER,),
) -> EnvVarSpec:
    return EnvVarSpec(
        name=name,
        purpose=purpose,
        owner="config.child_environments",
        sensitivity=sensitivity,
        validation=validation,
        direction="parent-read, child-forward",
        qualified_readers=readers,
    )


def _child(name: str, purpose: str, writer: str, validation: str) -> EnvVarSpec:
    return EnvVarSpec(
        name=name,
        purpose=purpose,
        owner="config.child_environments",
        sensitivity="public",
        validation=validation,
        direction="child-write",
        qualified_writers=(writer,),
    )


def _workflow(name: str, purpose: str, *readers: str, sensitivity: str = "public") -> EnvVarSpec:
    return EnvVarSpec(
        name=name,
        purpose=purpose,
        owner="ci/workflow helpers",
        sensitivity=sensitivity,
        validation="string-no-nul",
        direction="parent-read",
        qualified_readers=readers,
    )


APPROVED_ENV_VARS: tuple[EnvVarSpec, ...] = (
    _parent("PATH", "Command discovery", validation="non-empty-no-nul"),
    _parent("HOME", "CLI home and configuration lookup", sensitivity="private", validation="path"),
    _parent("USER", "Host identity hint"),
    _parent("LOGNAME", "Host identity hint"),
    _parent("SHELL", "Interactive shell hint", validation="path"),
    _parent("LANG", "Locale stability"),
    _parent("LC_ALL", "Locale override"),
    _parent("LC_CTYPE", "Character encoding"),
    _parent("TZ", "Timezone stability"),
    _parent(
        "TMPDIR",
        "Temporary and runtime directory",
        sensitivity="private",
        validation="path",
        readers=(_PARENT_READER, "hephaestus.github.rate_limit._runtime_base_dir"),
    ),
    _parent("TMP", "Windows temporary directory", sensitivity="private", validation="path"),
    _parent("TEMP", "Windows temporary directory", sensitivity="private", validation="path"),
    _parent("USERPROFILE", "Windows home directory", sensitivity="private", validation="path"),
    _parent(
        "APPDATA", "Windows application configuration", sensitivity="private", validation="path"
    ),
    _parent("LOCALAPPDATA", "Windows application cache", sensitivity="private", validation="path"),
    _parent(
        "XDG_CONFIG_HOME", "Unix configuration directory", sensitivity="private", validation="path"
    ),
    _parent("XDG_CACHE_HOME", "Unix cache directory", sensitivity="private", validation="path"),
    _parent("XDG_DATA_HOME", "Unix data directory", sensitivity="private", validation="path"),
    _parent("SYSTEMROOT", "Windows system root", validation="path"),
    _parent("SystemRoot", "Windows system root alias", validation="path"),
    _parent("WINDIR", "Windows system directory", validation="path"),
    _parent("ComSpec", "Windows command processor", validation="path"),
    _parent("COMSPEC", "Windows command processor alias", validation="path"),
    _parent("PATHEXT", "Windows executable suffixes"),
    _workflow("CI", "CI runtime detection", "scripts.check_license_compatibility.main"),
    _workflow(
        "GITHUB_ACTIONS",
        "GitHub Actions runtime detection",
        "scripts.check_license_compatibility.main",
    ),
    _workflow(
        "GITHUB_EVENT_NAME",
        "Workflow event classification",
        "scripts.check_license_compatibility.main",
    ),
    _workflow(
        "GITHUB_OUTPUT",
        "GitHub step output file",
        "scripts.check_license_compatibility.main",
        sensitivity="private",
    ),
    _workflow(
        "GITHUB_REPOSITORY",
        "Workflow repository identity",
        "hephaestus.github.severity_label.main",
    ),
    _workflow(
        "GITHUB_STEP_SUMMARY",
        "GitHub step summary file",
        "hephaestus.ci.precommit.write_step_summary",
        "scripts.check_license_compatibility.main",
        sensitivity="private",
    ),
    _workflow(
        "GITHUB_WORKSPACE",
        "Workflow checkout root",
        "scripts.check_license_compatibility.main",
        sensitivity="private",
    ),
    _workflow("ISSUE_NUMBER", "Workflow issue identifier", "hephaestus.github.severity_label.main"),
    _workflow(
        "ISSUE_BODY",
        "Untrusted workflow issue body",
        "hephaestus.github.severity_label.main",
        sensitivity="private",
    ),
    _parent(
        "GITHUB_TOKEN",
        "GitHub authentication bridge",
        sensitivity="secret",
        validation="non-empty-no-nul",
        readers=(_GH_READER,),
    ),
    _parent(
        "GH_TOKEN",
        "GitHub CLI authentication bridge",
        sensitivity="secret",
        validation="non-empty-no-nul",
        readers=(_GH_READER,),
    ),
    _parent(
        "SSH_AUTH_SOCK",
        "Git SSH authentication and signing bridge",
        sensitivity="secret",
        validation="path",
        readers=(_SIGNING_READER,),
    ),
    _parent(
        "GPG_TTY",
        "GPG signing terminal bridge",
        sensitivity="private",
        validation="path",
        readers=(_SIGNING_READER,),
    ),
    _child(
        "CLAUDECODE",
        "Claude nested-process marker",
        "hephaestus.config.child_environments.build_claude_child_env",
        "empty",
    ),
    _child(
        "GH_TRACE_ID",
        "GitHub correlation identifier",
        "hephaestus.config.child_environments.with_correlation_id",
        "token",
    ),
    _child(
        "CODEX_HOME",
        "Codex isolated home",
        "hephaestus.config.child_environments.build_codex_child_env",
        "path",
    ),
    _child(
        "GIT_CONFIG_GLOBAL",
        "Scoped Git configuration",
        "hephaestus.config.child_environments.build_git_child_env",
        "path",
    ),
    _child(
        "GIT_CONFIG_NOSYSTEM",
        "Disable system Git configuration",
        "hephaestus.config.child_environments.build_git_child_env",
        "literal-1",
    ),
    _child(
        "GIT_NO_REPLACE_OBJECTS",
        "Disable Git object replacement",
        "hephaestus.config.child_environments.build_git_child_env",
        "literal-1",
    ),
    _child(
        "GIT_PAGER",
        "Disable interactive Git paging",
        "hephaestus.config.child_environments.build_git_child_env",
        "cat-or-empty",
    ),
    _child(
        "GIT_TERMINAL_PROMPT",
        "Disable Git credential prompts",
        "hephaestus.config.child_environments.build_git_child_env",
        "literal-0",
    ),
    _child(
        "NPM_CONFIG_IGNORE_SCRIPTS",
        "Disable package install scripts",
        "hephaestus.config.child_environments.build_pi_child_env",
        "literal-true",
    ),
    _child(
        "PI_TELEMETRY",
        "Disable Pi telemetry",
        "hephaestus.config.child_environments.build_pi_child_env",
        "literal-0",
    ),
    _child(
        "PI_SKIP_VERSION_CHECK",
        "Disable Pi update checks",
        "hephaestus.config.child_environments.build_pi_child_env",
        "literal-1",
    ),
    _child(
        "PI_CODING_AGENT_DIR",
        "Explicit Pi configuration directory",
        "hephaestus.config.child_environments.build_pi_child_env",
        "path",
    ),
    _child(
        "UV_OFFLINE",
        "Offline host verification",
        "hephaestus.config.child_environments.build_host_verification_env",
        "literal-1",
    ),
    _child(
        "UV_NO_SYNC",
        "Disable implicit uv synchronization",
        "hephaestus.config.child_environments.build_host_verification_env",
        "literal-1",
    ),
    _child(
        "UV_PROJECT_ENVIRONMENT",
        "Host verification runtime",
        "hephaestus.config.child_environments.build_host_verification_env",
        "path",
    ),
    _child(
        "UV_CACHE_DIR",
        "Isolated uv cache",
        "hephaestus.config.child_environments.build_host_verification_env",
        "path",
    ),
    _child(
        "RUFF_CACHE_DIR",
        "Isolated Ruff cache",
        "hephaestus.config.child_environments.build_host_verification_env",
        "path",
    ),
    _child(
        "COVERAGE_FILE",
        "Isolated coverage output",
        "hephaestus.config.child_environments.build_host_verification_env",
        "path",
    ),
    _child(
        "PYTHONPYCACHEPREFIX",
        "Isolated bytecode cache",
        "hephaestus.config.child_environments.build_host_verification_env",
        "path",
    ),
    _child(
        "PYTHONDONTWRITEBYTECODE",
        "Disable child bytecode writes",
        "hephaestus.config.child_environments.build_python_phase_env",
        "literal-1",
    ),
    _child(
        "PYTHONPATH",
        "Source checkout for internal Python phases",
        "hephaestus.config.child_environments.build_python_phase_env",
        "path",
    ),
    _child(
        "PYTEST_ADDOPTS",
        "Scoped pytest behavior",
        "hephaestus.config.child_environments.build_host_verification_env",
        "string-no-nul",
    ),
)

APPROVED_ENV_BY_NAME = {spec.name: spec for spec in APPROVED_ENV_VARS}

if len(APPROVED_ENV_BY_NAME) != len(APPROVED_ENV_VARS):
    raise ValueError("environment registry contains duplicate names")


def validate_environment_value(spec: EnvVarSpec, value: str) -> bool:
    """Return whether ``value`` satisfies an approved variable's exact rule."""
    if "\0" in value:
        return False
    rule = spec.validation
    if rule == "string-no-nul":
        return True
    if rule == "non-empty-no-nul":
        return bool(value)
    if rule == "path":
        return bool(value) and Path(value).is_absolute()
    if rule == "empty":
        return value == ""
    if rule == "token":
        return bool(value) and not any(char.isspace() for char in value)
    if rule == "literal-0":
        return value == "0"
    if rule == "literal-1":
        return value == "1"
    if rule == "literal-true":
        return value == "true"
    if rule == "cat-or-empty":
        return value in {"", "cat"}
    raise ValueError(f"unknown environment validation rule: {rule}")


def reader_is_authorized(name: str, qualified_reader: str) -> bool:
    """Return whether an exact qualified reader may read ``name``."""
    spec = APPROVED_ENV_BY_NAME.get(name)
    return spec is not None and qualified_reader in spec.qualified_readers


def writer_is_authorized(name: str, qualified_writer: str) -> bool:
    """Return whether an exact qualified writer may emit ``name``."""
    spec = APPROVED_ENV_BY_NAME.get(name)
    return spec is not None and qualified_writer in spec.qualified_writers


RETIRED_ENV_NAMES = frozenset(
    {
        "PROJECTS_ROOT",
        "FLEET_ORG",
        "FLEET_REPOS",
        "FLEET_GIT_EMAIL",
        "FLEET_SKIP_EMAIL_KEY_CHECK",
        "COREDUMP_TARGET_DIRS",
        "COREDUMP_MAX_BYTES",
        "GH_RATE_LIMIT_PER_SEC",
        "RUN_UNDER_GDB",
        "GDB_CMD_PREFIX",
        "NATS_URL",
        "NATS_TLS",
        "NATS_TLS_CA_FILE",
        "NATS_TLS_CERT_FILE",
        "NATS_TLS_KEY_FILE",
        "NATS_TLS_HOSTNAME",
        "NATS_TLS_HANDSHAKE_FIRST",
        "NATS_ALLOW_PLAINTEXT",
        "NATS_STREAM",
        "NATS_DURABLE_NAME",
        "NATS_INITIAL_BACKOFF_SECONDS",
        "NATS_MAX_BACKOFF_SECONDS",
        "NATS_BACKOFF_MULTIPLIER",
        "HEPH_ADDRESS_REVIEW_AGENT_TIMEOUT",
        "HEPH_ADVISE_AGENT_TIMEOUT",
        "HEPH_ADVISE_MODEL",
        "HEPH_AGENT_AUTH_STATUS_TIMEOUT",
        "HEPH_AGENT_CLONE_TIMEOUT",
        "HEPH_AGENT_DEFAULT_TIMEOUT",
        "HEPH_AGENT_GIT_TIMEOUT",
        "HEPH_AGENT_IMPL_TIMEOUT",
        "HEPH_AGENT_LEARN_TIMEOUT",
        "HEPH_AGENT_PLAN_TIMEOUT",
        "HEPH_AGENT_REBASE_TIMEOUT",
        "HEPH_AGENT_REVIEW_TIMEOUT",
        "HEPH_CI_DRIVER_AGENT_TIMEOUT",
        "HEPH_CI_DRIVER_FORCE",
        "HEPH_CI_POLL_MAX_WAIT",
        "HEPH_CODEX_FINAL_MESSAGE_GRACE",
        "HEPH_DIFF_COLLECT_TIMEOUT",
        "HEPH_DISABLE_PI_AUTOMATION",
        "HEPH_FALLBACK_MODEL",
        "HEPH_FOLLOW_UP_AGENT_TIMEOUT",
        "HEPH_GH_CLI_TIMEOUT",
        "HEPH_GH_TIMEOUT",
        "HEPH_GIT_MESSAGE_AGENT_TIMEOUT",
        "HEPH_GIT_MESSAGE_MODEL",
        "HEPH_IMPLEMENTER_AGENT_TIMEOUT",
        "HEPH_IMPLEMENTER_MODEL",
        "HEPH_LEARN_AGENT_TIMEOUT",
        "HEPH_LEARN_MODEL",
        "HEPH_MNEMOSYNE_OWNER",
        "HEPH_PHASE_TIMEOUT",
        "HEPH_PI_ISOLATION_ADAPTER",
        "HEPH_PI_MODEL",
        "HEPH_PI_PROVIDER",
        "HEPH_PI_SMOKE_LOG_DIR",
        "HEPH_PLANNER_AGENT_TIMEOUT",
        "HEPH_PLANNER_MODEL",
        "HEPH_PLAN_REVIEWER_AGENT_TIMEOUT",
        "HEPH_PLAN_STAGE_TIMEOUT",
        "HEPH_PRE_PR_TEST_TIMEOUT",
        "HEPH_PR_REVIEWER_AGENT_TIMEOUT",
        "HEPH_REVIEWER_MODEL",
        "HEPH_TRUNK_GITHASH",
        "HEPH_WORK_REPORT",
        "HEPHAESTUS_CONTRACT_AGENT",
        "HEPHAESTUS_CONTRACT_MODEL",
        "HEPHAESTUS_CONTRACT_REPO",
        "HEPHAESTUS_CONTRACT_TESTS",
        "HEPHAESTUS_LOG_FORMAT",
        "HEPHAESTUS_PLUGIN_SKILLS_DIR",
        "HEPHAESTUS_RATE_GUARD",
        "HEPHAESTUS_RATE_GUARD_THRESHOLD",
        "HEPHAESTUS_REPO_ROOT",
        "HEPHAESTUS_REQUIRE_CLI",
        "HEPHAESTUS_REQUIRE_PI_PACKAGE_SMOKE",
        "HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT",
        "HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT",
    }
)


__all__ = [
    "APPROVED_ENV_BY_NAME",
    "APPROVED_ENV_VARS",
    "RETIRED_ENV_NAMES",
    "EnvVarSpec",
    "reader_is_authorized",
    "validate_environment_value",
    "writer_is_authorized",
]
