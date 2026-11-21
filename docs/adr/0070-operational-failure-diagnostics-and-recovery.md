# ADR-0070: Operational failures retain their causes and do not invent review verdicts

- Status: Proposed
- Date: 2026-11-19
- Tracks: 2026-11-19 automation operations review

## Context

Automation logs reduced different failures to stable categories such as
`agent job failed`, `os-error`, and `comment_journal_read_error`. The category
is useful for durable routing, but it did not tell an operator the cause or the
recovery action. Some absolute monotonic deadline checks also raised a
subprocess timeout with a fabricated duration of zero seconds.

A transient plan-comment read failure could exhaust the planning retry budget
and apply `state:plan-no-go`. A transport failure is not a review verdict. The
same rule applies to coordinator cancellation: an unread issue remains eligible
for a later run. Its state is not known to be closed.

GitHub auto-merge mutations omitted `--repo`. The GitHub CLI then inferred the
repository from the process working directory, which is not necessarily a Git
checkout. Codex also encoded bounded reconnect notices as error events. These
notices could hide a later successful turn result.

## Decision

1. Keep stable failure categories for durable routing. Add a bounded, redacted
   cause and the selected recovery action to the operator-facing log. Issue
   read errors use structured metadata, such as an exit status or `errno`, and
   do not copy command lines, paths, response bodies, or subprocess output.
2. Report an exhausted absolute monotonic deadline as a deadline expiry. Keep
   `subprocess.TimeoutExpired` compatibility for callers. Do not report a
   subprocess duration that was not measured.
3. Treat coordinator cancellation as an interrupted read. Retain the issue for
   the next run and name the cancellation in the warning.
4. Retry a failed plan discovery read within the planning budget. If the read
   remains unavailable, hold planning without changing the plan-review label.
   Missing transport evidence must not create a GO or NOGO verdict.
5. Treat a bounded Codex reconnect notice as nonfatal while the provider owns
   its reconnect loop. The terminal event or process result determines failure.
6. Pass the selected repository to every native auto-merge mutation. The
   mutation must not depend on the coordinator working directory.
7. Worktree and no-commit retries name the observed condition and state that
   the applicable retry loop is recovering the work.

## Alternatives considered

- Keep category-only messages. This preserves terse logs but leaves operators
  unable to distinguish transport, provider, configuration, and agent causes.
- Copy raw subprocess output into every error. This adds detail but can expose
  credentials and untrusted response content. Structured metadata and bounded
  redaction provide the necessary cause without that exposure.
- Convert persistent transport reads into NOGO. This invents a review decision
  that no reviewer made and can stop correct work because infrastructure failed.

## Consequences

- Existing failure-kind routing remains compatible. Log consumers can use the
  stable category and operators can read the cause and recovery action.
- Absolute deadline failures remain compatible with existing timeout
  classification, but their messages no longer claim a zero-second subprocess.
- A persistent plan-discovery outage holds work instead of applying NOGO. A
  later run can resume after GitHub recovers.
- Auto-merge mutations work from a repo-less coordinator process.
- Provider notices do not discard a successful Codex turn. A genuine terminal
  provider or process failure still fails normally.
