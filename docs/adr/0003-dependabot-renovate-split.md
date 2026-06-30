# ADR-0003: Dependabot owns pip+actions; Renovate owns pixi/conda

- Status: Accepted
- Date: 2026-06-30
- Tracks: #1452

## Context

ProjectHephaestus has two distinct dependency ecosystems. The Python wheel
dependencies and the GitHub Actions pins are expressible in formats Dependabot
understands (`pip`, `github-actions`). The pixi/conda-forge dependencies live
in `pixi.toml`, which Dependabot cannot parse at all.

Renovate can manage the pixi ecosystem, but it also ships a default
GitHub Actions manager. With both bots updating GitHub Actions, the repository
received duplicate update PRs (issue #687).

## Decision

Split ownership by ecosystem with no overlap:

1. **Dependabot** (`.github/dependabot.yml`) owns `pip`
   (`.github/dependabot.yml:3`) and `github-actions`
   (`.github/dependabot.yml:18`).
2. **Renovate** (`renovate.json`) owns **only** the pixi/conda-forge ecosystem
   (`matchManagers: ["pixi"]`) and explicitly disables its GitHub Actions
   manager (`"github-actions": {"enabled": false}`, `renovate.json:14`). An
   inline comment records the #687 duplicate-PR rationale so the disable is not
   accidentally reverted.

## Alternatives considered

- **Renovate-only (drop Dependabot).** Rejected: weakens the pip
  security-update story that Dependabot provides out of the box.
- **Dependabot-only (drop Renovate).** Rejected: Dependabot cannot parse
  `pixi.toml`, leaving the conda-forge ecosystem unmanaged.
- **Both bots manage GitHub Actions.** Rejected: this is exactly what produced
  the duplicate PRs in #687.

## Consequences

- `renovate.json` must keep `"github-actions": {"enabled": false}`; re-enabling
  it re-opens the #687 duplicate-PR risk.
- A new ecosystem is assigned to exactly one bot — Dependabot if it can parse
  the format, Renovate otherwise.
- Conda/pixi updates are grouped into a single PR
  (`groupName: "conda-pixi-dependencies"`) mirroring Dependabot's
  `python-dependencies` group.
