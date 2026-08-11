#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: cleanup-stale-worktrees.sh [--dry-run] [--trunk BRANCH]

Interactively remove clean linked worktrees whose local branch is already
merged into the trunk branch or whose leading issue number is closed.
EOF
}

dry_run=0
trunk="main"

while (($#)); do
  case "$1" in
    --dry-run)
      dry_run=1
      shift
      ;;
    --trunk)
      if (($# < 2)); then
        echo "cleanup-stale-worktrees.sh: --trunk requires a branch name" >&2
        exit 2
      fi
      trunk="$2"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "cleanup-stale-worktrees.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

repo_root="$(git rev-parse --show-toplevel)"
stale_count=0
current_path=""
current_branch=""
current_locked=0

issue_is_closed() {
  local issue="$1"
  local state

  if ! state="$(gh issue view "$issue" --json state --jq .state 2>/dev/null)"; then
    return 1
  fi
  [[ "$state" == "CLOSED" ]]
}

branch_is_merged() {
  local branch="$1"

  git merge-base --is-ancestor "$branch" "$trunk" >/dev/null 2>&1
}

worktree_is_dirty() {
  local path="$1"

  [[ -n "$(git -C "$path" status --porcelain)" ]]
}

remove_worktree() {
  local path="$1"
  local branch="$2"

  git worktree remove "$path"
  git branch -d "$branch" >/dev/null 2>&1 || true
}

consider_worktree() {
  local path="$1"
  local branch="$2"
  local locked="$3"
  local issue=""
  local reason=""

  if [[ "$path" == "$repo_root" ]]; then
    return 0
  fi

  if [[ "$branch" =~ ^([0-9]+) ]]; then
    issue="${BASH_REMATCH[1]}"
  fi

  if [[ -n "$issue" ]] && issue_is_closed "$issue"; then
    reason="issue #$issue is closed"
  elif branch_is_merged "$branch"; then
    reason="merged into $trunk"
  else
    return 0
  fi

  stale_count=$((stale_count + 1))

  if worktree_is_dirty "$path"; then
    echo "Skipping dirty worktree $path ($reason)"
    return 0
  fi

  if [[ "$locked" == "1" ]]; then
    echo "Skipping locked worktree $path ($reason)"
    return 0
  fi

  if [[ "$dry_run" == "1" ]]; then
    echo "Would remove stale worktree $path (branch $branch; $reason)"
    return 0
  fi

  read -r -p "Remove stale worktree $path (branch $branch; $reason)? [y/N] " reply
  case "$reply" in
    y | Y)
    remove_worktree "$path" "$branch"
    echo "Removed stale worktree $path"
      ;;
    *)
    echo "Kept worktree $path"
      ;;
  esac
}

flush_record() {
  if [[ -n "$current_path" && -n "$current_branch" ]]; then
    consider_worktree "$current_path" "$current_branch" "$current_locked"
  fi
  current_path=""
  current_branch=""
  current_locked=0
}

while IFS= read -r -d '' field; do
  case "$field" in
    "worktree "*)
      flush_record
      current_path="${field#worktree }"
      ;;
    "branch refs/heads/"*)
      current_branch="${field#branch refs/heads/}"
      ;;
    locked*)
      current_locked=1
      ;;
    "")
      flush_record
      ;;
  esac
done < <(git worktree list --porcelain -z)
flush_record

if [[ "$stale_count" == "0" ]]; then
  echo "No stale worktrees found."
fi
