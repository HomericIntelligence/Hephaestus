# Third-Party Services: Responsibilities and SLAs

This document inventories every third-party service the Hephaestus build,
release, and automation pipeline depends on at runtime, records the
responsibility split between the project and each vendor, and states the
availability expectations that actually apply. It exists so that a dependency
on an external service can never be silently added without a documented owner,
failure mode, and mitigation (issue #2177).

## Purpose and scope

A **third-party service dependency** here means an external, vendor-hosted
SaaS or registry that the build, release, or automation pipeline calls over the
network — GitHub, the Python Package Index, and the agent providers, plus the
supply-chain services CI reaches during a run. Vendored third-party *code*
(pinned GitHub Actions, npm packages) is included because its availability at
install time is a runtime dependency, even though the code itself is pinned.

Self-hosted infrastructure and in-ecosystem components are **out of scope** —
see [Explicitly out of scope](#explicitly-out-of-scope).

Related decisions: [ADR-0003](adr/0003-dependabot-renovate-split.md)
(dependency-update bot ownership), [ADR-0004](adr/0004-single-aggregator-required-checks.md)
and [ADR-0007](adr/0007-dual-surface-required-checks.md) (GitHub required-check
gating), and [ADR-0008](adr/0008-uv-only-development-environment.md) (uv as the
sole environment manager).

## Availability expectations (no contractual SLAs)

Hephaestus consumes every service below on a free, best-effort, or usage-tier
basis. **None of them grant this project a contractual SLA.** The
"Availability commitment" column therefore records each vendor's *published*
status page or best-effort posture, not a guaranteed uptime figure. Claiming an
SLA the project does not hold would itself be an audit finding.

## Service inventory

| Service | Used for | Criticality | Our responsibility | Vendor responsibility | Availability commitment / status page |
|---|---|---|---|---|---|
| GitHub (repo, API, Actions, Pages, Dependabot) | Source hosting, `gh` API automation, CI runners, branch protection and merge gates, API-docs hosting on Pages, `uv`/actions dependency updates | Critical — all merges and CI stop | Pin actions by commit SHA, keep the required-checks aggregator accurate (ADR-0004 / ADR-0007), respect API rate limits (`hephaestus/github/rate_limit.py`), operate the stall runbooks | Service and API availability, Actions runner fleet, correctness of API responses | <https://www.githubstatus.com/> — no contractual SLA on the free/team tier |
| PyPI / TestPyPI | Release publishing via OIDC trusted publishing (`pypa/gh-action-pypi-publish` in `.github/workflows/release.yml`) | High — blocks releases only; development is unaffected | Maintain the trusted-publisher configuration, publish from signed `vX.Y.Z` tags, retry a failed publish by re-running the release workflow | Index availability, OIDC token exchange, package serving | <https://status.python.org/> — best-effort, no SLA |
| Anthropic (Claude CLI / API) | Primary agent provider for the automation pipeline (the loop shells out to the `claude` CLI) | High — automation halts; manual development is unaffected | Quota monitoring, model-cap fallback to `opus-4-8` (PR #1794), circuit breaker and retry (`hephaestus/resilience/`), the [claude-quota-exhausted](runbooks/claude-quota-exhausted.md) runbook | API availability, model behavior and quotas | <https://status.anthropic.com/> — usage-tier limits, no SLA |
| Pi private provider (optional) | Alternative OpenAI-compatible agent runtime (`docs/pi-private-provider.md`); the CLI is `@earendil-works/pi-coding-agent`, installed with `npm install -g --ignore-scripts` | Low — optional adapter, off by default | Operator-local configuration only; documentation uses placeholders (ADR-0011 / `docs/pi-private-provider.md`) | Provider-local availability | Operator-managed |
| npm registry | Installs the pinned Pi coding-agent CLI in CI (`.github/actions/setup-pi-cli`) | Low — only the Pi integration test path | Version pinning (`@0.80.2`), install with `--ignore-scripts` | Registry availability | <https://status.npmjs.org/> — best-effort |
| Astral (`astral-sh/setup-uv`) | Provisions the uv toolchain in CI (ADR-0008) | High for CI — uv is the sole environment manager | Pin the action by SHA; uv binaries are cached by `actions/cache` | Release-artifact availability | GitHub-hosted release artifacts — best-effort |
| Third-party GitHub Actions (`astral-sh/setup-uv`, `pypa/gh-action-pypi-publish`, `crazy-max/ghaction-import-gpg`, `softprops/action-gh-release`, `extractions/setup-just`) | uv provisioning, PyPI publishing, GPG import, release creation, `just` provisioning | Medium — release and CI ergonomics | SHA-pinning (done — see `release.yml` and `_required.yml`), `github-actions` Dependabot updates (ADR-0003) | Correctness of the pinned action code | Not a hosted service — vendored, pinned code |
| Dependabot (`.github/dependabot.yml`) | Automated `uv` and `github-actions` dependency-update PRs | Low — dependency hygiene | Keep the ecosystem list correct; review update PRs (ADR-0003) | Update-generation availability | GitHub-native — best-effort |
| Renovate (Mend app) | Historically owned the pixi/conda ecosystem (ADR-0003); **currently inactive** — no `renovate.json` is tracked since the uv-only migration (ADR-0008) removed the pixi ecosystem it managed | None — no active configuration | If pixi/conda ever returns, re-add a scoped `renovate.json` per ADR-0003 (GitHub-Actions manager disabled) | App availability, when configured | Best-effort, when configured |

## Responsibility policy

The split is the same across every row above:

- **Vendors own** the availability and correctness of their hosted service,
  registry, or runner fleet.
- **Hephaestus owns** everything within the project's control: pinning
  third-party code by commit SHA, configuring authentication and authorization
  (OIDC trusted publishing, GPG signing, GitHub branch protection), rate-limit
  hygiene, graceful degradation (circuit breaker, retries, provider fallback),
  and operator runbooks.

**Adding a new external service requires a new inventory row.** This is
enforced executably: `tests/unit/docs/test_third_party_services_doc.py` fails
CI if any remote GitHub Action owner referenced by a `uses:` line in
`.github/workflows/*.yml` is absent from this document, and if the required
services above are not all named.

## Degradation matrix

For each Critical or High service, the observable failure mode, the immediate
operator action, and the mechanism or runbook that covers it:

| Service | Observable failure mode | Immediate operator action | Mechanism / runbook |
|---|---|---|---|
| GitHub API | 4xx/5xx responses, rate-limit exhaustion, circuit breaker open | Pause the automation loop; wait for the rate-limit window or breaker reset | `hephaestus/github/rate_limit.py`, `hephaestus/resilience/circuit_breaker.py`, [ci-driver-stall](runbooks/ci-driver-stall.md) |
| GitHub Actions / merge gates | Required checks stuck queued or a merge gate never arms | Inspect the required-checks aggregator; re-run the workflow | ADR-0004 / ADR-0007, [ci-driver-stall](runbooks/ci-driver-stall.md) |
| PyPI / TestPyPI | Publish step fails or the index is unreachable | Re-run the release workflow from the signed tag once the index recovers | `.github/workflows/release.yml` |
| Anthropic (Claude) | HTTP 429 / quota exhaustion, model-cap 429 | Let the model-cap fallback and retry run; if persistent, pause the loop | Model fallback (PR #1794), `hephaestus/resilience/`, [claude-quota-exhausted](runbooks/claude-quota-exhausted.md) |
| Astral / npm registry | CI install step fails to fetch a toolchain or CLI | Re-run the job (artifacts are cached); if persistent, wait for registry recovery | `actions/cache`, action SHA pins |

## Explicitly out of scope

- **NATS** — self-hosted JetStream infrastructure, not a vendor service. See
  [NATS JetStream Configuration](nats.md).
- **Mnemosyne knowledge backend** — an in-ecosystem HomericIntelligence
  component, not a third-party vendor.
