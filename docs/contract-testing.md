# Contract Testing (opt-in external-integration lane)

Hephaestus' automation layer depends on two external CLIs — GitHub's `gh` and
the `claude` agent CLI. The default test suite exercises both through mocks and
fakes only (e.g. `tests/integration/test_gh_trace_id_propagation.py` injects a
*fake* `gh` on `PATH`); no default test reads a real token or spends model
tokens. That keeps CI fast and hermetic, but it means a drift in the real
external contract — a renamed JSON field, a changed error shape, a broken
session-resume seam — would go unnoticed until it broke automation in
production.

The **contract lane** closes that gap. It is an opt-in, sandboxed set of tests
under `tests/integration/contract/` that runs read-only authenticated calls
through the exact production chokepoints (`gh_call` in
`hephaestus/github/client.py`, `invoke_claude_with_session` in
`hephaestus/automation/claude_invoke.py`) to verify the contracts the
automation layer relies on.

## Opt-in gating

The lane is **off by default**. A collection hook in `tests/conftest.py` marks
every `contract`-marked test skipped unless `HEPHAESTUS_CONTRACT_TESTS=1` is
set, so `just test`, the required CI checks, and `test.yml` are all unaffected —
the lane collects (proving the tests import and parametrize) and skips.

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `HEPHAESTUS_CONTRACT_TESTS` | Master gate. Set to `1` to run the lane at all; otherwise every contract test skips. | unset (lane skips) |
| `HEPHAESTUS_CONTRACT_AGENT` | Second gate for the **agent** lane only, which spends real model tokens. Set to `1` to run it. | unset (agent lane skips) |
| `HEPHAESTUS_CONTRACT_REPO` | `owner/name` slug the GitHub lane targets. If unset, it is resolved once from the repo root with an explicit `cwd` pin. | resolved from repo root |
| `HEPHAESTUS_CONTRACT_MODEL` | Model the agent lane uses. Keep it cheap. | `haiku` |

Individual tests still skip cleanly (not fail) when their specific prerequisite
is absent: the GitHub lane skips if `gh` is not installed or `gh auth status`
reports unauthenticated; the agent lane skips if `claude` is not
installed/authenticated.

## Sandbox guarantees

- **Read-only GitHub calls.** Every `gh` call is a read (`api rate_limit`,
  `repo view`, `issue list --json`); the lane cannot mutate the target repo.
- **Chokepoints only.** Calls route through `gh_call` and
  `invoke_claude_with_session`, never a bare `subprocess.run(["gh", ...])` —
  so the lane exercises the exact production paths, circuit breaker included.
- **Explicit repo pinning.** The target repo comes from
  `HEPHAESTUS_CONTRACT_REPO` or is resolved with an explicit `cwd` pin — never
  ambient CWD — so the lane cannot misroute to whatever repository the runner
  happens to be sitting in.
- **Isolated agent cwd + cheap model.** The agent lane runs in a pytest
  `tmp_path` with a fixed trivial prompt on `HEPHAESTUS_CONTRACT_MODEL`
  (default `haiku`).

> **Token-cost warning:** the agent lane spends real model tokens on every run.
> It is double-gated (`HEPHAESTUS_CONTRACT_TESTS=1` **and**
> `HEPHAESTUS_CONTRACT_AGENT=1`) for exactly this reason.

## Running the lane

```bash
# GitHub lane only (requires an authenticated gh CLI):
just test-contract

# Or directly, equivalent to the recipe:
HEPHAESTUS_CONTRACT_TESTS=1 \
  uv run pytest tests/integration/contract --override-ini="addopts=" -v --strict-markers

# Add the agent lane (requires claude auth; spends tokens):
HEPHAESTUS_CONTRACT_TESTS=1 HEPHAESTUS_CONTRACT_AGENT=1 \
  uv run pytest tests/integration/contract --override-ini="addopts=" -v --strict-markers
```

## CI

`.github/workflows/contract.yml` runs the GitHub lane on **manual
`workflow_dispatch` only** — never on `pull_request`/`push` — and is **never a
required check**. It provisions `GH_TOKEN` from `github.token` and targets
`github.repository`. The agent lane self-skips there (no `claude` auth,
`HEPHAESTUS_CONTRACT_AGENT` unset).
