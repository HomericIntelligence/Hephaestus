"""Tests for centralized durable-diagnostic redaction."""

from hephaestus.automation.pipeline.diagnostics import (
    bounded_pipeline_diagnostic,
    redact_diagnostic_text,
)
from hephaestus.diagnostics import redact_truncated_diagnostic_prefix


def _pem_marker(action: str, key_type: str) -> str:
    """Build a synthetic PEM marker without a source-level secret signature."""
    return "-----" + action + " " + key_type + "-----"


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
    begin = _pem_marker("BEGIN", "RSA PRIVATE KEY")
    end = _pem_marker("END", "RSA PRIVATE KEY")
    payload = f"{begin}\nMIIEpA==\n{end}\nafter"
    result = redact_diagnostic_text(payload)
    assert "MIIEpA==" not in result
    assert "<redacted>" in result
    assert result.endswith("after")


def test_redacts_incomplete_private_key_block_through_end_of_stream() -> None:
    """An incomplete PEM block cannot persist its body."""
    begin = _pem_marker("BEGIN", "PRIVATE KEY")
    key_material = "AAAA" * 24
    diagnostic = f"before\n{begin}\n{key_material}\n{key_material}"

    result = bounded_pipeline_diagnostic(diagnostic, limit=4000)

    assert result == "before\n<redacted>"
    assert key_material not in result


def test_combined_diagnostic_redacts_pem_before_git_assignments() -> None:
    """The combined helper masks a PEM block before Git assignment rules."""
    begin = _pem_marker("BEGIN", "PRIVATE KEY")
    end = _pem_marker("END", "PRIVATE KEY")
    key_material = "TEST ONLY PRIVATE KEY BODY"
    diagnostic = f"before\nclient_secret={begin}\n{key_material}\n{end}\nafter"

    result = bounded_pipeline_diagnostic(diagnostic, limit=200)

    assert result == "before\nclient_secret=<redacted>\nafter"
    assert key_material not in result


def test_leaves_plain_diagnostics_unchanged() -> None:
    """Non-secret diagnostic text passes through byte-for-byte unchanged."""
    text = "pytest output duplicate ADR number 0027\n1 failed in 0.3s"
    assert redact_diagnostic_text(text) == text


def test_bounded_diagnostic_is_idempotent_and_keeps_runtime_values() -> None:
    """Combined redaction keeps its sentinels and ordinary runtime values."""
    diagnostic = (
        "token=private-value\n"
        "podman:hephaestus-ci\n"
        "cache:hephaestus-ci\n"
        "git@example.invalid:org/repository.git\n"
        "github.com:org/private-repo"
    )

    result = bounded_pipeline_diagnostic(diagnostic, limit=200)

    assert result == (
        "token=<redacted-value>\n"
        "podman:hephaestus-ci\n"
        "cache:hephaestus-ci\n"
        "<redacted-git-url>\n"
        "<redacted-git-url>"
    )
    assert bounded_pipeline_diagnostic(result, limit=200) == result


def test_truncated_prefix_masks_terminating_payload_after_every_marker_suffix() -> None:
    """Each marker suffix masks a long first payload fragment through its terminator."""
    key_types = (
        "PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "RSA PRIVATE KEY",
        "DSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
    )
    markers = tuple(_pem_marker("BEGIN", key_type) for key_type in key_types)
    separators = ("\n", "\r\n", r"\n", r"\r\n")
    wrappers = ("", " ", "\t", "\r", '"', "'", "\\")
    terminators = (" ", "\t", "\r", ",", ";", "}", '"', "'", "\\")
    payload = "A" * 4100

    for marker in markers:
        for cut in range(1, len(marker)):
            for separator in separators:
                for wrapper in wrappers:
                    for terminator in terminators:
                        diagnostic = (
                            marker[cut:] + separator + wrapper + payload + terminator + "after\n"
                        )

                        result = redact_truncated_diagnostic_prefix(diagnostic)

                        assert result == "<redacted-value>" + terminator + "after\n", (
                            marker,
                            cut,
                            separator,
                            wrapper,
                            terminator,
                        )
                        assert payload[-4000:] not in result
