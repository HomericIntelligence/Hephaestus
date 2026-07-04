import textwrap
from pathlib import Path

from hephaestus.validation import skill_merge_method
from hephaestus.validation.skill_merge_method import scan


def _make_skill(tmp_path: Path, name: str, body: str) -> Path:
    skill_dir = tmp_path / "skills" / name
    skill_dir.mkdir(parents=True)
    p = skill_dir / "SKILL.md"
    p.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return p


def test_flags_hardcoded_rebase(tmp_path: Path) -> None:
    """Verify detection of hardcoded --rebase flag."""
    _make_skill(tmp_path, "bad", "```bash\ngh pr merge --auto --rebase\n```\n")
    findings = scan(tmp_path)
    assert len(findings) == 1
    assert findings[0][1] == 2


def test_flags_hardcoded_squash_and_merge(tmp_path: Path) -> None:
    """Verify detection of hardcoded --squash and --merge flags."""
    _make_skill(tmp_path, "bad1", "```bash\ngh pr merge $P --auto --squash --repo X/Y\n```\n")
    _make_skill(tmp_path, "bad2", "```bash\ngh pr merge --auto --merge\n```\n")
    assert len(scan(tmp_path)) == 2


def test_allows_marked_example_block(tmp_path: Path) -> None:
    """Verify that marked example blocks are excluded from linting."""
    _make_skill(
        tmp_path,
        "good",
        """
        <!-- merge-method-allowed: example -->
        ```bash
        gh pr merge --auto --rebase   # OLD: do not copy
        ```
        """,
    )
    assert scan(tmp_path) == []


def test_marker_only_applies_to_immediate_block(tmp_path: Path) -> None:
    """Verify that marker only exempts the immediately following code block."""
    _make_skill(
        tmp_path,
        "tricky",
        """
        <!-- merge-method-allowed: example -->
        ```bash
        gh pr merge --auto --rebase
        ```

        some prose

        ```bash
        gh pr merge --auto --squash
        ```
        """,
    )
    findings = scan(tmp_path)
    assert len(findings) == 1
    assert "--squash" in findings[0][2]


def test_clean_repo_passes(tmp_path: Path) -> None:
    """Verify that repos with no violations pass the check."""
    _make_skill(tmp_path, "fine", "# nothing to see here\n")
    assert scan(tmp_path) == []


def test_real_repo_skills_pass() -> None:
    """The real skills/ tree must be clean after this PR lands."""
    repo_root = Path(__file__).resolve().parents[3]
    assert scan(repo_root) == []


def test_default_repo_root_points_at_checkout_root() -> None:
    """The flattened module default resolves the repository root."""
    repo_root = Path(__file__).resolve().parents[3]
    assert repo_root == skill_merge_method._DEFAULT_REPO_ROOT
