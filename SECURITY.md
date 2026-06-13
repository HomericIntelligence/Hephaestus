# Security Policy

## Supported Versions

ProjectHephaestus supports **Python 3.10–3.13** (`requires-python = ">=3.10"` in
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

- **No hardcoded secrets**: Credentials are always read from environment variables
- **Pickle safety**: `load_data` and `save_data` block pickle by default (`allow_unsafe_deserialization=False`)
- **Subprocess safety**: Avoid passing untrusted input to `run_subprocess`; always use list-form commands (never `shell=True`)
- **HTTPS downloads**: All dataset downloads use HTTPS
