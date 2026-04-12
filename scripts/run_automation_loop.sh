#!/usr/bin/env bash
# run_automation_loop.sh
#
# Clones all HomericIntelligence repos (excluding Odysseus), then runs
# hephaestus-plan-issues + hephaestus-implement-issues in a loop N times
# for every repo that has open issues.
#
# Usage:
#   ./scripts/run_automation_loop.sh [--dry-run] [--loops N] [--max-workers N]
#
# Options:
#   --dry-run       Pass --dry-run to both plan and implement (default: off)
#   --loops N       Number of loop iterations (default: 5)
#   --max-workers N Parallel workers per repo per loop (default: 3)

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve script location so PYTHONPATH always points at ProjectHephaestus
# regardless of where this script is invoked from.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEPHAESTUS_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$(cd "$HEPHAESTUS_DIR" && pixi run which python)"
export PYTHONPATH="$HEPHAESTUS_DIR${PYTHONPATH:+:$PYTHONPATH}"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DRY_RUN=0
LOOPS=5
MAX_WORKERS=3
PROJECTS_DIR="$HOME/Projects"
ORG="HomericIntelligence"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)       DRY_RUN=1; shift ;;
    --loops)         LOOPS="$2"; shift 2 ;;
    --max-workers)   MAX_WORKERS="$2"; shift 2 ;;
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
echo "Loops: $LOOPS | Max workers: $MAX_WORKERS | Dry run: $DRY_RUN"
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
# Step 2: Main loop
# ---------------------------------------------------------------------------
for (( loop=1; loop<=LOOPS; loop++ )); do
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "▶ LOOP $loop / $LOOPS"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  for repo in "${REPOS[@]}"; do
    dir="$PROJECTS_DIR/$repo"

    echo ""
    echo "── $repo ──────────────────────────────────────────────────────"

    # Fetch open issue numbers (up to 1000)
    mapfile -t ISSUE_NUMBERS < <(
      gh issue list --repo "$ORG/$repo" \
        --state open \
        --limit 1000 \
        --json number \
        --jq '.[].number' 2>/dev/null || true
    )

    if [[ ${#ISSUE_NUMBERS[@]} -eq 0 ]]; then
      echo "  No open issues — skipping"
      continue
    fi

    echo "  Open issues (${#ISSUE_NUMBERS[@]}): ${ISSUE_NUMBERS[*]}"

    # Rebase main before starting work
    echo "  Rebasing main..."
    git -C "$dir" fetch origin --quiet
    git -C "$dir" rebase origin/main --quiet 2>/dev/null \
      || git -C "$dir" reset --hard origin/main --quiet 2>/dev/null \
      || echo "  Warning: could not rebase $repo, continuing anyway"

    # On loop 3+, suppress follow-up issue filing to avoid noise
    FOLLOW_UP_FLAG=""
    if [[ "$loop" -ge 3 ]]; then
      FOLLOW_UP_FLAG="--no-follow-up"
    fi

    # Run plan-issues
    echo "  Planning issues..."
    (
      cd "$dir"
      "$PYTHON" -m hephaestus.automation.planner \
        --issues "${ISSUE_NUMBERS[@]}" \
        $DRY_RUN_FLAGS \
        || echo "  Warning: plan-issues exited non-zero for $repo (loop $loop)"
    )

    # Run implement-issues
    echo "  Implementing issues..."
    (
      cd "$dir"
      "$PYTHON" -m hephaestus.automation.implementer \
        --issues "${ISSUE_NUMBERS[@]}" \
        --max-workers "$MAX_WORKERS" \
        --no-ui \
        $FOLLOW_UP_FLAG \
        $DRY_RUN_FLAGS \
        || echo "  Warning: implement-issues exited non-zero for $repo (loop $loop)"
    )
  done

  echo ""
  echo "  Loop $loop complete."
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✓ All $LOOPS loops complete across ${#REPOS[@]} repos."
