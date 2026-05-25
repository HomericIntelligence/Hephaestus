#!/usr/bin/env bats
# Tests for scripts/run_automation_loop.sh — specifically the `process_repo`
# 6-phase pipeline and the `phase_enabled` dependency-ordering validation.
#
# These tests are load-bearing regression tests for:
#   * issue #555 — bats coverage for process_repo (silent-abort fix)
#   * issue #557 — root-cause documentation for the original silent abort
#   * issue #559 — process_repo returns non-zero when any phase fails
#   * issue #554 — phase lifecycle (START/done/SKIP/Warning) routes to stderr
#   * issue #562 — --phases dependency-ordering warnings
#
# Strategy: extract `phase_enabled`, `phase_start`, `phase_done`, and
# `process_repo` from the production script via sed line-range, source them
# into the bats shell, and drive them with a PATH full of stubs.

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" rev-parse --show-toplevel)"
    SCRIPT="${REPO_ROOT}/scripts/run_automation_loop.sh"
    [[ -f "$SCRIPT" ]]

    # Sandbox dirs
    TEST_TMPDIR="$(mktemp -d)"
    PROJECTS_DIR="$TEST_TMPDIR/projects"
    STUB_DIR="$TEST_TMPDIR/stubs"
    mkdir -p "$PROJECTS_DIR" "$STUB_DIR"

    # Make a fake repo directory so `cd $dir` in phase subshells doesn't fail.
    REPO_NAME="fakerepo"
    REPO_DIR="$PROJECTS_DIR/$REPO_NAME"
    mkdir -p "$REPO_DIR"
    git -C "$REPO_DIR" init -q
    git -C "$REPO_DIR" config user.email "t@t" && git -C "$REPO_DIR" config user.name "t"
    echo hi > "$REPO_DIR/README.md"
    git -C "$REPO_DIR" add README.md
    git -C "$REPO_DIR" -c commit.gpgsign=false commit -q -m init

    # ------------------------------------------------------------------
    # Stub binaries. Each stub honors STUB_<NAME>_EXIT for the exit code
    # so individual tests can force failures.
    # ------------------------------------------------------------------
    make_stub() {
        local name="$1" exit_var="$2"
        cat > "$STUB_DIR/$name" <<EOF
#!/usr/bin/env bash
echo "stub:$name args=\$*" >&2
exit \${$exit_var:-0}
EOF
        chmod +x "$STUB_DIR/$name"
    }
    make_stub "hephaestus-plan-issues"      STUB_PLAN_EXIT
    make_stub "hephaestus-implement-issues" STUB_IMPL_EXIT
    # The Python phases (review_plans / review_issues / address_review /
    # drive_prs_green) are invoked via "$PYTHON $SCRIPT_DIR/<file>".
    # We override PYTHON to a stub launcher that switches on argv[0] basename.
    cat > "$STUB_DIR/stub-python" <<'EOF'
#!/usr/bin/env bash
# Fake $PYTHON: dispatch based on the second arg (the .py path) so we
# can vary exit codes per phase via STUB_<NAME>_EXIT.
script_path="${1:-}"
shift || true
name="$(basename "${script_path:-unknown}" .py)"
echo "stub:python:$name args=$*" >&2
case "$name" in
    review_plans)    exit "${STUB_REVIEW_PLANS_EXIT:-0}" ;;
    review_issues)   exit "${STUB_REVIEW_ISSUES_EXIT:-0}" ;;
    address_review)  exit "${STUB_ADDRESS_REVIEW_EXIT:-0}" ;;
    drive_prs_green) exit "${STUB_DRIVE_GREEN_EXIT:-0}" ;;
    *)               exit "${STUB_PYTHON_DEFAULT_EXIT:-0}" ;;
esac
EOF
    chmod +x "$STUB_DIR/stub-python"

    # Stub gh: only `gh issue list` is called inside process_repo (via mapfile).
    cat > "$STUB_DIR/gh" <<'EOF'
#!/usr/bin/env bash
# Fake gh: only `gh issue list --repo … --state open --limit … --json number --jq …` is used.
if [[ "${1:-}" == "issue" && "${2:-}" == "list" ]]; then
    # OPEN_ISSUES is a newline-separated list of issue numbers
    if [[ -n "${STUB_GH_ISSUES:-}" ]]; then
        printf '%s\n' ${STUB_GH_ISSUES}
    fi
    exit 0
