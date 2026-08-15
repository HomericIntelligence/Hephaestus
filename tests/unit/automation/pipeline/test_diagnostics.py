"""Tests for centralized durable-diagnostic redaction."""

from hephaestus.automation.pipeline.diagnostics import redact_diagnostic_text


def test_redacts_github_tokens() -> None:
    """A ghp_-style token embedded in a URL is masked in full."""
    token = "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyzABCDE"
    assert redact_diagnostic_text(f"clone https://{token}@x") == "clone https://<redacted>@x"


def test_redacts_authorization_headers_keeping_prefix() -> None:
    """Authorization header values keep the scheme prefix but mask the token."""
    assert (
        redact_diagnostic_text("Authorization: Bearer abcdef1234567890")
        == "Authorization: Bearer <redacted>"
    )


def test_redacts_key_value_credentials() -> None:
    """key=value and key:value credential assignments are masked."""
    assert redact_diagnostic_text("token=sekret-value-here") == "token=<redacted>"
    assert redact_diagnostic_text("password: hunter2") == "password: <redacted>"


def test_redacts_url_credentials_keeping_user() -> None:
    """user:pass@ URL authorities keep the user but mask the password."""
    assert (
        redact_diagnostic_text("https://user:pass@host.com/path")
        == "https://user:<redacted>@host.com/path"
    )


def test_redacts_aws_and_openai_tokens() -> None:
    """AKIA-style and sk-live- style tokens are masked as whole values."""
    aws_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    openai_key = "sk-live-" + "abcdefghijklmnopqrst"
    assert redact_diagnostic_text(aws_key) == "<redacted>"
    assert redact_diagnostic_text(openai_key) == "<redacted>"


def test_redacts_private_key_blocks() -> None:
    """PEM private-key blocks are masked while surrounding text survives."""
    begin = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
    end = "-----END " + "RSA PRIVATE KEY" + "-----"
    payload = f"{begin}\nMIIEpA==\n{end}\nafter"
    result = redact_diagnostic_text(payload)
    assert "MIIEpA==" not in result
    assert "<redacted>" in result
    assert result.endswith("after")


def test_leaves_plain_diagnostics_unchanged() -> None:
    """Non-secret diagnostic text passes through byte-for-byte unchanged."""
    text = "pytest output duplicate ADR number 0027\n1 failed in 0.3s"
    assert redact_diagnostic_text(text) == text
