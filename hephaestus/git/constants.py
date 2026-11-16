"""Constants shared by the git package modules.

Issue #3509: ``DEFAULT_STATE_DIR`` gets its library home here so the
git-lifecycle modules in ``hephaestus/automation/`` stop importing the
pydantic-backed ``hephaestus.automation.models`` for one constant.
"""

from __future__ import annotations

DEFAULT_STATE_DIR: str = "build/.issue_implementer"
