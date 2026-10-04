# Documentation Maintenance

Living normative documentation includes root policy files, `.github/**/*.md`,
`scripts/README.md`, and `docs/**/*.md`, including `docs/specs/**/*.md`.
Specifications are normative and remain in the maintained corpus. Accepted ADR
bodies and component-scoped release-note bodies are point-in-time records; their
README/index files remain living documentation. Generated API output and scratch
environments are excluded from the corpus.

Ownership follows [CODEOWNERS](../.github/CODEOWNERS).

| Surface | Maintained source | Review trigger | Validation |
|---|---|---|---|
| Roadmap focus | Open GitHub epics and audit findings; [release checklist](RELEASING.md) | Release, epic state change, or priority change | `validate_roadmap_maintenance` |
| Architecture | Linked source modules, especially [`ROUTES`](../hephaestus/automation/pipeline/routing.py) | Change to a cited module or pipeline contract | semantic-source and link validation |
| Prompt specifications | [`PromptCatalog`](../hephaestus/prompts/catalog.py) and packaged templates | Prompt catalog, layout, or override change | semantic-source validation |
| Required checks | [required workflow](../.github/workflows/_required.yml) plus live GitHub audit | Workflow, branch-protection, or ruleset change | local YAML validation plus documented live audit |
| CLI inventory | [`pyproject.toml [project.scripts]`](../pyproject.toml) | Console-script registration change | `check_cli_table_sync` |
| Coverage floor | [`pyproject.toml [tool.coverage.report]`](../pyproject.toml) | Coverage configuration change | `hephaestus-check-doc-config` |

Run the offline guard from the repository root:

```bash
uv run python -m hephaestus.validation.doc_maintenance --repo-root .
```

The guard performs no writes and does not query GitHub. Live GitHub state is
reconciled by the documented release and required-check audits when their
triggers occur.
