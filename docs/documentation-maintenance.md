# Documentation Maintenance

Living normative documentation includes root policy files, `.github/**/*.md`,
`scripts/README.md`, and `docs/**/*.md`, including `docs/specs/**/*.md`.
Accepted ADR bodies and component-scoped release-note bodies are point-in-time
records; their README/index files remain living documentation.

Ownership follows [CODEOWNERS](../.github/CODEOWNERS).

| Surface | Maintained source | Review trigger | Validation |
|---|---|---|---|
| Writing standard | [ASD-STE100 policy](asd-ste100.md) and the official standard | Any new or revised English technical prose | Author and reviewer check against the official standard |
| Roadmap focus | Open GitHub epics and audit findings; [release checklist](RELEASING.md) | Release, epic state change, or priority change | `validate_roadmap_maintenance` |
| Architecture | Linked source modules, especially [`ROUTES`](../hephaestus/automation/pipeline/routing.py) | Change to a cited module or pipeline contract | semantic-source and link validation |
| Prompt specifications | [`PromptCatalog`](../hephaestus/prompts/catalog.py) and packaged templates | Prompt catalog, layout, or override change | semantic-source validation |
| Required checks | [required workflow](../.github/workflows/_required.yml) plus live GitHub audit | Workflow, branch-protection, or ruleset change | local YAML validation plus documented live audit |
| CLI inventory | [`pyproject.toml [project.scripts]`](../pyproject.toml) | Console-script registration change | `check_cli_table_sync` |
| Coverage floor | [`pyproject.toml [tool.coverage.report]`](../pyproject.toml) | Coverage configuration change | `hephaestus-check-doc-config` |
