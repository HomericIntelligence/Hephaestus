#!/usr/bin/env bash
# run_automation_loop.sh
#
# Clones all HomericIntelligence repos (excluding Odysseus), then runs
# 6-phase pipeline: plan → review-plans → implement → review-PRs → address-review → drive-green
# in a loop N times for every repo that has open issues.
# drive-green only runs on final loop. Up to PARALLEL_REPOS repos are processed concurrently.
#
# Usage:
#   ./scripts/run_automation_loop.sh [--dry-run] [--loops N] [--max-workers N] [--parallel-repos N]
#
# Options:
#   --dry-run           Pass --dry-run to plan, implement, and review (default: off)
#   --loops N           Number of loop iterations (default: 5)
#   --max-workers N     Parallel workers per repo per phase (default: 3)
#   --parallel-repos N  Repos processed in parallel (default: 3)

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve script location so PYTHONPATH always points at ProjectHephaestus
# regardless of where this script is invoked from.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEPHAESTUS_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$HEPHAESTUS_DIR/.pixi/envs/default/bin/python"
export PYTHONPATH="$HEPHAESTUS_DIR${PYTHONPATH:+:$PYTHONPATH}"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DRY_RUN=0
LOOPS=5
MAX_WORKERS=3
PARALLEL_REPOS=3
PROJECTS_DIR="$HOME/Projects"
ORG="HomericIntelligence"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)         DRY_RUN=1; shift ;;
    --loops)           LOOPS="$2"; shift 2 ;;
    --max-workers)     MAX_WORKERS="$2"; shift 2 ;;
    --parallel-repos)  PARALLEL_REPOS="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

DRY_RUN_FLAGS=""
if [[ "$DRY_RUN" -eq 1 ]]; then
  DRY_RUN_FLAGS="--dry-run"
fi

# ---------------------------------------------------------------------------
# Repos to process (all non-archived, excluding Odysseus)
# ---------------------------------------------------------------------------
mapfile -t REPOS < <(
  gh repo list "$ORG" \
    --json name,isArchived \
    --limit 50 \
    --jq '[.[] | select(.isArchived == false and .name != "Odysseus") | .name] | sort[]'
)

