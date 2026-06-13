#!/usr/bin/env bash
# Source this file to get choose_merge_flag().
#
# Usage:
#   . "<path>/scripts/choose_merge_flag.sh"
#   MERGE_FLAG=$(choose_merge_flag HomericIntelligence/ProjectMnemosyne) || exit 1
#   gh pr merge "$PR" --auto "$MERGE_FLAG" --repo HomericIntelligence/ProjectMnemosyne
#
# Preference order: rebase (linear history) -> squash -> merge commit.
# Exit codes:
#   0  printed flag to stdout
#   1  gh api failed OR repo allows no methods (message on stderr)
#   2  missing argument (message on stderr)
choose_merge_flag() {
    local repo="$1"
    if [ -z "$repo" ]; then
        echo "choose_merge_flag: missing required argument <owner/repo>" >&2
        return 2
    fi
    local raw flag _err_file
    _err_file=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$_err_file'" RETURN
    if ! raw=$(gh api "repos/${repo}" 2>"$_err_file"); then
        echo "choose_merge_flag: gh api repos/${repo} failed: $(cat "$_err_file")" >&2
        echo "  (check: gh auth status; token needs 'repo' scope; repo exists)" >&2
        return 1
    fi
    flag=$(printf '%s' "$raw" | jq -r '[
        (if .allow_rebase_merge then "--rebase" else empty end),
        (if .allow_squash_merge then "--squash" else empty end),
        (if .allow_merge_commit then "--merge"  else empty end)
    ] | .[0] // ""' 2>/dev/null)
    if [ -z "$flag" ]; then
        echo "choose_merge_flag: target repo ${repo} allows no merge methods" >&2
        return 1
    fi
    printf '%s\n' "$flag"
}
