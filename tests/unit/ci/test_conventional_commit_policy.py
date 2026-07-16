"""Cross-file regression guards for the Conventional Commit policy."""

import sys
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_conventional_commit import validate_subject  # noqa: E402

REQUIRED_WORKFLOW = REPO_ROOT / ".github/workflows/_required.yml"


def _yaml(path: Path) -> dict[str, Any]:
    """Return a YAML mapping loaded from *path*."""
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _pr_policy_step(name: str) -> dict[str, Any]:
    """Return the named ``pr-policy`` workflow step."""
    steps = _yaml(REQUIRED_WORKFLOW)["jobs"]["pr-policy"]["steps"]
    return next(step for step in steps if step.get("name") == name)


def test_pr_policy_validates_title_and_commit_subjects() -> None:
    """The squash subject and every branch subject have the intended policy."""
    fetch = str(_pr_policy_step("Fetch PR metadata")["run"])
    check = _pr_policy_step("Check 3: PR title and commit subjects follow Conventional Commits")
    run = str(check["run"])

    assert "--json body,title" in fetch
    assert "check_conventional_commit.py --strict -" in run
    assert "commit.message | split" in run
    assert "dependabot[bot]" not in run
    assert "PR_AUTHOR" not in check.get("env", {})


def test_pr_title_edits_rerun_only_the_policy_jobs() -> None:
    """A post-CI title edit cannot bypass the squash-subject gate."""
    text = REQUIRED_WORKFLOW.read_text(encoding="utf-8")

    assert "      - edited" in text
    assert "auto_merge_enabled | auto_merge_disabled | edited)" in text
    assert "Policy-only event ($ACTION)" in text


def test_dependabot_titles_satisfy_strict_policy() -> None:
    """Every configured Dependabot title is authored-form compatible."""
    updates = _yaml(REPO_ROOT / ".github/dependabot.yml")["updates"]

    for update in updates:
        prefix = update["commit-message"]["prefix"]
        assert prefix == "chore(deps)"
        title = f"{prefix}: bump {update['package-ecosystem']} dependencies"
        assert validate_subject(title, allow_machinery=False) is None


def test_policy_documents_history_cutover() -> None:
    """Published history is explicitly grandfathered at the enforcement cutover."""
    text = (REPO_ROOT / "docs/DEFINITION_OF_DONE.md").read_text(encoding="utf-8")
    assert "PR that closes issue #2157" in text
    assert "grandfathered" in text
    assert "must not be rewritten" in text
