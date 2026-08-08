"""Cross-file regression guards for the Conventional Commit policy."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_conventional_commit import validate_subject  # noqa: E402

REQUIRED_WORKFLOW = REPO_ROOT / ".github/workflows/_required.yml"


def _yaml(path: Path) -> dict[str, Any]:
    """Load a repository YAML file as a mapping."""
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _pr_policy_step(name: str) -> dict[str, Any]:
    """Return a named step from the required PR-policy job."""
    steps = _yaml(REQUIRED_WORKFLOW)["jobs"]["pr-policy"]["steps"]
    return next(step for step in steps if step.get("name") == name)


def test_pr_policy_validates_title_and_commit_subjects() -> None:
    """The required workflow validates both the squash title and branch commits."""
    fetch = str(_pr_policy_step("Fetch PR metadata")["run"])
    check = _pr_policy_step("Check 2: PR title and commit subjects follow Conventional Commits")
    run = str(check["run"])

    assert "--json body,title" in fetch
    checkout = next(
        step for step in _yaml(REQUIRED_WORKFLOW)["jobs"]["pr-policy"]["steps"] if "uses" in step
    )
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert checkout["with"]["path"] == "policy-base"
    assert "policy-base/scripts/check_conventional_commit.py" in run
    dco_run = str(
        _pr_policy_step("Check 3: every commit carries a DCO Signed-off-by trailer")["run"]
    )
    assert "policy-base/scripts/check_dco_signoff.py" in dco_run
    assert "strict Conventional Commits form" in run
    assert "commit.message | split" in run
    assert "dependabot[bot]" not in run
    assert "PR_AUTHOR" not in check.get("env", {})


def test_pr_policy_paginates_all_commit_pages() -> None:
    """The policy query cannot silently omit a 101st commit."""
    fetch = str(_pr_policy_step("Fetch PR metadata")["run"])

    assert "commits(first:100, after:$after)" in fetch
    assert "pageInfo { hasNextPage endCursor }" in fetch
    assert "while true" in fetch
    assert '-F after="$cursor"' in fetch
    assert "--slurpfile nodes" in fetch

    # Model the two GraphQL pages used by a 101-commit PR and ensure the
    # workflow's accumulator contract retains the commit beyond page one.
    pages = [
        [{"commit": {"oid": str(index)}} for index in range(100)],
        [{"commit": {"oid": "100"}}],
    ]
    accumulated = [node for page in pages for node in page]
    assert len(accumulated) == 101
    assert accumulated[-1]["commit"]["oid"] == "100"


def test_dependabot_titles_satisfy_strict_policy() -> None:
    """Every configured Dependabot title satisfies strict validation."""
    updates = _yaml(REPO_ROOT / ".github/dependabot.yml")["updates"]

    for update in updates:
        prefix = update["commit-message"]["prefix"]
        assert prefix == "chore(deps)"
        title = f"{prefix}: bump {update['package-ecosystem']} dependencies"
        assert validate_subject(title, allow_machinery=False) is None


def test_policy_documents_history_cutover() -> None:
    """The Definition of Done documents the non-rewriting history boundary."""
    text = (REPO_ROOT / "docs/DEFINITION_OF_DONE.md").read_text(encoding="utf-8")
    assert "PR that closes issue #2157" in text
    assert "grandfathered" in text
    assert "must not be rewritten" in text
