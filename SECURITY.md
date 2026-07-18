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

### Safe Harbor & Scope Eligibility

We consider security research conducted in good faith and in line with this
policy to be authorized. For eligible research we
**will not pursue or support legal action** against you, and we waive
restrictions in our repository terms that would otherwise prohibit that
research, to the extent those restrictions would conflict with it.

Research is **eligible** for safe harbor when all of the following hold:

- It targets the assets named in the [threat model](#threat-model): code in
  this repository and your **own** local installation of it.
- It respects the reporting channel above — no public disclosure before a
  coordinated fix, and no access to, modification of, or retention of data
  that is not yours (if you encounter such data, stop and report immediately).
- It stays within the threat model's **in-scope** classes (unsafe
  deserialization, command/subprocess injection, secret leakage, dependency
  supply chain).

Research is **ineligible** (voids safe harbor) when it involves:

- Social engineering, phishing, or physical attacks against maintainers,
  contributors, or infrastructure operators.
- Denial of service or resource-exhaustion testing against shared
  infrastructure (GitHub, PyPI, CI runners) — DoS is out of scope per the
  threat model in any case.
- Testing third-party services (GitHub, PyPI, NATS providers) rather than
  this repository's code; report those to the affected vendor instead.
- Automated scanning that generates spam issues, emails, or pull requests.

If you are unsure whether planned research is eligible, email
**<research@villmow.us>** first and we will clarify before you proceed.

### Remediation Handling

After the 48-hour acknowledgement, reports move through a defined lifecycle:

1. **Triage** — we validate the report and assign a severity (aligned with
   your assessment where possible) within 7 days, as part of the status
   update promised above.
2. **Remediation targets** — from triage, we aim to land a fix within:
   **Critical**: 7 days · **High**: 30 days · **Medium**: 60 days ·
   **Low**: 90 days or next scheduled release.
3. **Release & advisory** — fixes ship in a signed `vX.Y.Z` release; for
   Critical/High issues we publish a GitHub Security Advisory crediting the
   reporter (unless you prefer to remain anonymous).
4. **Coordinated disclosure** — we ask that you hold public disclosure until
   a fix is released or **90 days** from the acknowledgement, whichever comes
   first; we will tell you when the fix lands and coordinate timing, and we
   may ask for a short extension for actively exploited or unusually complex
   issues.

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
