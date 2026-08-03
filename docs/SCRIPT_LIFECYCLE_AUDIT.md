# Script Lifecycle Audit

This is an evidence snapshot, not deletion authorization. A script needs both
no confirmed consumer and a concrete obsolete or replacement rationale before
it can be removed.

## Snapshot and method

- **Repository snapshot:** current working tree, 2026-08-02.
- **Athena main:** [`8fd520d4`](https://github.com/HomericIntelligence/Athena/tree/8fd520d4df62412dbdc78e93a8befc2a1aaa2fb9).
- **Athena open PRs scanned:** [#64](https://github.com/HomericIntelligence/Athena/pull/64)
  and [#62](https://github.com/HomericIntelligence/Athena/pull/62).
- **Evidence sources:** GitHub Actions, pre-commit, justfile, package source,
  tests, runbooks/ADRs, installed console commands, Git history, script
  behavior, Athena skill sources, and open-PR diffs.

The Athena scan found no direct use of `scripts/` paths. Its confirmed
integration is the [`tidy` skill](https://github.com/HomericIntelligence/Athena/blob/8fd520d4df62412dbdc78e93a8befc2a1aaa2fb9/skills/tidy/SKILL.md),
which invokes `hephaestus-tidy` (`hephaestus.github.tidy`). This is a console
integration, not a script dependency.

## Findings

| Script | Classification | Evidence and disposition |
| --- | --- | --- |
| `scripts/README.md` | Active documentation | Complete catalog is enforced by `test_scripts_readme_catalog.py`. |
| `scripts/backup_state.py` | Active operator tool | ADR-0013, backup runbook, and focused unit tests require it. |
| `scripts/check-symlinks.sh` | Active validation | Referenced by repository validation/configuration. |
| `scripts/check_build_dir_untracked.py` | Active validation | Pre-commit hook enforces the build-directory invariant. |
| `scripts/check_conventional_commit.py` | Active CI gate | Required workflow and commit-policy tests invoke it. |
| `scripts/check_dco_signoff.py` | Active CI gate | Required workflow and DCO invariant tests invoke it. |
| `scripts/check_license_compatibility.py` | Active CI gate | Required and security workflows invoke it. |
| `scripts/check_private_denylist.py` | Active pre-commit gate | Pre-commit hook and privacy tests invoke it. |
| `scripts/check_security_policy_no_hardcoded_date.py` | Active pre-commit gate | Pre-commit hook and focused tests invoke it. |
| `scripts/choose_merge_flag.sh` | Retained operator tool | Focused integration coverage; manual-merge helper has no equivalent queue role. |
| `scripts/compare_benchmarks.py` | Retained operator tool | Focused regression test; standalone benchmark-report interface. |
| `scripts/demo_cli.py` | Unresolved | No consumer beyond catalog found; retain until the supported demo/documentation surface is decided. |
| `scripts/example_usage.py` | Retained demonstration | Script smoke coverage preserves the example API surface. |
| `scripts/fix_invalid_links.py` | Unresolved | A similarly named installed markdown command exists, but CLI behavior must be compared before consolidation. |
| `scripts/pi_smoke.py` | Active operator tool | Pi-provider ADR and provider-dispatch tests define its narrow smoke contract. |
| `scripts/pi_smoke_slurm.py` | Active operator tool | Pi ADR/runbook and Slurm-wrapper tests define the scheduler boundary. |
| `scripts/scaffold_subpackage.py` | Redundant legacy candidate | Seven-line shim duplicates the installed `hephaestus-scaffold-subpackage` command. Remove only with its catalog entry and a replacement-invocation migration. |
| `scripts/shell/cleanup-stale-worktrees.sh` | Unresolved | `hephaestus-tidy` overlaps in purpose, but its approval-gated semantics must be compared before replacement. |
| `scripts/shell/coredump-host-handler.sh` | Retained operator tool | Host `core_pattern` pipe handler is distinct from the package coredump command. |
| `scripts/shell/drive_prs_green_ecosystem.sh` | Retained operator tool | Focused BATS coverage and its organization-wide driver behavior remain unique. |
| `scripts/shell/install.sh` | Active installer | `justfile`, installer architecture document, and shell tests invoke it. |
| `scripts/shell/install_hooks.sh` | Retained operator tool | Focused BATS coverage preserves its idempotent bootstrap interface. |
| `scripts/shell/lib/install_helpers.sh` | Active library | Sourced by the installer. |
| `scripts/shell/preflight_check.sh` | Unresolved | No confirmed caller; retain until its six checks are compared with pipeline admission. |
| `scripts/shell/run-under-gdb.sh` | Unresolved | Installed `hephaestus-run-under-gdb` may overlap; compare output and crash-capture behavior first. |
| `scripts/shell/setup_api_key.sh` | Unresolved | Security-sensitive credential export with no confirmed caller; require operator confirmation before any retirement. |
| `scripts/show_prompt.py` | Retained operator tool | Focused structural coverage and unique pipeline-prompt inspection behavior. |
| `scripts/slurm/pi_smoke.sbatch` | Active operator template | Submitted by `pi_smoke_slurm.py` and covered by the Pi contract. |
| `scripts/update_version.py` | Redundant legacy candidate | It writes secondary version files despite the repository's tag-derived hatch-vcs model; use the release/tag workflow instead. |
| `scripts/validate_readme_commands.py` | Unresolved | Unique wrapper around `ReadmeValidator`, but no confirmed invoker beyond the catalog. |

## Follow-up candidates

No deletion occurs in this audit. Before proposing a removal, verify the
replacement or obsolete contract with a focused behavior comparison and a
fresh Athena main/open-PR scan. The two highest-confidence candidates are
`scripts/scaffold_subpackage.py` and `scripts/update_version.py`; all other
unresolved rows require an operator or architectural decision.
