"""Structural guard for PRIVACY.md (issue #2175).

Keeps the published privacy/retention/deletion policy present, complete,
free of rot-prone absolute date stamps (see issue #730), grounded in the
real persistence surfaces named in code, and cross-linked from SECURITY.md
and docs/index.md.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PRIVACY = REPO_ROOT / "PRIVACY.md"

REQUIRED_SECTIONS = (
    "## Scope",
    "## Data Inventory",
    "## Retention",
    "## Deletion Procedures",
    "## Data Subject Requests (GDPR)",
    "## Sub-processors",
)

# Persistence surfaces the inventory must name; each is a real default in code.
REAL_PATHS = (
    "build/.issue_implementer",  # hephaestus/automation/models.py:151
    "build/.worktrees",  # hephaestus/automation/worktree_manager.py:104
    "/tmp/crash-bundle/cores",  # hephaestus/forensics/coredump_handler.py:62
)


def test_privacy_policy_exists() -> None:
    """The published policy lives at the repo root as PRIVACY.md."""
    assert PRIVACY.exists(), "PRIVACY.md missing at repo root (issue #2175)"


def test_privacy_policy_has_required_sections() -> None:
    """Every contract section (scope through sub-processors) is present."""
    text = PRIVACY.read_text(encoding="utf-8")
    missing = [s for s in REQUIRED_SECTIONS if s not in text]
    assert not missing, f"PRIVACY.md missing sections: {missing}"


def test_privacy_policy_has_no_hardcoded_date_stamp() -> None:
    """Absolute 'As of YYYY-MM-DD' stamps rot — same rule as SECURITY.md (#730)."""
    text = PRIVACY.read_text(encoding="utf-8")
    assert not re.search(r"As of \d{4}-\d{2}-\d{2}", text), (
        "PRIVACY.md must not carry a rot-prone 'As of YYYY-MM-DD' stamp (issue #730)"
    )


def test_privacy_policy_documents_real_paths() -> None:
    """The inventory must name the actual persistence surfaces in code."""
    text = PRIVACY.read_text(encoding="utf-8")
    missing = [path for path in REAL_PATHS if path not in text]
    assert not missing, f"PRIVACY.md inventory missing real paths: {missing}"


def test_privacy_policy_names_dsr_contact() -> None:
    """The GDPR data-subject-request channel reuses the published contact."""
    text = PRIVACY.read_text(encoding="utf-8")
    assert "research@villmow.us" in text, "PRIVACY.md must publish the DSR contact"


def test_privacy_policy_is_cross_linked() -> None:
    """SECURITY.md and docs/index.md both link to the policy so it stays discoverable."""
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    index = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    assert "PRIVACY.md" in security, "SECURITY.md must link to PRIVACY.md"
    assert "PRIVACY.md" in index, "docs/index.md must link to PRIVACY.md"