fi
echo "stub:gh:unhandled $*" >&2
exit 0
EOF
    chmod +x "$STUB_DIR/gh"

    # Prepend stubs to PATH so they shadow real binaries.
    export PATH="$STUB_DIR:$PATH"

    # ------------------------------------------------------------------
    # Extract just the function defs we need from the production script.
    # We cannot source the script wholesale because it runs preflight + the
    # main loop at top level.
    # ------------------------------------------------------------------
    EXTRACT="$TEST_TMPDIR/extract.sh"
    {
        # Extract phase_enabled (start 159, end with closing brace ~164)
        sed -n '/^phase_enabled() {/,/^}/p' "$SCRIPT"
        # Extract phase_start
        sed -n '/^phase_start() {/,/^}/p' "$SCRIPT"
        # Extract phase_done
        sed -n '/^phase_done() {/,/^}/p' "$SCRIPT"
        # Extract process_repo
        sed -n '/^process_repo() {/,/^}/p' "$SCRIPT"
    } > "$EXTRACT"

    # Required globals (would normally be set near the top of the script).
    export PROJECTS_DIR
    export PLAN_BIN="$STUB_DIR/hephaestus-plan-issues"
    export IMPL_BIN="$STUB_DIR/hephaestus-implement-issues"
    export PYTHON="$STUB_DIR/stub-python"
    SCRIPT_DIR="$TEST_TMPDIR"   # review_plans.py etc. paths just need a dir
    : > "$SCRIPT_DIR/review_plans.py"
    : > "$SCRIPT_DIR/review_issues.py"
    : > "$SCRIPT_DIR/address_review.py"
    : > "$SCRIPT_DIR/drive_prs_green.py"
    export SCRIPT_DIR
    export ORG="TestOrg"
    export MAX_WORKERS=1
    export LOOPS=1
    export DRY_RUN=0
    export PHASES="plan,review-plans,implement,review-prs,address-review,drive-green"
    export DRY_RUN_FLAGS=()   # not actually exportable; reset in driver
    export STUB_GH_ISSUES=""   # default empty
}

teardown() {
    rm -rf "$TEST_TMPDIR"
}

# Helper: run process_repo with extracted defs and stdout/stderr split.
# Captures combined output in $output but also writes split streams to
# $STDOUT_FILE and $STDERR_FILE for stream-routing assertions.
run_process_repo() {
    local repo="${1:-$REPO_NAME}" loop="${2:-1}"
    STDOUT_FILE="$TEST_TMPDIR/stdout"
    STDERR_FILE="$TEST_TMPDIR/stderr"
    : > "$STDOUT_FILE"
    : > "$STDERR_FILE"
    # We need split stdout/stderr but also bats' $status. Run inline (not
    # via `run`) with redirects into files, then capture $? manually.
    set +e
    bash -c '
        set -uo pipefail
        DRY_RUN_FLAGS=()
        # shellcheck source=/dev/null
        source "'"$EXTRACT"'"
        process_repo "'"$repo"'" "'"$loop"'"
    ' > "$STDOUT_FILE" 2> "$STDERR_FILE"
    status=$?
    set -e
    STDOUT="$(cat "$STDOUT_FILE")"
    STDERR="$(cat "$STDERR_FILE")"
    output="$STDOUT"$'\n'"$STDERR"
}


# ---------------------------------------------------------------------------
# Tests for process_repo
# ---------------------------------------------------------------------------

@test "process_repo: all 6 phase banners appear with non-empty OPEN_ISSUES" {
    export STUB_GH_ISSUES="42"
    export LOOPS=1   # so phase 6 (drive-green) runs on this single loop
    run_process_repo "$REPO_NAME" 1
    [ "$status" -eq 0 ]
    # Phase START banners (one per phase) — all 6 should fire on stderr.
    [[ "$STDERR" == *"phase 1/6 plan START"* ]]
    [[ "$STDERR" == *"phase 2/6 review-plans START"* ]]
    [[ "$STDERR" == *"phase 3/6 implement START"* ]]
    [[ "$STDERR" == *"phase 4/6 review-prs START"* ]]
    [[ "$STDERR" == *"phase 5/6 address-review START"* ]]
    [[ "$STDERR" == *"phase 6/6 drive-green START"* ]]
    # done lines too
    [[ "$STDERR" == *"phase 1/6 plan done"* ]]
    [[ "$STDERR" == *"phase 6/6 drive-green done"* ]]
}

@test "process_repo: SKIP banners on phases 2/4/5/6 with empty OPEN_ISSUES" {
    export STUB_GH_ISSUES=""   # empty issue list
    export LOOPS=1
    run_process_repo "$REPO_NAME" 1
    [ "$status" -eq 0 ]
    # Phases 1 and 3 still run (they auto-discover internally).
    [[ "$STDERR" == *"phase 1/6 plan START"* ]]
    [[ "$STDERR" == *"phase 3/6 implement START"* ]]
    # Phases 2/4/5/6 skip with "(no open issues)".
    [[ "$STDERR" == *"phase 2/6 review-plans SKIP (no open issues)"* ]]
    [[ "$STDERR" == *"phase 4/6 review-prs SKIP (no open issues)"* ]]
    [[ "$STDERR" == *"phase 5/6 address-review SKIP (no open issues)"* ]]
    [[ "$STDERR" == *"phase 6/6 drive-green SKIP (no open issues)"* ]]
}

@test "process_repo: returns 0 when all phases exit 0" {
    export STUB_GH_ISSUES="42"
    export LOOPS=1
    run_process_repo "$REPO_NAME" 1
    [ "$status" -eq 0 ]
}

