# Installer Architecture

## Purpose

`scripts/shell/install.sh` is the role-gated check-and-install driver for HomericIntelligence ecosystem dependencies. It is invoked from `justfile` recipes `check-deps` and `install-deps` (lines 81 and 85) to verify or install the tools, libraries, and environment needed to build and run the distributed agent mesh.

## The SRP Boundary That Exists

Reusable primitives have already been extracted to `scripts/shell/lib/install_helpers.sh` (72 lines, source-guarded by `INSTALL_HELPERS_LOADED`). This is where the Single Responsibility Principle work has been done:

- Color codes (`RED`, `GREEN`, `BLUE`, `NC`)
- Result counters (`_PASS`, `_FAIL`, `_WARN`, `_SKIP`)
- Helper functions: `has_cmd`, `apt_install`, `install_github_binary`, `version_gte`

The 12 numbered sections (Section 0 Homebrew through Section 11 PATH + trailing summary) remain in `install.sh` and share this extracted infrastructure.

## Why the 12 Numbered Sections Stay Together

Three verified pillars justify keeping the sections in a single file:

### 1. Shared Result Counters

Every section increments `_PASS`/`_FAIL`/`_WARN`/`_SKIP` counters (defined in `lib/install_helpers.sh:21`) to track installation outcomes. Splitting into per-tool installers would require either:

- Replicating the counter logic in each child script, or
- Building a dispatcher that threads the counters through every call

Both violate KISS/YAGNI for a script with no current caller pain.

### 2. The `--role` Filter

A single `--role` argument (values: `all`, `worker`, `control`) is parsed once at the entry point and used by the `should_check_worker` and `should_check_control` helper functions (lines 53–54 of `install.sh`) to gate entire sections. All 12 sections obey the same filter. A per-tool split would require either:

- Replicating role-gating logic in each child, or
- Threading role state through a dispatcher

### 3. The Unified Trailing Summary

The final summary block (`install.sh:968–988`) aggregates results from **all sections** to produce a single outcome report. Splitting would require coordinating summary output across N processes, or building a state-collection mechanism at the dispatcher level.

## What the Script Is *Not*

The entry-point guard at `install.sh:178–180` returns early when the script is
sourced rather than executed (its inline comment anticipates Odysseus phase
scripts as the intended sourcing consumer).

**The guard is load-bearing.** The regression suite
`tests/shell/scripts/test_install_install_branches.bats` sources `install.sh`
in its `setup()` (line 15) and depends on the guard returning before argument
parsing so it can exercise the helper functions (`check_pass`/`check_fail`) and
counters (`_PASS`/`_FAIL`/`_WARN`/`_SKIP`) directly. The suite's first test,
`@test "sourcing guard exposes helpers and counters"`, explicitly asserts the
guard worked. The guard must not be removed without updating that suite.

An honest grep that includes `tests/` confirms the in-repo consumer exists:

```bash
grep -rn "source .*SRC_SCRIPT\|SRC_SCRIPT=.*install\.sh" \
     scripts/ docs/ .github/ tests/ README.md CONTRIBUTING.md justfile
# Result:
#   tests/shell/scripts/test_install_install_branches.bats:12:    SRC_SCRIPT="${REPO_ROOT}/scripts/shell/install.sh"
#   tests/shell/scripts/test_install_install_branches.bats:15:    source "$SRC_SCRIPT"
```

That consumer is narrow (test-only) and does not justify the monolith's
structure on its own. The three verified pillars above — shared counters,
`--role` filter, and unified summary — remain the load-bearing justifications
for keeping the 12 sections together.

## Triggers That Would Justify Revisiting This Decision

Any one of the following would signal that a per-tool split is warranted:

1. Any single section grows beyond ~150 lines (sign of overgrown responsibility within that tool)
2. A tool needs an independent public entry point for direct invocation or library use
3. Role gating becomes per-section rather than per-block (sign that the `--role` filter is a poor fit)
4. A verified caller starts sourcing the script as a library (would break the current "defensive guard" assumption)

## Reference

Closes #792 (audit nitpick from `/hephaestus:repo-analyze-strict-full` on 2026-05-29, S13).
