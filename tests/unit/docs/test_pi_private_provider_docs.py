"""Tests for sanitized Pi private-provider documentation."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PI_DOC = REPO_ROOT / "docs" / "pi-private-provider.md"
CONFIG_SUFFIXES = {".json", ".toml", ".yaml", ".yml", ".env", ".ini", ".cfg"}


def _is_pi_config_surface(path: str) -> bool:
    p = Path(path)
    parts = {part.lower() for part in p.parts}
    stem_tokens = set(re.split(r"[-_.]+", p.stem.lower()))
    return p.suffix.lower() in CONFIG_SUFFIXES and (
        ".pi" in parts or "pi" in parts or "pi" in stem_tokens
    )


def test_pi_private_provider_docs_are_sanitized() -> None:
    """Docs should describe local setup using placeholders only."""
    text = PI_DOC.read_text(encoding="utf-8")

    assert "~/.pi/agent/models.json" in text
    assert "HEPH_PI_MODEL" in text
    assert ".heph-private-denylist" in text
    assert "--staged --tracked" in text
    assert "<operator-local-alias>" in text
    assert "https://" not in text
    assert "http://" not in text


def test_no_tracked_pi_provider_config_surfaces_exist() -> None:
    """Tracked config-like Pi paths would invite private provider details."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    paths = [p.decode() for p in result.stdout.split(b"\0") if p]

    assert [p for p in paths if _is_pi_config_surface(p)] == []
