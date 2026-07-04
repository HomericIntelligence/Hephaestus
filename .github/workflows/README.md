# GitHub Workflows

## Workflow Summary

| Workflow | Purpose | Triggers |
| --- | --- | --- |
| _required.yml | Required branch-protection checks, policy gates, tests, coverage, schema, and security gates. | pull_request, push to main |
| auto-label-needs-plan.yml | Applies `state:needs-plan` to opened or reopened issues and exposes the reusable issue-label workflow. | workflow_call, issues opened/reopened |
| auto-label-severity.yml | Reconciles `severity:*` labels from issue form content. | issues opened/edited |
| auto-tag.yml | Manually computes and pushes the next signed release tag. | workflow_dispatch |
| enable-auto-merge-on-implementation-go.yml | Enables squash auto-merge when an eligible PR receives `state:implementation-go`. | pull_request_target labeled |
| hol-plugin-scanner.yml | Runs the Hashgraph Online plugin scanner and uploads SARIF. | push to main, pull_request, workflow_dispatch |
| release.yml | Builds, tests, publishes, and creates releases for tags. | push tags `v*`, workflow_dispatch |
| security.yml | Runs scheduled/manual security scans and PR-time security checks for dependency-sensitive changes. | pull_request paths, schedule, workflow_dispatch |
| test.yml | Runs the cross-Python unit/integration test matrix not duplicated by `_required.yml`. | pull_request, push to main |
