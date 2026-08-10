# Pi Issue 2519 Report

This report is rendered from the private `build/pi-e2e-2519/<run-id>/`
manifest. It summarizes the live Pi/Codex conformance evidence for issue
#2519 and records the reproducible operator artifacts that can be re-generated
from the same run directory.

## Fixture

- Title: `fix(utils): reject negative byte sizes`
- Scope: `hephaestus/utils/helpers.py human_readable_size`
- Regression test: `tests/unit/utils/test_general_utils.py TestHumanReadableSize`

## Evidence Summary

- Run ID: `<run-id>`
- Pi version: `<recorded in run.json>`
- Package inventory: `<recorded in run.json>`
- Skill commands: `skill:advise`, `skill:learn`, `skill:pr-review`
- Session IDs: `<recorded from captured provider JSON events>`
- Tool scopes: `<recorded from captured provider JSON events and proxy argv>`
- Stage outcomes: `<recorded per captured command>`

## Artifacts

- Private run manifest: `build/pi-e2e-2519/<run-id>/run.json`
- Provider proxy log: `build/pi-e2e-2519/<run-id>/provider-proxy.jsonl`
- Captured command outputs: `build/pi-e2e-2519/<run-id>/commands/`
- Defect records: `build/pi-e2e-2519/<run-id>/defects/`
- Rendered runbook: `docs/runbooks/pi-e2e-2519.md`

## Defects

Any defects observed during the live run should be recorded in the private
manifest and mirrored here as follow-up issues before publication.
