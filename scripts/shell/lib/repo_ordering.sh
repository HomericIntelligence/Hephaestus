#!/usr/bin/env bash
# scripts/shell/lib/repo_ordering.sh
#
# Sourceable helpers for ordering HomericIntelligence repos by open-issue
# count. Extracted from scripts/run_automation_loop.sh so the sort logic
# is independently testable under BATS without invoking the full automation
# driver.
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/repo_ordering.sh"
#   mapfile -t REPOS < <(sort_repos_by_open_count "$ORG" "${CANDIDATE_REPOS[@]}")
#
# Safe to source multiple times — guarded by REPO_ORDERING_LOADED.

[[ -n "${REPO_ORDERING_LOADED:-}" ]] && return 0
REPO_ORDERING_LOADED=1


# count_open_issues <org> <repo>
#
# Print the number of open issues for ``<org>/<repo>`` on stdout. Returns 0
# unconditionally — when ``gh issue list`` fails (rate limit, missing token,
# repo gone) the count defaults to ``0`` so the caller's sort pipeline still
# orders that repo first rather than aborting.
count_open_issues() {
  local org="$1"
  local repo="$2"
  local count
  count=$(gh issue list --repo "$org/$repo" --state open --limit 1000 \
            --json number --jq 'length' 2>/dev/null) || count=0
  # Empty stdout (no JSON, jq returned nothing) also collapses to 0.
  printf '%s\n' "${count:-0}"
}


# sort_repos_by_open_count <org> <repo>...
#
# Print the input repos one per line, ordered ascending by their open-issue
# count. Ties preserve the input order (sort is stable on the secondary key).
# Used by run_automation_loop.sh to drain the smallest backlogs first so
# parallel workers don't all pile onto the busiest repo.
sort_repos_by_open_count() {
  local org="$1"
  shift
  local repo count
  for repo in "$@"; do
    count=$(count_open_issues "$org" "$repo")
    # `sort -s -n -k1,1` keys on the leading numeric column and is stable
    # for equal counts; `cut -f2` strips the count and yields the bare name.
    printf '%d\t%s\n' "$count" "$repo"
  done | sort -s -n -k1,1 | cut -f2
}
