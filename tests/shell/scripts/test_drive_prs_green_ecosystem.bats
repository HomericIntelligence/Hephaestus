#!/usr/bin/env bats
# Tests for scripts/shell/drive_prs_green_ecosystem.sh — the three honesty
# fixes from #848:
#   * issue enumeration uses ``hephaestus_gh api --paginate`` (no --limit 200 cap)
#   * open bot-authored PRs are counted alongside issues so a repo with
#     only Dependabot PRs is NOT misclassified as "skip — no issues"
#   * the driver is invoked WITHOUT --issues when the issue list is empty
#     but bot PRs exist (the driver's --include-bot-prs default handles it)
#
# These are LOAD-BEARING regression tests: a future edit that re-introduces
# a --limit cap or that re-skips bot-only repos must trip these.

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" rev-parse --show-toplevel)"
    SCRIPT="${REPO_ROOT}/scripts/shell/drive_prs_green_ecosystem.sh"
    [[ -f "$SCRIPT" ]]
}

@test "drive_prs_green_ecosystem.sh: shellcheck-clean syntax (bash -n)" {
    run bash -n "$SCRIPT"
    [ "$status" -eq 0 ]
}

@test "drive_prs_green_ecosystem.sh: issue enumeration uses shared gh adapter with paginate" {
    # The --limit 200 cap silently dropped older issues in repos with more
    # than 200 open. The fix must use the REST endpoint with --paginate.
    run grep -F 'hephaestus_gh api --paginate "/repos/$ORG/$REPO/issues?state=open&per_page=100"' "$SCRIPT"
    [ "$status" -eq 0 ]
}

@test "drive_prs_green_ecosystem.sh: throttle flags are forwarded to child driver" {
    run grep -F '"${GH_ARGS[@]}" \' "$SCRIPT"
    [ "$status" -eq 0 ]
    run grep -F 'hephaestus_gh repo list "$ORG"' "$SCRIPT"
    [ "$status" -eq 0 ]
}

@test "drive_prs_green_ecosystem.sh: help documents both throttle flags" {
    run grep -F -- '--gh-global-rate 5' "$SCRIPT"
    [ "$status" -eq 0 ]
    run grep -F -- '--gh-global-burst 20' "$SCRIPT"
    [ "$status" -eq 0 ]
}

@test "drive_prs_green_ecosystem.sh: help documents explicit gh wrapper and context flags" {
    run grep -F -- '--gh-bin hephaestus-gh' "$SCRIPT"
    [ "$status" -eq 0 ]
    run grep -F -- '--org HomericIntelligence' "$SCRIPT"
    [ "$status" -eq 0 ]
    run grep -F -- '--project-root /path/to/Hephaestus' "$SCRIPT"
    [ "$status" -eq 0 ]
}

@test "drive_prs_green_ecosystem.sh: does not read HEPHAESTUS env configuration" {
    run grep -E 'HEPHAESTUS_(GH|ORG|DIR)' "$SCRIPT"
    [ "$status" -ne 0 ]
}

@test "drive_prs_green_ecosystem.sh: --limit 200 is NOT used on issue enumeration" {
    # The audit of run 20260531T190615Z proved this cap was silently in
    # effect. Re-introduction would re-introduce the bug.
    run grep -F 'gh issue list --repo "$ORG/$REPO" --state open --limit 200' "$SCRIPT"
    [ "$status" -ne 0 ]
}

@test "drive_prs_green_ecosystem.sh: enumerates open bot-authored PRs" {
    # Bot PRs lack Closes #N links so the issue-driven path is blind to
    # them. The script must probe them so a Dependabot-only repo is not
    # classified "skip — no issues".
    run grep -F 'hephaestus_gh api --paginate "/repos/$ORG/$REPO/pulls?state=open&per_page=100"' "$SCRIPT"
    [ "$status" -eq 0 ]
    run grep -F 'select(.user.type == "Bot")' "$SCRIPT"
    [ "$status" -eq 0 ]
}

@test "drive_prs_green_ecosystem.sh: skip-no-issues gate requires BOTH issues and bot PRs empty" {
    # A repo with zero issues but non-zero open bot PRs must NOT be skipped.
    run grep -F '${#ISSUES[@]} -eq 0 && "$BOT_PR_COUNT" -eq 0' "$SCRIPT"
    [ "$status" -eq 0 ]
}

@test "drive_prs_green_ecosystem.sh: driver invoked without --issues when issue list empty" {
    # When only bot PRs are present the driver must run on its own
    # bot-PR enumeration; passing --issues with an empty array would
    # trip argparse.
    run grep -nF 'if ((${#ISSUES[@]}))' "$SCRIPT"
    [ "$status" -eq 0 ]
}
