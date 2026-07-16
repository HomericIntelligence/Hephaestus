# ADR-0008: uv is the sole development environment manager

- Status: Accepted
- Date: 2026-07-16
- Tracks: no tracking issue

## Context

Hephaestus previously maintained both a Pixi environment and a uv environment.
Two manifests, lockfiles, task syntaxes, and CI setup paths allowed the
environments to diverge. In particular, invoking a console script from an
ambient environment could run a different dependency set than the checked-in
project environment.

ADR-0003 records the historical decision that divided Pixi/conda and pip/GitHub
Actions updates between Renovate and Dependabot. Its Accepted record remains
unchanged for auditability, but its Pixi premise no longer applies once Pixi is
removed.

## Decision

1. uv is the sole development-environment and dependency-lock tool. The
   checked-in source of environment state is `pyproject.toml` and `uv.lock`.
2. Developer and CI commands run through uv. `uv sync` creates the editable
   project environment; checks that need every optional dependency use
   `uv sync --all-groups --all-extras --locked`.
3. Pixi manifests, Pixi task syntax, and Renovate configuration for the retired
   Pixi/conda ecosystem are removed. Dependabot continues to manage the Python
   and GitHub Actions ecosystems declared in `.github/dependabot.yml`.

## Alternatives considered

- **Keep both Pixi and uv.** Rejected: duplicate lockfiles and task runners
  caused environment selection errors and increased the maintenance surface.
- **Use Pixi as the sole tool.** Rejected: the project has standardized on uv
  commands and a uv lockfile for development and CI.
- **Use raw pip and virtualenv.** Rejected: this would discard the locked,
  reproducible workflow provided by uv.

## Consequences

- Documentation, automation, hooks, and CI must use actual uv commands; no
  Pixi task or manifest may be introduced for active development.
- `uv lock --check` verifies lockfile currency, and dependency changes include
  the corresponding `uv.lock` update.
- ADR-0003 is superseded for the active dependency-manager topology but remains
  an immutable historical record.
