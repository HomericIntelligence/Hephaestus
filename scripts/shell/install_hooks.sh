#!/usr/bin/env bash
# Install this repo's git hooks via the `pre-commit` framework (the repo
# standard — hooks are declared in .pre-commit-config.yaml, NOT copied from a
# scripts/hooks/ directory).
#
# Usage:
#   bash scripts/shell/install_hooks.sh
#   # or, with the project environment already on PATH:
#   pixi run pre-commit install
#
# Hooks installed:
#   pre-commit  — runs the lint/format/security hook suite before every commit
#   pre-push    — runs the same suite at the pre-push stage as a safety net
#
# This is intentionally idempotent and safe on a fresh clone: it shells out to
# `pre-commit install`, which (re)writes .git/hooks/* itself. It does not depend
# on any extra files existing in the working tree.
#
# Exit codes:
#   0 = hooks installed successfully
#   1 = not inside a git repository, or pre-commit is unavailable

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cd "${REPO_ROOT}"

# Verify we're inside a git repository before pre-commit tries to write hooks.
if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "Error: not inside a git repository — clone the repo and re-run from within it." >&2
    exit 1
fi

# Resolve a pre-commit invocation. Prefer a directly-installed `pre-commit`;
# fall back to `pixi run pre-commit` when only the pixi environment has it.
if command -v pre-commit >/dev/null 2>&1; then
    pc() { pre-commit "$@"; }
elif command -v pixi >/dev/null 2>&1; then
    pc() { pixi run pre-commit "$@"; }
else
    echo "Error: pre-commit not found. Install it first:" >&2
    echo "  pixi install   # then re-run this script" >&2
    echo "  # or: pip install pre-commit" >&2
    exit 1
fi

echo "Installing pre-commit hook..."
pc install

echo "Installing pre-push hook..."
pc install --hook-type pre-push

echo ""
echo "Hooks installed. They will run automatically on 'git commit' and 'git push'."
echo "Run the full suite manually with: pixi run pre-commit run --all-files"
