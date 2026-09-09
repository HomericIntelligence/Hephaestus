# ADR-0048: Automation rebase triggers

- Status: Accepted
- Date: 2026-09-08
- Scope: Automation loop

## Context

Automatic PR adoption, a branch behind main, a merged scope dependency, and
publication recovery could cause a rebase. These triggers could rewrite a PR
before it passed review or without a merge conflict.

## Decision

The loop permits a rebase only in these conditions:

1. The first implementation of an issue starts. Fetch `origin/main` and prepare
   the writer before the implementation agent starts. Keep a durable host
   record so a restart does not repeat this preparation. Require host creation
   evidence for a fresh branch. An old writer without that evidence needs an
   explicit manual rebase before it can continue. Advance a new direct issue's
   empty remote reservation to the prepared base under its exact-head lease.
2. A PR has GO for its exact current head and a merge conflict. Start the
   rebase agent. Recheck the live state before the host changes the branch.
3. The operator supplies `--rebase` with selected issues or PRs. Apply the
   request once per selected item in the invocation. Then continue normal
   work. Abort a failed mechanical rebase before the agent-assisted retry.

Each replay uses an exact commit from `origin/main`. The host owns Git,
signing, and publication. Conflict agents edit only the allowed files.
Publication keeps its exact remote-head lease. A changed published head
requires a new review unless the host proves the same change under
[ADR-0038](0038-reviewed-head-ci-merge-gate.md). That exception keeps the
initial review identity and requires fresh CI/CD for the resulting head.

The loop's `--update-plan` option updates selected issue plans once per
invocation. Plan updates use the latest fetched `origin/main` in a detached
planning checkout. They do not rebase the implementation branch. Retries stay on the
captured commit for that planning epoch.

A branch that is only behind main waits. A missing merged scope dependency
requires manual preparation. A changed remote head during publication stops
the item without an automatic rebase. Server merge protection still applies.

This decision does not change fleet-sync or tidy. It supersedes automatic
rebase behavior described in earlier automation decisions. It does not change
the detached planning-source boundary in ADR-0040.

## Alternatives considered

Prompt rules alone cannot control host Git jobs. A general disable switch
cannot preserve the three permitted triggers. The host therefore checks each
rebase request before it changes branch history.

## Consequences

More items can need a manual rebase. The operator must resolve a dirty initial
worktree before implementation starts. A conflict-free PR can wait when server
policy requires an updated base. No automatic fallback bypasses that policy.

Tests must cover each allowed trigger, forbidden triggers, restart, changed
head or base, manual selection, conflict abort, agent start, and publication.
Tests must prove that forbidden paths leave branch history unchanged.
