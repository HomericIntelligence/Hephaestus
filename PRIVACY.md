# Privacy, Data Retention, and Deletion Policy

## Scope

Hephaestus is a locally run library and CLI toolset, not a hosted service.
All data it processes stays on the operator's machine or in the operator's
own GitHub account; the project itself operates no servers, databases, or
telemetry endpoints. This policy documents what data the tooling touches,
how long it persists, how to delete it, and how to raise a data-subject
request.

## Data Inventory

| Data category | Where it lives | Written by |
|---------------|----------------|------------|
| GitHub content (issue/PR bodies, review comments, diffs) | GitHub; transient copies under `build/.worktrees/` and `build/.issue_implementer/` | `hephaestus.automation`, `hephaestus.github` |
| Developer credentials (GitHub / Anthropic tokens) | Environment variables only — never persisted by this library | operator shell / CI secrets |
| Automation queue state and logs | `build/.issue_implementer/` (git-ignored) | `hephaestus.automation` |
| Crash bundles (raw core dumps; may embed process memory) | `/tmp/crash-bundle/cores` by default | `hephaestus.forensics` |
| Observability metrics | In-process Prometheus registry; local scrape endpoint only | `hephaestus.observability` |

## Retention

- **Credentials**: held in process memory for the lifetime of a command;
  never written to disk, logs, or state files. Log formatters must not
  interpolate token values.
- **Automation state, worktrees, logs** (`build/`): ephemeral working data.
  Safe to delete at any time; retained only until the operator clears the
  `build/` directory. There is no fixed retention obligation because the data
  is a cache of content whose system of record is GitHub.
- **Crash bundles**: contain raw process memory and MUST be treated as
  secret-bearing. Operators should review and delete bundles within 30 days
  of capture; CI environments are ephemeral and destroy them at teardown.
- **Observability metrics**: live only in the in-process Prometheus registry
  and disappear when the process exits; they are never persisted to disk by
  this library.
- **GitHub content**: retention on GitHub is governed by GitHub's own
  [Privacy Statement](https://docs.github.com/site-policy/privacy-policies/github-privacy-statement)
  and the repository owner's settings, not by this project.

## Deletion Procedures

- **Local caches and state**: `rm -rf build/` from the repository root (or
  `git worktree prune` after removing `build/.worktrees/`).
- **Crash bundles**: `rm -rf /tmp/crash-bundle/cores` (or the operator's
  `--target-dir` override).
- **Credentials**: rotate or revoke at the issuer (GitHub token settings,
  Anthropic console); there is nothing to purge locally.
- **Content already pushed to GitHub** (comments, PRs posted by automation):
  delete via GitHub's UI or API; this is covered by GitHub's own
  data-deletion practices.

## Data Subject Requests (GDPR)

The project does not itself act as a data controller for third-party
personal data: it processes repository content under the operator's own
GitHub authorization. For questions, access, or erasure requests concerning
personal data in this repository's issues, PRs, or history, email
**<research@villmow.us>**. Requests are acknowledged within 7 days.

## Sub-processors

None. The library calls the GitHub API and the operator-configured model
provider directly with the operator's own credentials; no data is routed
through project-operated infrastructure.
