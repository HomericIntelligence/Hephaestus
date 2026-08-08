# Plan reviewer: skip semantic changed — APPROVED-only short-circuit

**Affected component:** `hephaestus.automation.plan_reviewer`
**Issues:** #550 (epic), #552, #553, #560, #561, #563, #565
**Ships with:** PR landing the bundle-A fixes against epic #550

## What changed

Before this release the plan reviewer treated *any* prior `## 🔍 Plan Review`
comment as a terminal signal and short-circuited the issue forever — even if
the prior review had said `**Verdict: REVISE**` or `**Verdict: BLOCK**`. Once
the planner amended the plan, the reviewer never re-evaluated it.

After this release the reviewer short-circuits **only** when the latest plan
review carries the `**Verdict: APPROVED**` marker on its own line. REVISE,
BLOCK, or any pre-marker review body re-triggers a fresh review on the next
loop run.

Two correctness fixes ride along:

1. **Last-line-wins verdict parsing.** The gate now extracts every line
   matching `^**Verdict: (APPROVED|REVISE|BLOCK)**$` from the review body
   (regex, multiline) and uses the LAST match — matching the contract the
   prompt gives Claude (the "last matching line" rule documented in
   `hephaestus/automation/prompts/__init__.py`; the former single-file
   `prompts.py` was split into the `hephaestus/automation/prompts/`
   package). The previous substring `in`
   check could mis-fire True on a body that quoted the marker in prose or
   discussed APPROVED before settling on BLOCK.

2. **GraphQL comment fetch.** `_fetch_issue_comments` now queries the
   GitHub GraphQL API directly with
   `comments(last: 100, orderBy: {field: UPDATED_AT, direction: DESC})`
   instead of `gh issue view --comments`, which silently capped at 100 in
   chronological order. Issues with 100+ comments could previously have
   their actual-latest review fall off the head of the list.

## Operational impact: one-time Claude call burst

Because the skip condition tightened from *"any prior review"* to
*"APPROVED-only"*, every existing open issue whose latest plan review is
non-APPROVED (REVISE / BLOCK / pre-marker) will be re-reviewed on the first
loop run after this lands. This is intentional — those issues should have
been re-reviewed all along — but it means a one-time spike in Claude calls
across the HomericIntelligence fleet.

### Estimating the spike per repo

Run this in each repo to count issues that will re-trigger:

```bash
gh issue list --state open --limit 1000 --json number,comments --jq '
  [.[]
   | select(any(.comments[]; .body | startswith("## 🔍 Plan Review")))
   | select(
       all(
         .comments[]
         | select(.body | startswith("## 🔍 Plan Review"))
         ; (.body | contains("**Verdict: APPROVED**")) | not
       )
     )
   | .number
  ]
  | length
'
```

Multiply by the per-review Claude token cost to get a one-time budget delta.
Subsequent loop runs return to baseline — only the *first* loop after
deploy pays the back-fill cost.

## Mitigations

- **Watch fleet quota.** The reviewer respects per-agent rate-limit
  back-off (`scan_quota_reset` + `wait_until`); a quota hit during the spike
  delays but does not corrupt the review.
- **Per-repo rollout.** If the bulk count above is uncomfortable for any
  single repo, schedule that repo's first post-deploy loop during a
  low-traffic window.

## No action required from operators if

- The repo has zero open issues with prior plan reviews, or
- All prior plan reviews already carry `**Verdict: APPROVED**`.

In either case the short-circuit gate behaves identically to before.
