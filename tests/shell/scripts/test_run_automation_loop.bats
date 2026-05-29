#!/usr/bin/env bats
# Tests for scripts/run_automation_loop.sh — specifically the `process_repo`
# 3-stage pipeline (plan → implement → drive-green) and the `phase_enabled`
# dependency-ordering validation.
#
# These tests are load-bearing regression tests for:
#   * issue #555 — bats coverage for process_repo (silent-abort fix)
#   * issue #557 — root-cause documentation for the original silent abort
#   * issue #559 — process_repo returns non-zero when any stage fails
#   * issue #554 — stage lifecycle (START/done/SKIP/Warning) routes to stderr
#   * issue #562 — --phases dependency-ordering warnings
#
# The pipeline collapsed from 6 phases to 3 stages: plan-review folded into the
# planner's in-loop review and PR-review + address-review folded into the
# implementer's in-loop steps, so only plan / implement / drive-green remain as
# standalone stages here.
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
    # The only remaining Python stage entrypoint (drive_prs_green) is invoked
    # via "$PYTHON $SCRIPT_DIR/<file>". We override PYTHON to a stub launcher
    # that switches on argv[0] basename.
    cat > "$STUB_DIR/stub-python" <<'EOF'
#!/usr/bin/env bash
# Fake $PYTHON: dispatch based on the second arg (the .py path) so we
# can vary exit codes per stage via STUB_<NAME>_EXIT.
script_path="${1:-}"
shift || true
name="$(basename "${script_path:-unknown}" .py)"
echo "stub:python:$name args=$*" >&2
case "$name" in
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
    SCRIPT_DIR="$TEST_TMPDIR"   # drive_prs_green.py path just needs a dir
    : > "$SCRIPT_DIR/drive_prs_green.py"
    export SCRIPT_DIR
    export ORG="TestOrg"
    export MAX_WORKERS=1
    export LOOPS=1
    export DRY_RUN=0
    export PHASES="plan,implement,drive-green"
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

@test "process_repo: all 3 stage banners appear with non-empty OPEN_ISSUES" {
    export STUB_GH_ISSUES="42"
    export LOOPS=1   # so stage 3 (drive-green) runs on this single loop
    run_process_repo "$REPO_NAME" 1
    [ "$status" -eq 0 ]
    # Stage START banners (one per stage) — all 3 should fire on stderr.
    [[ "$STDERR" == *"stage 1/3 plan START"* ]]
    [[ "$STDERR" == *"stage 2/3 implement START"* ]]
    [[ "$STDERR" == *"stage 3/3 drive-green START"* ]]
    # done lines too
    [[ "$STDERR" == *"stage 1/3 plan done"* ]]
    [[ "$STDERR" == *"stage 3/3 drive-green done"* ]]
}

@test "process_repo: only drive-green SKIPs on empty OPEN_ISSUES" {
    export STUB_GH_ISSUES=""   # empty issue list
    export LOOPS=1
    run_process_repo "$REPO_NAME" 1
    [ "$status" -eq 0 ]
    # plan and implement still run (they auto-discover internally).
    [[ "$STDERR" == *"stage 1/3 plan START"* ]]
    [[ "$STDERR" == *"stage 2/3 implement START"* ]]
    # Only drive-green requires --issues, so it is the lone "(no open issues)" SKIP.
    [[ "$STDERR" == *"stage 3/3 drive-green SKIP (no open issues)"* ]]
}

@test "process_repo: returns 0 when all stages exit 0" {
    export STUB_GH_ISSUES="42"
    export LOOPS=1
    run_process_repo "$REPO_NAME" 1
    [ "$status" -eq 0 ]
}

@test "process_repo: returns non-zero when the implement stage stub exits 1" {
    # Regression for #559: process_repo MUST propagate stage failures.
    export STUB_GH_ISSUES="42"
    export STUB_IMPL_EXIT=1
    export LOOPS=1
    run_process_repo "$REPO_NAME" 1
    [ "$status" -ne 0 ]
    # The Warning line for the failing stage must appear on stderr.
    [[ "$STDERR" == *"Warning: implement-issues exited rc=1"* ]]
}

@test "process_repo: later stages still run when the plan stage exits non-zero" {
    # Load-bearing silent-abort regression test (#555).
    # If a future refactor re-introduces `set -e` without the `||` guards,
    # the stages after plan will silently disappear from the output.
    export STUB_GH_ISSUES="42"
    export STUB_PLAN_EXIT=1
    export LOOPS=1
    run_process_repo "$REPO_NAME" 1
    # The plan stage must have logged its warning.
    [[ "$STDERR" == *"Warning: plan-issues exited rc=1"* ]]
    # implement + drive-green MUST still fire their START banners.
    [[ "$STDERR" == *"stage 2/3 implement START"* ]]
    [[ "$STDERR" == *"stage 3/3 drive-green START"* ]]
    # And process_repo returns non-zero overall.
    [ "$status" -ne 0 ]
}

@test "process_repo: stage banners route to stderr (#554)" {
    # After Bundle C's #554 fix, START / done / SKIP / Warning all go to
    # stderr. Stdout should not contain any of those lifecycle markers.
    export STUB_GH_ISSUES="42"
    export STUB_IMPL_EXIT=1
    export LOOPS=1
    run_process_repo "$REPO_NAME" 1
    # Stderr has the lifecycle.
    [[ "$STDERR" == *"stage 1/3 plan START"* ]]
    [[ "$STDERR" == *"stage 1/3 plan done"* ]]
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

@test "phase_enabled --phases drive-green: warns about missing implement" {
    # The only surviving cross-stage ordering hazard is running `drive-green`
    # without `implement` (plan-review + PR-review/address-review are now
    # in-loop steps, not standalone stages).

    # Grep the script for the warning literal AND assert the conditional
    # structure is present. This catches accidental removal of the check.
    run grep -F \
        "WARNING: --phases includes 'drive-green' but not 'implement'" \
        "$SCRIPT"
    [ "$status" -eq 0 ]

    # Additionally drive the actual block: extract the ALLOW_UNSAFE_PHASE_ORDER
    # check and source it with a controlled PHASES env to confirm the warning
    # fires when drive-green is selected without implement.
    DEP_CHECK="$TEST_TMPDIR/dep_check.sh"
    sed -n '/^phase_in_list() {/,/^fi$/p' "$SCRIPT" > "$DEP_CHECK"
    run bash -c '
        set -uo pipefail
        PHASES="drive-green"
        ALLOW_UNSAFE_PHASE_ORDER=0
        # shellcheck source=/dev/null
        source "'"$DEP_CHECK"'"
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"--phases includes 'drive-green' but not 'implement'"* ]]
}

@test "phase_enabled --phases plan,implement: no dependency warning" {
    # Selecting plan + implement (without drive-green) must be silent: there is
    # no cross-stage predecessor hazard for that subset.
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
    [[ "$output" != *"WARNING:"* ]]
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
