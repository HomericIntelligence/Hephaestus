#!/usr/bin/env bats
# Tests for scripts/shell/install_hooks.sh — the git-hook installer.
#
# Regression guard for #687: the script used to copy hooks from a nonexistent
# scripts/hooks/ directory, so under `set -euo pipefail` it aborted on a fresh
# clone. It now delegates to the `pre-commit` framework (the repo standard) and
# must:
#   * install BOTH the pre-commit and pre-push hook types,
#   * succeed on a fresh clone (no scripts/hooks/ dir required),
#   * fail clearly when run outside a git repo or with no pre-commit available.
#
# Strategy: copy the production script into a sandbox laid out like the real
# repo (so its `../..` REPO_ROOT resolution lands on a throwaway git repo), put
# a fake `pre-commit` on PATH that records its args, and assert on the recording.

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" rev-parse --show-toplevel)"
    SRC_SCRIPT="${REPO_ROOT}/scripts/shell/install_hooks.sh"
    [ -f "$SRC_SCRIPT" ]

    TEST_TMPDIR="$(mktemp -d)"

    # Sandbox repo with the same scripts/shell/ layout the script assumes.
    SANDBOX_REPO="$TEST_TMPDIR/repo"
    mkdir -p "$SANDBOX_REPO/scripts/shell"
    cp "$SRC_SCRIPT" "$SANDBOX_REPO/scripts/shell/install_hooks.sh"
    SCRIPT="$SANDBOX_REPO/scripts/shell/install_hooks.sh"

    # PATH stub dir holding the fake pre-commit (and, where needed, uv).
    # It is the ONLY directory on PATH during a run, so the host's real
    # pre-commit/uv never leak in. Symlink the few external binaries the
    # script genuinely needs (git + the coreutils it shells out to) so a
    # hermetic PATH still works.
    STUB_DIR="$TEST_TMPDIR/stubs"
    mkdir -p "$STUB_DIR"
    PC_LOG="$TEST_TMPDIR/pre-commit.log"
    for bin in git dirname basename cd pwd env bash; do
        local real
        real="$(command -v "$bin" 2>/dev/null || true)"
        [ -n "$real" ] && ln -sf "$real" "$STUB_DIR/$bin"
    done
}

teardown() {
    [ -d "${TEST_TMPDIR:-}" ] && rm -rf "$TEST_TMPDIR"
}

# Initialise the sandbox as a real git repo.
init_git() {
    git -C "$SANDBOX_REPO" init -q
}

# Install a fake `pre-commit` that appends each invocation to PC_LOG.
make_fake_precommit() {
    cat > "$STUB_DIR/pre-commit" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$PC_LOG"
exit 0
EOF
    chmod +x "$STUB_DIR/pre-commit"
}

# A hermetic PATH: ONLY the stub dir. Real pre-commit/uv are deliberately
# excluded so the script's tool-resolution logic is exercised against the
# fakes (or their absence) and nothing else.
stub_path() {
    echo "$STUB_DIR"
}


@test "install_hooks.sh installs pre-commit AND pre-push hook types" {
    init_git
    make_fake_precommit
    run env PATH="$(stub_path)" bash "$SCRIPT"
    [ "$status" -eq 0 ]
    # Both hook types must be requested via pre-commit.
    grep -qx "install" "$PC_LOG"
    grep -qx "install --hook-type pre-push" "$PC_LOG"
}


@test "install_hooks.sh succeeds on a fresh clone with no scripts/hooks dir" {
    # The sandbox deliberately has no scripts/hooks/ directory.
    [ ! -d "$SANDBOX_REPO/scripts/hooks" ]
    init_git
    make_fake_precommit
    run env PATH="$(stub_path)" bash "$SCRIPT"
    [ "$status" -eq 0 ]
}


@test "install_hooks.sh fails clearly outside a git repository" {
    # No `git init` — the sandbox is just a plain directory.
    make_fake_precommit
    run env PATH="$(stub_path)" bash "$SCRIPT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"not inside a git repository"* ]]
}


@test "install_hooks.sh fails clearly when pre-commit is unavailable" {
    init_git
    # No fake pre-commit and no uv on PATH.
    run env PATH="$(stub_path)" bash "$SCRIPT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"pre-commit not found"* ]]
}


@test "install_hooks.sh falls back to 'uv run pre-commit' when only uv exists" {
    init_git
    # Provide uv (which forwards to our recorder) but NO direct pre-commit.
    cat > "$STUB_DIR/uv" <<EOF
#!/usr/bin/env bash
# Expect: uv run pre-commit <args...>
shift 2   # drop "run" and "pre-commit"
echo "\$*" >> "$PC_LOG"
exit 0
EOF
    chmod +x "$STUB_DIR/uv"
    run env PATH="$(stub_path)" bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -qx "install" "$PC_LOG"
    grep -qx "install --hook-type pre-push" "$PC_LOG"
}
