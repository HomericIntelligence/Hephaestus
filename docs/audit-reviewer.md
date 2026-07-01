# Audit Reviewer (`hephaestus-audit-prs`)

A coordinator-pattern PR auditor: one agent invocation reviews ALL open
PRs and posts a summary-only review per PR. Code at
`hephaestus/automation/audit_reviewer.py`.

## Architecture

`AuditReviewer.run()` →
`fetch_open_prs()` (or `_fetch_prs_by_number` when `--pr-numbers` is set) →
`run_audit_coordinator()` → `_parse_coordinator_results()` →
`write_audit_report()` → `print_audit_summary()` → per-PR `gh_pr_review_post`.

Unlike `hephaestus-review-prs` (one Claude worker per PR), this script issues a
SINGLE agent call to bound cost on large open-PR backlogs.

## CLI Usage

```
hephaestus-audit-prs [--pr-numbers N ...] [--codex] [--dry-run] [--json] [-v]
```

- `--pr-numbers`: explicit PR list (default: all open PRs, no cap).
- `--codex`: use Codex instead of Claude.
- `--dry-run`: skip both the agent call and the GitHub posting step.
- `--json`: emit `{"status", "exit_code", "audits": <int>}` envelope on exit.
- `-v, --verbose`: enable DEBUG-level logging.

## Programmatic API

```python
from hephaestus.automation import AuditReviewer
rc, audits = AuditReviewer(pr_numbers=[101, 102]).run()
```

## Report Format

Written to `build/.issue_implementer/audit-report-<UTC-timestamp>.json`:

```json
{
  "generated_at": "20260605T120000Z",
  "audits": [
    {"pr_number": 101, "verdict": "GO",
     "summary": "...", "findings": ["...", "..."]}
  ]
}
```

## Coordinator Prompt

Built in `_build_coordinator_prompt`: explicitly requires EXECUTE (not PLAN),
forbids backgrounding and early exit, restricts the agent to read-only
tools (`Read`, `Grep`, `Glob`).

## Error Handling

- Coordinator subprocess failure or timeout → `RuntimeError` caught by
  `AuditReviewer.run()`, exit code `1`.
- Non-empty agent output that yields zero parseable JSON blocks →
  `RuntimeError("Coordinator returned no parseable JSON block")`,
  exit code `1` (silent-empty guard).
- Per-PR `gh_pr_review_post` failure → WARN log, run continues.
- Empty PR list → exit `0` with no agent call.

## Timeout

- The coordinator agent timeout defaults to
  `hephaestus.automation.agent_config.DEFAULT_AGENT_TIMEOUT`.
- All standard `gh` CLI auth vars apply for `fetch_open_prs`.

## Examples

```bash
# Audit every open PR
hephaestus-audit-prs

# Audit specific PRs only
hephaestus-audit-prs --pr-numbers 994 995 996

# Dry-run shows the JSON envelope without touching GitHub
hephaestus-audit-prs --dry-run --json
```
