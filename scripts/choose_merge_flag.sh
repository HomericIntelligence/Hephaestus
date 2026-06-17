#!/usr/bin/env bash
# Source this file to get choose_merge_flag().
#
# Usage:
#   . "<path>/scripts/choose_merge_flag.sh"
#   MERGE_FLAG=$(choose_merge_flag [--gh-bin hephaestus-gh] HomericIntelligence/ProjectMnemosyne) || exit 1
#   gh pr merge "$PR" --auto "$MERGE_FLAG" --repo HomericIntelligence/ProjectMnemosyne
#
# Preference order: rebase (linear history) -> squash -> merge commit.
# Exit codes:
#   0  printed flag to stdout
#   1  gh api failed OR repo allows no methods (message on stderr)
#   2  invalid arguments (message on stderr)

hephaestus_tmp_subdir() {
    local component="$1"
    local base
    base="${TMPDIR:-/tmp}/hephaestus-$(id -u)"
    if [[ -e "$base" && ( -L "$base" || ! -d "$base" || ! -O "$base" ) ]]; then
        echo "choose_merge_flag: unsafe temp root: $base" >&2
        return 1
    fi
    mkdir -p "$base/$component" || return 1
    if [[ -L "$base/$component" || ! -O "$base/$component" ]]; then
        echo "choose_merge_flag: unsafe temp directory: $base/$component" >&2
        return 1
    fi
    chmod 700 "$base" "$base/$component" || return 1
    printf '%s\n' "$base/$component"
}

choose_merge_flag() {
    local gh_bin="hephaestus-gh"
    local repo=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --gh-bin)
                if [[ $# -lt 2 ]]; then
                    echo "choose_merge_flag: --gh-bin requires a value" >&2
                    return 2
                fi
                gh_bin="$2"
                shift 2
                ;;
            --gh-bin=*)
                gh_bin="${1#*=}"
                shift
                ;;
            -*)
                echo "choose_merge_flag: unknown option $1" >&2
                return 2
                ;;
            *)
                if [[ -n "$repo" ]]; then
                    echo "choose_merge_flag: unexpected extra argument $1" >&2
                    return 2
                fi
                repo="$1"
                shift
                ;;
        esac
    done
    if [ -z "$repo" ]; then
        echo "choose_merge_flag: missing required argument <owner/repo>" >&2
        return 2
    fi
    local raw flag _err_file _tmp_dir
    _tmp_dir="$(hephaestus_tmp_subdir choose-merge-flag)" || return 1
    _err_file=$(mktemp "$_tmp_dir/gh-api-XXXXXX.err") || return 1
    trap 'rm -f -- "$_err_file"' RETURN
    if ! raw=$("$gh_bin" api "repos/${repo}" 2>"$_err_file"); then
        echo "choose_merge_flag: gh api repos/${repo} failed: $(cat "$_err_file")" >&2
        echo "  (check: $gh_bin auth status; token needs 'repo' scope; repo exists)" >&2
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
