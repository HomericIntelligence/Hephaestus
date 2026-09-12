# Recover an implementation-blocked issue

## When to use

Use this runbook when an issue has `state:implementation-blocked`. The
label means that an implementation run completed without a commit and needs a
human decision. It does not mean that the issue should be skipped.

The pipeline keeps the existing `state:plan-go` label. It does not start
another implementation run or request a new plan while the implementation
block remains.

The label is the routing authority. The host confirms it before the host writes
the feedback comment. If the comment write or its readback fails, the label
remains and prevents an automatic retry. Repair the comment audit before you
select a recovery action.

## Check the block

Read the current issue labels and the feedback comment:

    gh issue view <N> --json labels,comments

Review the implementation-agent summary. Treat it as bounded context. Do not
use raw logs, secrets, or unbounded terminal output as recovery input.

## Choose one action

Make one deliberate human state transition. Remove the implementation latch
only after you choose the action.

- Continue implementation under the approved plan. Remove
  `state:implementation-blocked`, keep `state:plan-go`, and run
  `hephaestus-implement-issues --issues <N>`.
- Clarify the requirements. Update the issue, then remove the latch and run
  the implementation command after the requirements are clear.
- Mark the issue as already satisfied. Remove the implementation latch and
  apply `state:skip` when the issue is complete.
- Explicitly skip the issue. Apply `state:skip` and remove the
  implementation latch.
- Request a new plan revision. Do this only when the approved plan is
  invalid. Remove the implementation latch, then run
  `hephaestus-plan-issues --issues <N> --force` as an explicit human
  action.

For the first two actions, remove the latch with:

    gh issue edit <N> --remove-label state:implementation-blocked

To record an explicit skip in one transition, use:

    gh issue edit <N> --add-label state:skip --remove-label state:implementation-blocked

After the transition, confirm the labels again. The next pipeline pass must
show the selected state. A comment alone does not resume automation.

## Safety rules

- Keep `state:plan-go` when the approved plan remains valid.
- Do not remove or replace `state:implementation-blocked` without a
  human decision.
- Use `--force` only for the explicit plan-revision action. The
  implementation stage never invokes it automatically for a no-commit run.
- If a label or comment read is unclear, stop and repair the GitHub state
  before you start another agent. Do not remove the latch only to retry a
  failed audit write.
