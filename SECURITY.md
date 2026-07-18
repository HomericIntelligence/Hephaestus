# Security Policy

## Supported Versions

Hephaestus supports **Python 3.10–3.13** (`requires-python = ">=3.10"` in
`pyproject.toml`; CI exercises 3.10, 3.11, 3.12, and 3.13). See
[COMPATIBILITY.md](COMPATIBILITY.md) for the full compatibility policy.

| Version | Supported       |
|---------|-----------------|
| 0.9.x   | ✅ Supported    |
| < 0.9   | ❌ End of life  |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

To report a vulnerability, email **<research@villmow.us>** with:

1. A description of the vulnerability and its impact
2. Steps to reproduce the issue
3. Any relevant code or configuration
4. Your assessment of severity (Critical / High / Medium / Low)

You can expect an acknowledgement within 48 hours and a status update within 7 days.
We will coordinate disclosure timing with you once a fix is available.

## Security Considerations

### Threat Model

Hephaestus is a **library and CLI utility repository**, not a
network-facing service. Its security posture reflects that scope:

- **Assets**: source-controlled utility code, the optional `automation`
  product layer, and developer credentials passed via environment variables.
- **Trust boundary**: inputs originate from the local developer, CI runners,
  and the GitHub API. There is no public, unauthenticated request surface.
- **In scope**: unsafe deserialization, command/subprocess injection,
  secret leakage, and supply-chain risk in dependencies.
- **Out of scope** (delegated to the *consuming* service): network rate
  limiting, request authentication/authorization, and DoS protection — this
  repo ships no long-running listener that could be flooded.

### Hardening Controls

- **No hardcoded secrets**: Credentials are always read from environment variables
- **Pickle safety**: `load_data` and `save_data` block pickle by default (`allow_unsafe_deserialization=False`)
- **Subprocess safety**: Avoid passing untrusted input to `run_subprocess`; always use list-form commands (never `shell=True`)
- **HTTPS downloads**: All dataset downloads use HTTPS
- **NATS TLS by default**: Enabled `hephaestus.nats` subscribers default to
  TLS, pass an `SSLContext` to nats-py, and reject non-local plaintext
  `nats://` URLs unless `allow_plaintext=True` is set for an explicit
  non-production exception. Certificate and key material must be provided as
  runtime file paths, never committed to the repository.

### Static Analysis Coverage

Each source surface has a dedicated, CI-gating static security scanner, so
security analysis is not limited to the Python surface:

| Surface | Tool | Gate |
|---------|------|------|
| Python (`hephaestus/`, `scripts/`) | Bandit (medium+) | pre-commit hook + required `security/sast-scan` |
| GitHub Actions workflows | zizmor (medium+, offline; online audits weekly) | pre-commit hook + required `security/workflow-scan` |
| Shell scripts (`**/*.sh`) | ShellCheck | pre-commit hook (all `.sh`) + required `shellcheck` job (`--severity=error`) |
| Secrets | gitleaks + detect-private-key | required `security/secrets-scan` + pre-commit |
| Dependencies | pip-audit | required `security/dependency-scan` + weekly schedule |

For the workflow surface (issue #2151), zizmor is used instead of
CodeQL/Semgrep: it is purpose-built for GitHub Actions security (template
injection, credential persistence, cache poisoning, unpinned/vulnerable
actions, excessive permissions), ships as a PyPI wheel locked in `uv.lock`
under the uv-only toolchain, and runs offline for a deterministic,
network-free PR gate. The
weekly `Security` workflow drops the offline flag to add the API-backed online
audits. Suppressions use inline `# zizmor: ignore[rule]` comments and must
carry a rationale.

### Abuse & Rate Limiting

Because this repository exposes no network service, there is no in-process
request rate limiter. The one external-call surface is the GitHub API
(`hephaestus.github`, `hephaestus.automation`); callers there rely on the
GitHub client's built-in retry/backoff and on GitHub's own per-token rate
limits. Downstream services that embed these utilities are responsible for
applying their own rate limiting and abuse controls at their request edge.

### Dependency Suppression Ledger

Known-but-accepted dependency vulnerabilities are tracked in the pip-audit
suppression ledger (`pyproject.toml`, `[feature.lint.tasks]`). Every suppression
must carry a re-review trigger; this is enforced at commit time by the
`check-pip-audit-ledger-reminder` pre-commit hook
(`scripts/check_pip_audit_ledger_reminder.py`). The weekly `Security`
workflow (`.github/workflows/security.yml`) re-scans for new vulnerabilities.
