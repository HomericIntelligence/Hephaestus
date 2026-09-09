# ADR-0049: Fast PR tests and nightly full validation

- Status: Accepted
- Date: 2026-09-09
- Tracks: #3141

## Context

Full test suites repeat expensive checks during pull-request validation.
Issue #3141 moves those checks to nightly CI and retains one deterministic fast
selection for local commits and pull requests. This changes the schedule and
pre-commit policy in ADR-0047.

## Decision

Use `scripts/run_fast_tests.sh` for pre-commit, `just test`, and required PR lint.
Profile this selection against the full baseline. Approximately ten percent is
a target, not a hard timing gate. Jobs shorter than one minute are exempt from
that runtime evaluation. Pi conformance runs nightly by explicit selection.

Nightly CI runs full normal unit coverage and the integration complement of the
fast selection. Separate nightly lanes run installed CLI, shell, package,
artifact, and Pi conformance checks. Keep the configured coverage floor at 85%.
The offline Codex artifact lane retains network isolation and both read-only
fixture aliases. Scheduled failures use the existing tracking-issue workflow.

Before PR creation, run every new or changed test and confirm nonempty
collection and success. A focused command outside the default fast selection
must clear `addopts` with `--override-ini="addopts="`.

Required PR contexts must match the revised job graph. This decision does not
change reviewed-head proof, thread resolution, signatures, or the policy-selected
server merge route. CI results do not independently authorize the automation loop.

## Consequences

PR validation gives fast feedback. Full unit coverage intentionally repeats fast
unit tests so coverage measures the complete package. Full validation can find a
failure after merge; the nightly failure record requires follow-up work.

The fast selection and nightly complement must cover the intended normal-test
contract. Special artifact and contract lanes keep their explicit controls.

## Alternatives considered

Running all suites on each PR retains earlier feedback but repeats the costly
lanes that issue #3141 moves to nightly CI. Removing all PR tests loses useful
fast feedback. The shared fast selection keeps that feedback while nightly CI
retains the full validation contract and coverage floor.