if [[ ${#REPOS[@]} -eq 0 ]]; then
  echo "ERROR: No repos returned from gh repo list — possible GitHub API rate limit." >&2
  echo "Check: gh api rate_limit" >&2
  exit 1
fi

echo "Repos to process: ${REPOS[*]}"
echo "Loops: $LOOPS | Max workers: $MAX_WORKERS | Parallel repos: $PARALLEL_REPOS | Dry run: $DRY_RUN"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ---------------------------------------------------------------------------
# Step 1: Clone any repos that don't exist locally
# ---------------------------------------------------------------------------
echo ""
echo "▶ Cloning missing repos..."
for repo in "${REPOS[@]}"; do
  dir="$PROJECTS_DIR/$repo"
  if [[ ! -d "$dir/.git" ]]; then
    echo "  Cloning $ORG/$repo -> $dir"
    gh repo clone "$ORG/$repo" "$dir"
  else
    echo "  Already cloned: $repo"
  fi
done

# ---------------------------------------------------------------------------
# process_repo: 6-phase pipeline for a single repo.
# Runs in a subshell so multiple repos can execute in parallel.
# ---------------------------------------------------------------------------
process_repo() {
  local repo="$1"
  local loop="$2"
  local dir="$PROJECTS_DIR/$repo"

  echo ""
  echo "── $repo ──────────────────────────────────────────────────────"

  # Fetch open issue numbers (up to 1000)
  local -a ISSUE_NUMBERS
  mapfile -t ISSUE_NUMBERS < <(
    gh issue list --repo "$ORG/$repo" \
      --state open \
      --limit 1000 \
      --json number \
      --jq '.[].number' 2>/dev/null || true
  )

  if [[ ${#ISSUE_NUMBERS[@]} -eq 0 ]]; then
    echo "  [$repo] No open issues — skipping"
    return 0
  fi

  echo "  [$repo] Open issues (${#ISSUE_NUMBERS[@]}): ${ISSUE_NUMBERS[*]}"

  # Rebase main before starting work
  echo "  [$repo] Rebasing main..."
  git -C "$dir" fetch origin --quiet
  git -C "$dir" rebase origin/main --quiet 2>/dev/null \
    || git -C "$dir" reset --hard origin/main --quiet 2>/dev/null \
    || echo "  [$repo] Warning: could not rebase, continuing anyway"

  # On loop 3+, suppress follow-up issue filing to avoid noise
  local FOLLOW_UP_FLAG=""
  if [[ "$loop" -ge 3 ]]; then
    FOLLOW_UP_FLAG="--no-follow-up"
  fi

  # --- Phase 1: Plan ---
  echo "  [$repo] Planning issues..."
  (
    cd "$dir"
    "$PYTHON" "$SCRIPT_DIR/plan_issues.py" \
      --issues "${ISSUE_NUMBERS[@]}" \
      -v \
      $DRY_RUN_FLAGS \
      || echo "  [$repo] Warning: plan-issues exited non-zero (loop $loop)"
  )

  # --- Phase 2: Review Plans ---
  echo "  [$repo] Reviewing plans..."
  (
    cd "$dir"
    "$PYTHON" "$SCRIPT_DIR/review_plans.py" \
      --issues "${ISSUE_NUMBERS[@]}" \
      --max-workers "$MAX_WORKERS" \
      -v \
      $DRY_RUN_FLAGS \
      || echo "  [$repo] Warning: review-plans exited non-zero (loop $loop)"
  )

  # --- Phase 3: Implement ---
  echo "  [$repo] Implementing issues..."
  (
    cd "$dir"
    "$PYTHON" "$SCRIPT_DIR/implement_issues.py" \
      --issues "${ISSUE_NUMBERS[@]}" \
      --max-workers "$MAX_WORKERS" \
      --no-ui \
      -v \
      $FOLLOW_UP_FLAG \
      $DRY_RUN_FLAGS \
      || echo "  [$repo] Warning: implement-issues exited non-zero (loop $loop)"
  )

  # --- Phase 4: Review PRs (inline comments) ---
  echo "  [$repo] Reviewing PRs..."
  (
    cd "$dir"
    "$PYTHON" "$SCRIPT_DIR/review_issues.py" \
      --issues "${ISSUE_NUMBERS[@]}" \
      --max-workers "$MAX_WORKERS" \
      --no-ui \
      -v \
      $DRY_RUN_FLAGS \
      || echo "  [$repo] Warning: review-issues exited non-zero (loop $loop)"
  )

  # --- Phase 5: Address Review Comments ---
  echo "  [$repo] Addressing review comments..."
  (
    cd "$dir"
    "$PYTHON" "$SCRIPT_DIR/address_review.py" \
      --issues "${ISSUE_NUMBERS[@]}" \
      --max-workers "$MAX_WORKERS" \
      --no-ui \
      -v \
      $DRY_RUN_FLAGS \
      || echo "  [$repo] Warning: address-review exited non-zero (loop $loop)"
  )

  # --- Phase 6: Drive PRs to Green CI (final loop only) ---
  if [[ "$loop" -eq "$LOOPS" ]]; then
    echo "  [$repo] Driving PRs to green CI..."
    (
      cd "$dir"
      "$PYTHON" "$SCRIPT_DIR/drive_prs_green.py" \
        --issues "${ISSUE_NUMBERS[@]}" \
        --max-workers "$MAX_WORKERS" \
        --no-ui \
        -v \
        $DRY_RUN_FLAGS \
        || echo "  [$repo] Warning: drive-prs-green exited non-zero (loop $loop)"
    )
  fi
}

# ---------------------------------------------------------------------------
# Step 2: Main loop — PARALLEL_REPOS repos processed concurrently
# ---------------------------------------------------------------------------
for (( loop=1; loop<=LOOPS; loop++ )); do
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "▶ LOOP $loop / $LOOPS"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  active_pids=()
  for repo in "${REPOS[@]}"; do
    process_repo "$repo" "$loop" &
    active_pids+=($!)

    if [[ ${#active_pids[@]} -ge "$PARALLEL_REPOS" ]]; then
      # Wait for the oldest job to finish before launching the next
      wait "${active_pids[0]}"
      active_pids=("${active_pids[@]:1}")
    fi
  done

  # Drain any remaining background jobs
  for pid in "${active_pids[@]}"; do
    wait "$pid"
  done

  echo ""
  echo "  Loop $loop complete."
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✓ All $LOOPS loops complete across ${#REPOS[@]} repos."
