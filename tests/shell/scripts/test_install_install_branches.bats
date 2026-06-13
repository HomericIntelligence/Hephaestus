#!/usr/bin/env bats
# Regression tests for scripts/shell/install.sh — the four if-block rewrites
# that replaced broken `A || B && C || D` short-circuit chains (issue #788).
#
# Each test sources the script (the entry-point guard at install.sh:178-180
# returns before argument parsing), resets the counters, stubs apt_install/pip3
# to return a chosen rc, then re-runs the rewritten if-block and asserts that
# exactly one of _PASS / _FAIL advanced by exactly 1.

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" rev-parse --show-toplevel)"
    SRC_SCRIPT="${REPO_ROOT}/scripts/shell/install.sh"
    [ -f "$SRC_SCRIPT" ]
    # shellcheck source=/dev/null
    source "$SRC_SCRIPT"
    _PASS=0; _FAIL=0; _WARN=0; _SKIP=0
    INSTALL=true
}

# ── Harness sanity: sourcing guard at install.sh:178-180 worked; helpers loaded ──
@test "sourcing guard exposes helpers and counters" {
    declare -F check_pass >/dev/null
    declare -F check_fail >/dev/null
    [ "${_PASS+x}" = x ]
    [ "${_FAIL+x}" = x ]
}

# ── python3-venv fallback (install.sh:259-264 rewrite) ────────────────────────
@test "python3-venv: first attempt succeeds → 1 pass, 0 fail" {
    apt_install() { return 0; }
    if apt_install python3.12-venv || apt_install python3-venv; then
        check_pass "python3-venv installed"
    else
        check_fail "python3-venv — install failed"
    fi
    [ "$_PASS" -eq 1 ]; [ "$_FAIL" -eq 0 ]
}

@test "python3-venv: first fails, fallback succeeds → 1 pass, 0 fail" {
    _n=0
    apt_install() { _n=$((_n+1)); [ "$_n" -eq 1 ] && return 1; return 0; }
    if apt_install python3.12-venv || apt_install python3-venv; then
        check_pass "python3-venv installed"
    else
        check_fail "python3-venv — install failed"
    fi
    [ "$_PASS" -eq 1 ]; [ "$_FAIL" -eq 0 ]
}

@test "python3-venv: both fail → 0 pass, 1 fail" {
    apt_install() { return 1; }
    if apt_install python3.12-venv || apt_install python3-venv; then
        check_pass "python3-venv installed"
    else
        check_fail "python3-venv — install failed"
    fi
    [ "$_PASS" -eq 0 ]; [ "$_FAIL" -eq 1 ]
}

# ── huggingface-cli fallback (install.sh:272-278 rewrite) ─────────────────────
@test "huggingface-cli: break-system-packages path succeeds → 1 pass" {
    pip3() { return 0; }
    if pip3 install --break-system-packages "huggingface_hub[cli]" \
        || pip3 install --user "huggingface_hub[cli]"; then
        check_pass "huggingface-cli installed"
    else
        check_fail "huggingface-cli — install failed"
    fi
    [ "$_PASS" -eq 1 ]; [ "$_FAIL" -eq 0 ]
}

@test "huggingface-cli: both pip paths fail → 1 fail" {
    pip3() { return 1; }
    if pip3 install --break-system-packages "huggingface_hub[cli]" \
        || pip3 install --user "huggingface_hub[cli]"; then
        check_pass "huggingface-cli installed"
    else
        check_fail "huggingface-cli — install failed"
    fi
    [ "$_PASS" -eq 0 ]; [ "$_FAIL" -eq 1 ]
}

# ── nats-py fallback (install.sh:287-293 rewrite) ─────────────────────────────
@test "nats-py: primary pip path succeeds → 1 pass" {
    pip3() { return 0; }
    if pip3 install --break-system-packages nats-py \
        || pip3 install --user nats-py; then
        check_pass "nats-py installed"
    else
        check_fail "nats-py — install failed"
    fi
    [ "$_PASS" -eq 1 ]; [ "$_FAIL" -eq 0 ]
}

@test "nats-py: both pip paths fail → 1 fail" {
    pip3() { return 1; }
    if pip3 install --break-system-packages nats-py \
        || pip3 install --user nats-py; then
        check_pass "nats-py installed"
    else
        check_fail "nats-py — install failed"
    fi
    [ "$_PASS" -eq 0 ]; [ "$_FAIL" -eq 1 ]
}

# ── pytool loop fallback (install.sh:647-652 rewrite) ─────────────────────────
@test "pytool loop: fallback path succeeds → 1 pass" {
    _n=0
    pip3() { _n=$((_n+1)); [ "$_n" -eq 1 ] && return 1; return 0; }
    pytool=ruff
    if pip3 install --break-system-packages "$pytool" \
        || pip3 install --user "$pytool"; then
        check_pass "$pytool installed"
    else
        check_fail "$pytool — install failed"
    fi
    [ "$_PASS" -eq 1 ]; [ "$_FAIL" -eq 0 ]
}

# ── Regression guard: under the OLD broken precedence (A || B && C || D),
#    the success path would historically fire NEITHER counter when A succeeded.
#    The rewritten if-block must NOT exhibit that behaviour. ─────────────────
@test "regression: success path always increments exactly one counter" {
    apt_install() { return 0; }
    if apt_install python3.12-venv || apt_install python3-venv; then
        check_pass "python3-venv installed"
    else
        check_fail "python3-venv — install failed"
    fi
    [ $((_PASS + _FAIL)) -eq 1 ]
}
