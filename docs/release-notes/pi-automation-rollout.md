# Managed Pi automation rollout

**Affected component:** Pi provider integration
**Issues:** #2513, #2520
**Ships with:** the next signed `vX.Y.Z` release

## What changed

Pi now uses the catalog-pinned CLI and the managed package bootstrap path. CI
and operator guidance install the package set through
`hephaestus-install-pi-plugins` after the CLI is present.

## Operational impact

- Install the pinned Pi CLI, then run
  `hephaestus-install-pi-plugins --global --yes --no-approve`.
- Use `--disable-pi-automation` when a run must omit Pi.
- Keep private Pi aliases in the local configuration and follow
  `docs/pi-private-provider.md` for the sanitized setup.
