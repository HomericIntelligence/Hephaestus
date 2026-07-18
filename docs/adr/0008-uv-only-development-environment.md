# ADR-0008: uv is the sole development environment manager

- Status: Accepted
- Date: 2026-07-16
- Tracks: #2235

## Context

Hephaestus previously maintained multiple development environments. Multiple
manifests, lockfiles, task syntaxes, and setup paths allowed those environments
to diverge. Invoking a console script from an ambient environment could run a
different dependency set than the checked-in project environment.

## Decision

1. uv is the sole development-environment and dependency-lock tool. The
   checked-in source of environment state is `pyproject.toml` and `uv.lock`.
2. Developer and CI commands run through uv. `uv sync` creates the editable
   project environment; checks that need every optional dependency use
   `uv sync --all-groups --all-extras --locked`.
3. Alternate environment manifests, task syntax, and dependency automation are
   removed. Dependabot continues to manage the Python and GitHub Actions
   ecosystems declared in `.github/dependabot.yml`.

## Alternatives considered

- **Keep multiple environment managers.** Rejected: duplicate lockfiles and task runners
  caused environment selection errors and increased the maintenance surface.
- **Use raw pip and virtualenv.** Rejected: this would discard the locked,
  reproducible workflow provided by uv.

## Consequences

- Documentation, automation, hooks, and CI must use actual uv commands; no
  alternate task or manifest may be introduced for active development.
- `uv lock --check` verifies lockfile currency, and dependency changes include
  the corresponding `uv.lock` update.
- ADR-0003 is retained only to preserve the ADR number sequence.
