"""Generate skills/repo-analyze*/SKILL.md from skills/_repo_analyze_common/ partials.

Single source of truth for the six repo-analyze* skill variants. Mirrors the
drift-check pattern from check_version_single_source.

Entry point: ``hephaestus-check-repo-analyze-skills`` (see pyproject.toml).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from string import Template

import yaml

from hephaestus.cli.utils import create_validation_parser, emit_json_status

# This module lives at hephaestus/validation/, so the repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
COMMON_DIR = REPO_ROOT / "skills" / "_repo_analyze_common"
SKILLS_DIR = REPO_ROOT / "skills"


def _load_partial(name: str) -> str:
    """Load a partial file, returning empty string if name is empty."""
    if not name:
        return ""
    return (COMMON_DIR / name).read_text(encoding="utf-8").rstrip("\n") + "\n"


def _task_for_mode(template_name: str) -> str:
    """Generate the <task> paragraph based on the template mode."""
    if "strict" in template_name:
        return (
            "Perform an exhaustive completeness and quality audit of the current "
            "repository with STRICT grading standards.\n\n"
            "Analyze every section defined below. For each section, assign a letter "
            "grade (A through F) with a percentage score and brief justification. "
            "Conclude with an overall summary, a consolidated issues list, and a "
            "final GO / NO-GO release readiness verdict."
        )
    elif "quick" in template_name:
        return (
            "Perform a fast health check of the current repository to catch "
            "showstoppers.\n\n"
            "Analyze each section defined below. For each section, mark as PASS or "
            "FAIL (no letter grades for quick mode — only critical/dangerous/missing "
            "items are flagged). Conclude with a summary and a final PASS / FAIL "
            "verdict."
        )
    else:
        return (
            "Perform a comprehensive completeness and quality audit of the current "
            "repository (rooted at the current working directory).\n\n"
            "Analyze every section defined below. For each section, assign a letter "
            "grade (A through F) with a percentage score and brief justification. "
            "Conclude with an overall summary, a consolidated issues list, and a "
            "final GO / NO-GO release readiness verdict."
        )


def _strict_warning() -> str:
    """Return the strict-mode warning block."""
    return (
        "> ⚠️ **Strict Mode:** This variant starts from an F and grades UP based "
        "on concrete evidence. Every grade requires justification. No grade "
        "inflation to be polite."
    )


def _quick_warning() -> str:
    """Return the quick-mode warning block."""
    return (
        "> ⚠️ **Quick Mode:** This variant checks only for showstoppers (broken, "
        "dangerous, or fundamentally missing). Defaults to PASS unless a critical "
        "blocker is found."
    )


def _usage_for_coverage(coverage_report: str, sections_file: str = "") -> str:
    """Generate the usage paragraph based on coverage mode and section count."""
    if coverage_report:
        if "8" in sections_file:
            return (
                "Run this from the root directory of the repository you want to audit. "
                "This variant dispatches one Sonnet agent per section (8 sections "
                "→ 2 waves of 4 agents, max 5 concurrent) so the entire file tree is "
                "covered without overflowing one agent's context."
            )
        else:
            return (
                "Run this from the root directory of the repository you want to audit. "
                "This variant dispatches one Sonnet agent per audit section (15 sections "
                "→ 3 waves of 5 agents, max 5 concurrent) so the entire file tree is "
                "covered without overflowing one agent's context."
            )
    else:
        return (
            "Run this from the root directory of the repository you want to audit. "
            "The agent will explore the current working directory as the repo root."
        )


def _render(variant: dict[str, str]) -> str:
    """Render a single variant's SKILL.md from template + partials."""
    template_path = COMMON_DIR / "templates" / variant["template"]
    template = Template(template_path.read_text(encoding="utf-8"))
    raw = template.safe_substitute(
        skill_name=variant["name"],
        description=variant["description"].strip(),
        principles_block=_load_partial("principles.md"),
        rubric_block=_load_partial(variant["rubric"]),
        sections_block=_load_partial(variant["sections"]),
        methodology_block=_load_partial(variant["methodology"]),
        output_format_block=_load_partial(variant["output_format"]),
        coverage_report_block=_load_partial(variant.get("coverage_report", "")),
        h1_suffix=variant.get("h1_suffix", ""),
        intro_paragraph=variant["intro"].strip(),
        vs_callout=variant.get("vs_callout", "").strip(),
        task_paragraph=_task_for_mode(variant["template"]),
        strict_warning=_strict_warning() if "strict" in variant["template"] else "",
        quick_warning=_quick_warning() if "quick" in variant["template"] else "",
        usage_paragraph=_usage_for_coverage(
            variant.get("coverage_report", ""), variant.get("sections", "")
        ),
    )
    # Collapse multiple consecutive blank lines to a single blank line so the
    # generated files pass markdownlint MD012 regardless of partial-file trailing
    # whitespace or empty substitution variables (e.g. vs_callout).
    return re.sub(r"\n{3,}", "\n\n", raw)


def main(argv: list[str] | None = None) -> int:
    """Generate repo-analyze* skill SKILL.md files from partials."""
    parser = create_validation_parser(
        "Generate repo-analyze* skill SKILL.md files from partials",
        include_repo_root=False,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        default=True,
        help="Check that SKILL.md files match rendered templates (default)",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="Regenerate SKILL.md files in place",
    )
    args = parser.parse_args(argv)

    spec = yaml.safe_load((COMMON_DIR / "variants.yaml").read_text(encoding="utf-8"))
    drift: list[str] = []
    for variant in spec["variants"]:
        rendered = _render(variant)
        target = SKILLS_DIR / variant["name"] / "SKILL.md"
        if args.write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered, encoding="utf-8", newline="\n")
        else:
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current != rendered:
                drift.append(variant["name"])
    has_drift = bool(drift) and not args.write
    exit_code = 1 if has_drift else 0
    remediation = "Run: pixi run --environment default hephaestus-check-repo-analyze-skills --write"

    if args.json:
        message = f"Drift detected in: {', '.join(drift)}" if has_drift else "No drift detected"
        emit_json_status(
            exit_code,
            message=message,
            drift=drift,
            remediation=remediation if has_drift else None,
        )
        return exit_code

    if has_drift:
        print(f"Drift detected in: {', '.join(drift)}", file=sys.stderr)
        print(remediation, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
