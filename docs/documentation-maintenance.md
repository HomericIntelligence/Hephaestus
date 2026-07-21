# Documentation Maintenance

Living normative documentation includes root policy files, `.github/**/*.md`,
`scripts/README.md`, and `docs/**/*.md`, including `docs/specs/**/*.md`.
Accepted ADR bodies and component-scoped release-note bodies are point-in-time
records; their README/index files remain living documentation.

Ownership follows [CODEOWNERS](../.github/CODEOWNERS).

| Surface | Maintained source | Review trigger | Validation |
| --- | --- | --- | --- |
| Roadmap focus | Open GitHub epics, audit findings, and the [release checklist](RELEASING.md#pre-release-checklist) | Release, epic state change, or priority change | `validate_roadmap_maintenance` |
| Automation architecture | [`ROUTES`](../hephaestus/automation/pipeline/routing.py) and [pipeline stages](../hephaestus/automation/pipeline/stages/) | Pipeline route or stage contract change | Semantic-source validation |
| Required checks | [Required workflow](../.github/workflows/_required.yml), [test workflow](../.github/workflows/test.yml), and live GitHub audit | Workflow, branch-protection, or ruleset change | Local YAML validation plus documented live audit |
| CLI inventory | [`pyproject.toml [project.scripts]`](../pyproject.toml) | Console-script registration change | `check_cli_table_sync` |
| Coverage floor | [`pyproject.toml [tool.coverage.report]`](../pyproject.toml) | Coverage configuration change | `hephaestus-check-doc-config` |

Run the read-only guard with:

```bash
uv run python -m hephaestus.validation.doc_maintenance --repo-root .
```