@test "process_repo: returns non-zero when phase 3 stub exits 1" {
    # Regression for #559: process_repo MUST propagate phase failures.
    export STUB_GH_ISSUES="42"
    export STUB_IMPL_EXIT=1
    export LOOPS=1
    run_process_repo "$REPO_NAME" 1
    [ "$status" -ne 0 ]
    # The Warning line for the failing phase must appear on stderr.
    [[ "$STDERR" == *"Warning: implement-issues exited rc=1"* ]]
}

@test "process_repo: phase 2-6 still run when phase 1 exits non-zero" {
    # Load-bearing silent-abort regression test (#555).
    # If a future refactor re-introduces `set -e` without the `||` guards,
    # phase 2 onwards will silently disappear from the output.
    export STUB_GH_ISSUES="42"
    export STUB_PLAN_EXIT=1
    export LOOPS=1
    run_process_repo "$REPO_NAME" 1
    # Phase 1 must have logged its warning.
    [[ "$STDERR" == *"Warning: plan-issues exited rc=1"* ]]
    # Phases 2-6 MUST still fire their START banners.
    [[ "$STDERR" == *"phase 2/6 review-plans START"* ]]
    [[ "$STDERR" == *"phase 3/6 implement START"* ]]
    [[ "$STDERR" == *"phase 4/6 review-prs START"* ]]
    [[ "$STDERR" == *"phase 5/6 address-review START"* ]]
    [[ "$STDERR" == *"phase 6/6 drive-green START"* ]]
    # And process_repo returns non-zero overall.
    [ "$status" -ne 0 ]
}

@test "process_repo: phase banners route to stderr (#554)" {
    # After Bundle C's #554 fix, START / done / SKIP / Warning all go to
    # stderr. Stdout should not contain any of those lifecycle markers.
    export STUB_GH_ISSUES="42"
    export STUB_IMPL_EXIT=1
    export LOOPS=1
    run_process_repo "$REPO_NAME" 1
    # Stderr has the lifecycle.
    [[ "$STDERR" == *"phase 1/6 plan START"* ]]
    [[ "$STDERR" == *"phase 1/6 plan done"* ]]
    [[ "$STDERR" == *"Warning: implement-issues"* ]]
    # Stdout must NOT contain the START/done/SKIP/Warning banners.
    [[ "$STDOUT" != *"plan START"* ]]
    [[ "$STDOUT" != *"plan done"* ]]
    [[ "$STDOUT" != *"Warning:"* ]]
    [[ "$STDOUT" != *"SKIP"* ]]
}


# ---------------------------------------------------------------------------
# Test for the `--phases` dependency-ordering warning (#562)
# ---------------------------------------------------------------------------

@test "phase_enabled --phases plan,implement: warns about missing review-plans" {
    # Drive the script header (which contains the dependency-ordering check)
    # with a non-default --phases and capture stderr. We invoke the real
    # script with --help-style dry args: feed it a doomed extra arg so it
    # exits quickly after the validation block but BEFORE the pixi/which lookup.
    # That isn't actually possible in-line, so instead we exercise the check
    # by re-implementing the same conditional in a sub-shell with the same
    # variables — this preserves the regression intent (warning text presence).

    # Simpler approach: grep the script for the warning literal AND assert
    # the conditional structure is present. This catches accidental removal
    # of the dependency check.
    run grep -F \
        "WARNING: --phases includes 'implement' but not 'review-plans'" \
        "$SCRIPT"
    [ "$status" -eq 0 ]

    # Additionally drive the actual block: extract lines 188-198 (the
    # ALLOW_UNSAFE_PHASE_ORDER check) and source them with a controlled
    # PHASES env to confirm the warning fires.
    DEP_CHECK="$TEST_TMPDIR/dep_check.sh"
    sed -n '/^phase_in_list() {/,/^fi$/p' "$SCRIPT" > "$DEP_CHECK"
    run bash -c '
        set -uo pipefail
        PHASES="plan,implement"
        ALLOW_UNSAFE_PHASE_ORDER=0
        # shellcheck source=/dev/null
        source "'"$DEP_CHECK"'"
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"--phases includes 'implement' but not 'review-plans'"* ]]
}


# ---------------------------------------------------------------------------
# Sanity guards
# ---------------------------------------------------------------------------

@test "run_automation_loop.sh: shellcheck-clean syntax (bash -n)" {
    run bash -n "$SCRIPT"
    [ "$status" -eq 0 ]
}

@test "run_automation_loop.sh: process_repo body documents #557 root cause" {
    # Regression for #557: the root-cause comment near `set +e` must mention
    # the bisected offender (`git fetch`). If a future maintainer rewrites
    # the comment to remove this evidence, this test catches it.
    run grep -E "Root cause of the original silent abort.*#557|git -C .*fetch origin" \
        "$SCRIPT"
    [ "$status" -eq 0 ]
}
