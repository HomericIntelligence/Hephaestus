#!/usr/bin/env bats
# Tests for scripts/shell/lib/repo_ordering.sh — the sort helper that
# orders HomericIntelligence repos by open-issue count for
# scripts/run_automation_loop.sh (issue #418, PR #417).

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" rev-parse --show-toplevel)"
    LIB="${REPO_ROOT}/scripts/shell/lib/repo_ordering.sh"

    # Reset the source-once guard so each test re-loads the helper fresh.
    unset REPO_ORDERING_LOADED

    # Per-test fake-gh shim. Bash associative arrays can't be exported to
    # subprocesses, so the fake reads a simple ``name=count`` lookup file
    # whose path is published via FAKE_GH_COUNTS. ``FAKE_GH_FAIL_REPOS``
    # (space-separated) makes the fake exit 1 to exercise the
    # count-defaults-to-zero fallback in the helper.
    GH_FAKE_DIR="$(mktemp -d)"
    FAKE_GH_COUNTS="$GH_FAKE_DIR/counts"
    : > "$FAKE_GH_COUNTS"
    FAKE_GH_FAIL_REPOS=""

    cat > "$GH_FAKE_DIR/gh" <<'FAKE'
#!/usr/bin/env bash
# Fake `gh` used only by tests/shell/scripts/test_repo_ordering.bats.
# Recognizes: gh issue list --repo <ORG>/<NAME> --state open --limit 1000 \
#                          --json number --jq 'length'
set -uo pipefail

repo=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) repo="$2"; shift 2 ;;
        *) shift ;;
    esac
done
name="${repo##*/}"

# Honor FAKE_GH_FAIL_REPOS (space-separated list of repo basenames to fail on).
for failed in ${FAKE_GH_FAIL_REPOS:-}; do
    if [[ "$failed" == "$name" ]]; then
        echo "fake-gh: simulated failure for $name" >&2
        exit 1
    fi
done

# Print the configured count from the lookup file; default 0 when absent.
count=$(awk -F= -v n="$name" '$1==n {print $2; exit}' "$FAKE_GH_COUNTS" 2>/dev/null)
printf '%s\n' "${count:-0}"
FAKE
    chmod +x "$GH_FAKE_DIR/gh"
    PATH="$GH_FAKE_DIR:$PATH"
    export PATH FAKE_GH_COUNTS FAKE_GH_FAIL_REPOS

    # shellcheck source=/dev/null
    source "$LIB"
}

teardown() {
    [[ -d "${GH_FAKE_DIR:-}" ]] && rm -rf "$GH_FAKE_DIR"
}

# write_counts <name>=<count> [...]  — populate FAKE_GH_COUNTS in one line.
write_counts() {
    : > "$FAKE_GH_COUNTS"
    for pair in "$@"; do
        printf '%s\n' "$pair" >> "$FAKE_GH_COUNTS"
    done
}


@test "sort_repos_by_open_count: typical ordering ascending by count" {
    write_counts Foo=10 Bar=2 Baz=5
    run sort_repos_by_open_count "TestOrg" Foo Bar Baz
    [ "$status" -eq 0 ]
    expected=$'Bar\nBaz\nFoo'
    [ "$output" = "$expected" ]
}


@test "sort_repos_by_open_count: ties preserve input order (stable sort)" {
    write_counts Alpha=3 Beta=3 Gamma=3
    run sort_repos_by_open_count "TestOrg" Gamma Alpha Beta
    [ "$status" -eq 0 ]
    expected=$'Gamma\nAlpha\nBeta'
    [ "$output" = "$expected" ]
}


@test "sort_repos_by_open_count: single repo is a no-op" {
    write_counts Solo=42
    run sort_repos_by_open_count "TestOrg" Solo
    [ "$status" -eq 0 ]
    [ "$output" = "Solo" ]
}


@test "sort_repos_by_open_count: zero candidates yields empty output" {
    run sort_repos_by_open_count "TestOrg"
    [ "$status" -eq 0 ]
    [ "$output" = "" ]
}


@test "sort_repos_by_open_count: gh failure for a repo falls back to count=0" {
    # Bar's gh call fails — the helper must treat its count as 0 and place
    # it FIRST in the ordering, not exit non-zero.
    write_counts Foo=10 Bar=999 Baz=5
    FAKE_GH_FAIL_REPOS="Bar"
    export FAKE_GH_FAIL_REPOS

    run sort_repos_by_open_count "TestOrg" Foo Bar Baz
    [ "$status" -eq 0 ]
    expected=$'Bar\nBaz\nFoo'
    [ "$output" = "$expected" ]
}


@test "sort_repos_by_open_count: large counts sort numerically (not lex)" {
    # Lexicographic sort would put "10" before "9"; numeric must not.
    write_counts Big=10 Small=9 Tiny=2
    run sort_repos_by_open_count "TestOrg" Big Small Tiny
    [ "$status" -eq 0 ]
    expected=$'Tiny\nSmall\nBig'
    [ "$output" = "$expected" ]
}


@test "count_open_issues: returns the count for a known repo" {
    write_counts X=7
    run count_open_issues "TestOrg" X
    [ "$status" -eq 0 ]
    [ "$output" = "7" ]
}


@test "count_open_issues: gh failure returns 0" {
    FAKE_GH_FAIL_REPOS="X"
    export FAKE_GH_FAIL_REPOS
    run count_open_issues "TestOrg" X
    [ "$status" -eq 0 ]
    [ "$output" = "0" ]
}


@test "count_open_issues: empty gh stdout returns 0" {
    # Repo not in counts file → fake gh emits "0" because awk's awk-then-default
    # branch handles missing keys. Verifies the caller does NOT propagate an
    # empty string.
    run count_open_issues "TestOrg" Missing
    [ "$status" -eq 0 ]
    [ "$output" = "0" ]
}


@test "repo_ordering.sh: source-guard is idempotent" {
    # Sourcing twice in the same shell must not error out or re-define
    # variables with different values.
    run bash -c "source '$LIB' && source '$LIB' && echo loaded=\${REPO_ORDERING_LOADED}"
    [ "$status" -eq 0 ]
    [ "$output" = "loaded=1" ]
}


@test "run_automation_loop.sh wires sort_repos_by_open_count" {
    # Regression guard: the production driver must source the helper.
    # Catches accidental re-inlining of the sort pipeline.
    run grep -E "source .*repo_ordering\.sh|sort_repos_by_open_count" \
        "$REPO_ROOT/scripts/run_automation_loop.sh"
    [ "$status" -eq 0 ]
}
