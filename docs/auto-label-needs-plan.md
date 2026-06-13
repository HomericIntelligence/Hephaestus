# Auto-tagging new issues with `state:needs-plan`

The automation pipeline (#704) uses three `state:*` labels as the single
source of truth for an issue's plan-review status:

| Label | Meaning |
|-------|---------|
| `state:needs-plan` | Issue is new; planner should run on the next loop. |
| `state:plan-no-go` | Reviewer's latest verdict was NOGO; re-plan next loop. |
| `state:plan-go` | Plan approved; implementer may proceed. |

ProjectHephaestus self-tags its own newly-opened issues via
[`.github/workflows/auto-label-needs-plan.yml`](../.github/workflows/auto-label-needs-plan.yml).
That workflow is also a **reusable workflow** (`workflow_call`-callable), so
every other HomericIntelligence repo gets the same behaviour by adding a
**single 8-line stub file** at `.github/workflows/needs-plan.yml`:

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
    uses: HomericIntelligence/ProjectHephaestus/.github/workflows/auto-label-needs-plan.yml@main
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
- **Audit-section** is intentionally **not** a form field. ProjectHephaestus has
  no per-audit-section label vocabulary (only `audit-finding`), so maintainers
  tag audit section during triage rather than via the form — avoiding an inert
  field with no consumer.

State is **not** a form field: `state:needs-plan` is applied automatically on
open/reopen (above). Keeping state automation-driven — not a free-text form
field — is deliberate; a free-text state field drifts off-format and
mis-routes issues.

## Rollout

Run [`hephaestus-ensure-state-labels --org HomericIntelligence`](../hephaestus/automation/ensure_state_labels.py)
first so every repo has the three `state:*` labels defined. Then copy the
stub above into each repo's `.github/workflows/needs-plan.yml` and merge — a
short PR per repo is the simplest path. New issues from then on get
`state:needs-plan` automatically; the next automation-loop iteration picks
them up and the reviewer transitions them to `state:plan-go` or
`state:plan-no-go`.

## Security

The reusable workflow only consumes **server-controlled integers**
(`github.event.issue.number` and `github.repository`) — no user-controlled
text (title/body/labels) is touched, so command-injection vectors via the
issue payload are not present. Permissions are scoped to
`contents: read` + `issues: write`.
