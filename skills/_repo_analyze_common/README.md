# repo-analyze* Skill Common Partials

This directory contains reusable markdown partials and templates that source the six `skills/repo-analyze*/SKILL.md` files. **This is NOT a skill** — there is no `SKILL.md` in this directory.

## How to Edit

1. Modify a partial file (e.g., `principles.md`, `rubric_default.md`) or `variants.yaml`.
2. Run: `pixi run --environment default hephaestus-check-repo-analyze-skills --write`
3. Commit the changed partials + the regenerated SKILL.md files.

The pre-commit hook `hephaestus-check-repo-analyze-skills` enforces that all `SKILL.md` files are up to date with their sources. If you edit a partial and forget to regenerate, the hook will fail and print instructions.

## Structure

- **`principles.md`** — The seven core development principles (KISS, YAGNI, TDD, DRY, SOLID, MODULARITY, POLA).
- **`rubric_default.md`**, **`rubric_strict.md`**, **`rubric_quick.md`** — Grading rubrics for each mode.
- **`sections_15.md`**, **`sections_8.md`** — Audit sections (15 for full variants, 8 for quick).
- **`output_format_full.md`**, **`output_format_quick.md`** — Report output scaffolds.
- **`methodology_sampling.md`**, **`methodology_full.md`** — Coverage methodology descriptions.
- **`coverage_report_block.md`** — Coverage report block (included only in full-coverage variants).
- **`variants.yaml`** — The source of truth for all 6 skill variants (names, descriptions, which partials each uses).
- **`templates/repo_analyze.md.tmpl`**, **`templates/repo_analyze_strict.md.tmpl`**, **`templates/repo_analyze_quick.md.tmpl`** — Template skeletons with `$slot_name` placeholders.
