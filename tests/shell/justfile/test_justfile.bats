#!/usr/bin/env bats
# Tests for the project justfile — verifies its public recipes exist.

REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" rev-parse --show-toplevel)"
JUSTFILE="${REPO_ROOT}/justfile"

# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------

@test "justfile exists at project root" {
    [ -f "$JUSTFILE" ]
}

# ---------------------------------------------------------------------------
# just --list succeeds
# ---------------------------------------------------------------------------

@test "just --list succeeds" {
    run just --justfile "$JUSTFILE" --list
    [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Expected recipes are present
# ---------------------------------------------------------------------------

@test "justfile contains 'bootstrap' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"bootstrap"* ]]
}

@test "justfile contains 'test' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"test "* ]] || [[ "$output" == *"test"$'\n'* ]]
}

@test "justfile contains 'test-unit' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"test-unit"* ]]
}

@test "justfile contains 'test-integration' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"test-integration"* ]]
}

@test "justfile contains 'lint' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"lint "* ]] || [[ "$output" == *"lint"$'\n'* ]]
}

@test "justfile contains 'format' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"format "* ]] || [[ "$output" == *"format"$'\n'* ]]
}

@test "justfile contains 'format-check' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"format-check"* ]]
}

@test "justfile contains 'typecheck' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"typecheck"* ]]
}

@test "justfile contains 'precommit' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"precommit"* ]]
}

@test "justfile contains 'check' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"check "* ]] || [[ "$output" == *"check"$'\n'* ]]
}

@test "justfile contains 'all' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"all "* ]] || [[ "$output" == *"all"$'\n'* ]]
}

@test "justfile contains 'audit' recipe" {
    run just --justfile "$JUSTFILE" --list
    [[ "$output" == *"audit"* ]]
}

@test "'audit' recipe pipes pip-audit through hephaestus-filter-audit" {
    run grep -A1 '^audit:' "$JUSTFILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"pip-audit --format json"* ]]
    [[ "$output" == *"hephaestus-filter-audit"* ]]
    [[ "$output" != *"--ignore-vuln"* ]]
}

# ---------------------------------------------------------------------------
# No heredocs (regression guard — known pitfall with just)
# ---------------------------------------------------------------------------

@test "justfile contains no heredocs" {
    run grep -cE '<<\s*[A-Z_"'"'"']' "$JUSTFILE"
    # grep -c returns exit 1 when count is 0 — that is the success case here
    [ "$status" -eq 1 ] || [ "$output" = "0" ]
}
