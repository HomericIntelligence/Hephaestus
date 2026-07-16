# ADR-0001: hephaestus.automation is an opt-in product layer

- Status: Accepted
- Date: 2026-06-05
- Tracks: #711 (parent #708)

## Context

`hephaestus/automation/` is 26,125 LoC of the 48,498-LoC source tree (53.9%, as of 2026-06-13). It
implements the full Claude/Codex automation pipeline: Planner, Implementer,
CIDriver, PRReviewer, PlanReviewer, AddressReviewer, AuditReviewer,
loop_runner, curses TUI, GitHub adapter, prompt assembly. The rest of
`hephaestus/` is a shared utility library consumed by every
HomericIntelligence project.

A consumer that wants `slugify` or `run_subprocess` from `hephaestus.utils`
should not pay for the automation product's weight. Issue #711 identified
this as the single biggest architectural smell.

## Decision

Adopt a **dual-layer package** with four guarantees:

1. **Library layer** — `hephaestus.{utils, io, config, logging, cli, system,
   github, validation, resilience, markdown, ci, benchmarks, datasets,
   discovery, forensics, nats, version, agents, scripts_lib}`. Loaded via PEP 562 lazy
   imports (`hephaestus/__init__.py`). `import hephaestus` MUST NOT
   transitively import `curses`, `fcntl`, `pydantic`, or any
   `hephaestus.automation.*` module. Verified empirically and locked in
   by `tests/unit/validation/test_import_surface.py`.

2. **Product layer** — `hephaestus.automation`. Opt-in via the
   `HomericIntelligence-Hephaestus[automation]` extra. The extra declares
   `pydantic` (used by `hephaestus/automation/models.py`) and `tzdata` on
   Windows (used indirectly via `hephaestus.github.rate_limit`).

3. **Six console scripts** ship registered in `[project.scripts]` —
   `hephaestus-automation-loop`, `hephaestus-plan-issues`,
   `hephaestus-implement-issues`, `hephaestus-review-prs`,
   `hephaestus-agent-stage`, `hephaestus-ensure-state-labels`. They require
   the `[automation]` extra to be honest about their dependency surface.
   As of issue #1458, `pydantic` is no longer a base dependency — it ships
   only in the `[automation]` extra, so a base install does not provide it
   and these scripts genuinely require `[automation]`.

4. **Boundary contract** — `hephaestus.automation` may import from any
   library subpackage; library subpackages MUST NOT import from
   `hephaestus.automation`. Enforced by
   `tests/unit/validation/test_automation_boundary.py`.

## Alternatives considered

- **Carve out to a `homeric-automation` distribution.** Rejected: requires
  new repo, dual uv.lock, CI sweep of every workflow invoking the seven
  automation console scripts. The `ci-library-migration-audit` team-knowledge
  skill documents how prone that path is to silent CI breakage.
- **Status quo (just document).** Rejected: leaves no installable boundary;
  future edits could wire automation into the lazy-import map and silently
  bloat the base install.
- **Empty `[automation] = []` extra.** Rejected on POLA grounds: an extra
  that installs nothing different from the base is surprising. Populating
  it with `pydantic` makes it honest and forward-compatible.

## Consequences

- `pyproject.toml` gains a populated
  `[project.optional-dependencies] automation = [...]`.
- README and CLAUDE.md gain a "Library vs product layer" section pointing
  here.
- `tests/unit/validation/test_import_surface.py` fails CI if `import hephaestus` ever
  pulls `curses`, `pydantic`, or any `hephaestus.automation.*` module into
  `sys.modules`.
- `tests/unit/validation/test_automation_boundary.py` fails CI if any library
  subpackage gains a `from hephaestus.automation` import.
- `hephaestus.nats` was migrated off pydantic to stdlib dataclasses
  (issue #1458; `load_nats_config` filters unknown keys to preserve the
  prior tolerant-YAML behavior), so pydantic was dropped from the base
  dependency list and `[automation]` is now load-bearing: a base install
  no longer pulls pydantic.
