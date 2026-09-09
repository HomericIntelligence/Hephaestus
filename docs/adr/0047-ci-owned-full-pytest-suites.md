# ADR-0047: CI owns full pytest suite execution

- Status: Partly superseded by [ADR-0049](0049-fast-pr-nightly-tests.md)
- Date: 2026-09-08
- Tracks: #3143

## Context

Full pytest suites can make local commit hooks slow and can duplicate the
required continuous integration and continuous delivery (CI/CD) test jobs.
However, a pull request that adds or changes a test needs evidence that the test
is valid. A successful command that collects no applicable tests is not valid
evidence.

## Decision

ADR-0049 replaces the pre-commit exclusion and the full-suite schedule below.
The focused-test evidence requirement remains in effect.

Do not run pytest from pre-commit. Required CI/CD runs the full unit and
integration test suites and applies the coverage gate.

Before a contributor or agent creates a pull request, it must run each new or
changed test with a focused pytest command. The pytest result must show that the
command collected the applicable tests and that they passed. A contributor can
run a full suite locally for diagnosis, but this is not a prerequisite for pull
request creation.

Keep fast lint, format, type, security, structure, and documentation-policy
checks in pre-commit. A validator that inspects test layout is not a pytest
suite and can remain in pre-commit.

The automation loop can run its host-owned pre-PR source-validation profile.
That profile is an automation safety gate, not a developer Git hook. It gives
early feedback only. The exact-head required CI/CD results remain the merge
contract.

## Consequences

Local commits do not wait for a full pytest run. Required CI/CD gives the
authoritative full-suite and coverage results. New or changed tests have direct
pre-PR evidence, including nonempty collection. Existing tests that the change
does not modify run in CI/CD.

The developer and reviewer must identify the applicable new or changed tests.
CI/CD can find failures outside that focused set after PR creation.

## Alternatives considered

Running all pytest suites in pre-commit gives early feedback, but it increases
commit time and duplicates CI/CD. Running no tests before PR creation can admit
new tests with collection errors or empty selection. Both alternatives were
rejected.
