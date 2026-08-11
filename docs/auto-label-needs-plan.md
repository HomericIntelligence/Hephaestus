# Auto-tagging new issues with `state:needs-plan`

The automation pipeline (#704) uses four planning `state:*` labels as the single
source of truth for an issue's plan-review status:

| Label | Meaning |
|-------|---------|
| `state:needs-plan` | Issue is new; planner should run on the next loop. |
| `state:plan-no-go` | Confirmed plan-review label; re-plan next loop. Review prose is audit context, not routing authority. |
| `state:plan-go` | Plan approved; implementer may proceed. |
| `state:plan-blocked` | Planning requires named external feedback or a dependency. |

Hephaestus self-tags its own newly-opened issues via
[`.github/workflows/auto-label-needs-plan.yml`](../.github/workflows/auto-label-needs-plan.yml).
That workflow is also a **reusable workflow** (`workflow_call`-callable), so
every other HomericIntelligence repo gets the same behaviour by adding a
**small caller stub** at `.github/workflows/needs-plan.yml`:

```yaml
name: needs-plan

on:
  issues:
    types: [opened, reopened]

permissions:
  contents: read
  issues: write

jobs:
  call:
    uses: HomericIntelligence/Hephaestus/.github/workflows/auto-label-needs-plan.yml@main
    with:
      issue_number: ${{ github.event.issue.number }}
```

## Issue intake (forms → labels)

The issue forms
([`feature_request.yml`](../.github/ISSUE_TEMPLATE/feature_request.yml),
[`bug_report.yml`](../.github/ISSUE_TEMPLATE/bug_report.yml)) feed the pipeline:

- **Severity** — a constrained dropdown (`critical` / `major` / `minor` /
  `nitpick`). On issue open/edit,
  [`auto-label-severity.yml`](../.github/workflows/auto-label-severity.yml) runs
  `hephaestus.github.severity_label`, which parses the rendered answer and
  **reconciles** the issue's `severity:*` label (removing any stale one). Only
  the server-controlled issue number and a fixed label string reach the API.
- **Parent Epic** — an optional `#NNN` reference for the Epic-and-children
  pattern (`epic` label, see [ROADMAP.md](ROADMAP.md)). **Reference only:**
  triage links it; it is not auto-consumed (free-text parsing into pipeline
  state is deliberately avoided).
- **Acceptance Criteria** — a required Markdown checklist of specific, testable
  outcomes that define when the issue is complete.
- **Verification Plan** — a required criterion-by-criterion mapping to runnable
  commands, automated tests, or manual checks and their expected evidence.
- **Audit-section** is intentionally **not** a form field. Hephaestus has
  no per-audit-section label vocabulary (only `audit-finding`), so maintainers
  tag audit section during triage rather than via the form — avoiding an inert
  field with no consumer.

State is **not** a form field: `state:needs-plan` is applied automatically on
open/reopen (above). Keeping state automation-driven — not a free-text form
field — is deliberate; a free-text state field drifts off-format and
mis-routes issues.

## Rollout and rollback

Run [`hephaestus-ensure-state-labels --org HomericIntelligence`](../hephaestus/automation/ensure_state_labels.py)
first so every repo has the planning `state:*` labels defined. The reusable
`issue_number` input is required, so an existing caller that does not pass it
fails before any label API request. Inventory callers, update each stub with
the `with` block above, and deploy the callers and reusable workflow as a
coordinated rollout. Then confirm that newly opened and reopened issues get
`state:needs-plan`; invalid inputs fail loudly before label mutation.

If the rollout must be contained, revert the reusable workflow and its caller
updates as one coordinated change. Reverting the workflow does not remove
already-applied labels. If cleanup is required, remove labels only from a
reviewed, explicit list of known issue numbers through the GitHub labels API;
do not perform a bulk deletion.

## Security

The reusable workflow receives the caller's server-controlled
`github.event.issue.number` as the required numeric `issue_number` input. It
resolves that input with the native event payload using
`${{ github.event.issue.number || inputs.issue_number }}` and validates the
resolved value as a positive decimal integer before making the API request.
The repository remains server-controlled through `github.repository`; no
user-controlled text (title/body/labels) is touched. Permissions are scoped
to `contents: read` + `issues: write`.
