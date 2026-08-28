# Hephaestus Compatibility Distribution

This directory is a packaged compatibility copy of the Hephaestus skill set.
It is restored from commit `5a70ad28df2249e2467d8b8b7543d42a1c0bd520`.

## Ownership

This tree is package content only. It is not the runtime skill source of
truth. Runtime skill enablement stays in `.claude/settings.json` and the
Athena marketplace.

## Contents

- `.codex-plugin/plugin.json`
- `assets/icon.svg`
- `skills/`
- `skills/THIRD_PARTY_LICENSES.md`
- `README.md`
- `LICENSE`
- `.codexignore`

## License

The package uses the BSD 3-Clause license. See [LICENSE](LICENSE).

## Validation

- Skill manifests are parsed with the shared frontmatter helper.
- The plugin manifest is checked with a strict JSON schema.
- Package assembly uses the package-level `.codexignore` rules.

## Rollback

To remove this compatibility distribution, delete `plugins/hephaestus/`, remove
the topology exception that allows it, and drop the ADR that records the
compatibility decision.
