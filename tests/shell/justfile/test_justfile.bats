#!/usr/bin/env bats
# Tests for the project justfile — verifies recipes exist and stay in sync with pixi tasks.

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

# ---------------------------------------------------------------------------
# No heredocs (regression guard — known pitfall with just)
# ---------------------------------------------------------------------------

@test "justfile contains no heredocs" {
    run grep -cE '<<\s*[A-Z_"'"'"']' "$JUSTFILE"
    # grep -c returns exit 1 when count is 0 — that is the success case here
    [ "$status" -eq 1 ] || [ "$output" = "0" ]
}

# ---------------------------------------------------------------------------
# Every pixi [tasks] key has a matching justfile recipe
# ---------------------------------------------------------------------------

@test "all pixi tasks have a corresponding just recipe" {
    local pixi_toml="${REPO_ROOT}/pixi.toml"
    [ -f "$pixi_toml" ] || skip "pixi.toml not found"

    # Get just recipe names (one per line, strip trailing spaces)
    local just_recipes
    just_recipes="$(just --justfile "$JUSTFILE" --list 2>/dev/null | tail -n +2 | awk '{print $1}')"

    # Parse [tasks] keys from pixi.toml (section-bounded, skip comments and blank lines)
    local missing=""
    while IFS= read -r task; do
        # 'audit' runs in a separate lint environment — skip for the default workflow.
        # 'mypy' pixi task is exposed as 'typecheck' in the justfile (UX alias).
        case "$task" in
            audit) continue ;;
            mypy)  continue ;;
        esac
        if ! echo "$just_recipes" | grep -qx "$task"; then
            missing="${missing} ${task}"
        fi
    done < <(sed -n '/^\[tasks\]/,/^\[/{ /^\[/d; /^#/d; /^$/d; s/ *=.*//p; }' "$pixi_toml")

    if [ -n "$missing" ]; then
        echo "pixi tasks missing from justfile:${missing}"
        return 1
    fi
}
