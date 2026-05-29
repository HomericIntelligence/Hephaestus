#!/usr/bin/env bash
# run_automation_loop.sh
#
# LEGACY bash driver — superseded by the `hephaestus-automation-loop` console
# script (hephaestus.automation.loop_runner), which is now the canonical
# pipeline driver. This script is kept for reference / fallback only.
#
# Clones all HomericIntelligence repos (excluding Odysseus), then runs the
# 3-stage pipeline: plan → implement → drive-green in a loop N times for every
# repo. Plan-review folded into `plan` (the planner owns its review loop) and
# PR-review + address-review folded into `implement` (the implementer absorbs
# them in-loop), so they are no longer standalone phases.
# drive-green only runs on the final loop. Up to PARALLEL_REPOS repos are processed concurrently.
#
# Issue discovery:
#   - plan, implement: each stage's Python entrypoint auto-discovers via
#     gh_list_open_issues(), so issues opened mid-loop by an earlier stage are
#     picked up by these stages automatically.
#   - drive-green: this entrypoint declares --issues as REQUIRED (no
#     auto-discovery). The orchestrator discovers open issues once per repo per
#     loop (`gh issue list`) and passes them via --issues. Repos with zero open
#     issues skip this stage cleanly with a log line of the form
#     "  [$repo] stage N/3 NAME SKIP (no open issues)" emitted on stderr.
#
# Diagnostic stream routing:
#   - stderr: every phase START / done / SKIP / Warning banner (operator
#     diagnostics — grep `2>` redirect for the full phase lifecycle).
#   - stdout: phase-binary output (Python tool stdout passes through).
#
# Usage:
#   ./scripts/run_automation_loop.sh [options]
#
# Options:
#   --dry-run                 Pass --dry-run to every phase (default: off)
#   --loops N                 Number of loop iterations (default: 5)
#   --max-workers N           Parallel workers per repo per phase (default: 3)
#   --parallel-repos N        Repos processed in parallel (default: 3)
#   --phases LIST             Comma-separated subset of stages to run.
#                             Valid: plan,implement,drive-green
#                             Default: all three.
#                             Normal gates still apply (drive-green only on final loop).
#                             A safety check warns (stderr) if `drive-green` is
#                             selected without `implement` (drive-green would
#                             run against PRs not touched this invocation).
#                             Use --allow-unsafe-phase-order to silence it.
#   --allow-unsafe-phase-order
#                             Suppress the dependency-ordering warning emitted
#                             when --phases selects `drive-green` without
#                             `implement`. Intended for operators deliberately
#                             running a partial pipeline (e.g. driving existing
#                             PRs green in a later invocation).
#   --planner-model MODEL     Set HEPH_PLANNER_MODEL for child processes
#   --reviewer-model MODEL    Set HEPH_REVIEWER_MODEL (covers the planner's in-loop
#                             plan-review and the implementer's in-loop PR-review)
#   --implementer-model MODEL Set HEPH_IMPLEMENTER_MODEL (covers implement incl. the
#                             in-loop PR-review + address-review steps, and ci-driver
#                             fresh sessions; --resume sites correctly omit --model)
#   -h, --help                Show this help and exit

set -euo pipefail

# Job control: each backgrounded `process_repo` runs in its own process group,
# so we can SIGTERM the whole subtree (repo subshell → Python phase →
# claude/gh descendants) by signalling the negative pgid.
set -m

usage() {
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------
# Cleanup: when interrupted (Ctrl-C, SIGTERM, SIGHUP), kill every backgrounded
# repo subtree. We only fire on real signals — a normal exit (including
# `exit 1` from bad args) skips the kill because there are no children yet
# OR the main loop already drained them with `wait`.
# ---------------------------------------------------------------------------
ACTIVE_PIDS=()
cleanup_on_signal() {
  local sig="$1"
  echo "" >&2
  echo "▶ Interrupted ($sig). Stopping ${#ACTIVE_PIDS[@]} background repo job(s)..." >&2
  trap - INT TERM HUP    # disarm to prevent re-entry mid-cleanup

  # Documented set +e/set -e bracket: signalling a process group that has
  # already exited returns ESRCH (kill exit 1). That is the expected outcome
  # during teardown — we do not want it to abort the cleanup loop. We also
  # suppress kill's stderr so "No such process" doesn't clutter the log when
  # children exit between our two passes.
  set +e
  for pid in "${ACTIVE_PIDS[@]}"; do
    # Negative pid = entire process group (the `set -m` job).
    # SIGTERM first; SIGKILL after a short grace.
    kill -TERM -"$pid" 2>/dev/null
  done
  sleep 2
  for pid in "${ACTIVE_PIDS[@]}"; do
    kill -KILL -"$pid" 2>/dev/null
  done
  set -e
  exit 130
}
trap 'cleanup_on_signal INT'  INT
trap 'cleanup_on_signal TERM' TERM
trap 'cleanup_on_signal HUP'  HUP

# ---------------------------------------------------------------------------
# Resolve the installed entry-point binaries from the Hephaestus pixi env.
# We invoke these directly (not `python -m hephaestus.automation.*`) so that
# CWD-shadowing — when `cd $repo` puts a stray `hephaestus/` directory at
# sys.path[0] — cannot mask the real package.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEPHAESTUS_DIR="$(dirname "$SCRIPT_DIR")"
PLAN_BIN="$(cd "$HEPHAESTUS_DIR" && pixi run which hephaestus-plan-issues)"
IMPL_BIN="$(cd "$HEPHAESTUS_DIR" && pixi run which hephaestus-implement-issues)"
PYTHON="$HEPHAESTUS_DIR/.pixi/envs/default/bin/python"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DRY_RUN=0
LOOPS=5
MAX_WORKERS=3
PARALLEL_REPOS=1
PROJECTS_DIR="$HOME/Projects"
ORG="HomericIntelligence"

ALL_PHASES="plan,implement,drive-green"
PHASES="$ALL_PHASES"
ALLOW_UNSAFE_PHASE_ORDER=0

PLANNER_MODEL=""
REVIEWER_MODEL=""
IMPLEMENTER_MODEL=""

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)            usage; exit 0 ;;
    --dry-run)            DRY_RUN=1; shift ;;
    --loops)              LOOPS="$2"; shift 2 ;;
    --max-workers)        MAX_WORKERS="$2"; shift 2 ;;
    --parallel-repos)     PARALLEL_REPOS="$2"; shift 2 ;;
    --phases)             PHASES="$2"; shift 2 ;;
    --allow-unsafe-phase-order) ALLOW_UNSAFE_PHASE_ORDER=1; shift ;;
    --planner-model)      PLANNER_MODEL="$2"; shift 2 ;;
    --reviewer-model)     REVIEWER_MODEL="$2"; shift 2 ;;
    --implementer-model)  IMPLEMENTER_MODEL="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; echo "Run with --help for usage." >&2; exit 1 ;;
  esac
done

# Validate --phases against the canonical list. Typos must fail loudly.
IFS=',' read -r -a PHASE_ARRAY <<< "$PHASES"
for p in "${PHASE_ARRAY[@]}"; do
  case ",${ALL_PHASES}," in
    *",${p},"*) ;;
    *) echo "Unknown phase: $p (valid: $ALL_PHASES)" >&2; exit 1 ;;
  esac
done

phase_enabled() {
  case ",${PHASES}," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Dependency-ordering validation for --phases.
#
# Phase typos are caught above. Plan-review and PR-review/address-review are no
# longer separate stages (the planner owns its review loop; the implementer
# absorbs PR-review + thread-addressing in-loop), so the only cross-stage
# ordering hazard left is running `drive-green` without `implement` — it would
# poll PRs not produced this invocation. The warning goes to stderr so it
# interleaves with the rest of the stage-lifecycle log. Operators who
# deliberately want a partial pipeline (e.g. drive existing PRs green in a
# later invocation) pass `--allow-unsafe-phase-order` to suppress it.
#
# This is intentionally NOT a hard error: the orchestrator is a tool for
# operators who know the pipeline, and forcing them to retype an opt-out for
# every partial run would itself violate POLA. A loud stderr warning is the
# right ergonomic balance.
# ---------------------------------------------------------------------------
phase_in_list() {
  case ",${PHASES}," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}
if [[ "$ALLOW_UNSAFE_PHASE_ORDER" -eq 0 ]]; then
  if phase_in_list drive-green && ! phase_in_list implement; then
    echo "WARNING: --phases includes 'drive-green' but not 'implement'; drive-green will run against PRs not touched this invocation (pass --allow-unsafe-phase-order to silence)" >&2
  fi
fi

# ---------------------------------------------------------------------------
# Phase banner helpers.
#
# Every stage block in process_repo wraps its work in a phase_start/phase_done
# pair so the operator can see (a) which of the 3 stages actually ran for each
# repo, (b) what exit code each stage returned, and (c) how long it took. A
# silent abort inside process_repo (e.g. set -e tripping on an unguarded
# infrastructure command) will be obvious from a missing `done` line.
#
# Stream-routing contract (POLA): all phase-lifecycle diagnostics go to STDERR
# so an operator running `script.sh 2>err.log >out.log` finds the full phase
# lifecycle (START, done, SKIP, Warning) in `err.log`, leaving stdout for the
# phase binaries' own output. The lone exception is `date +%s` inside
# phase_start, which MUST stay on stdout because callers capture it via
# `t0=$(phase_start …)`.
#
# phase_start prints the START banner to stderr and writes the epoch
# timestamp to stdout, so callers do `t0=$(phase_start …)`.
#
# phase_done clamps elapsed-time arithmetic with a bash ternary
# `$(( now >= t0 ? now - t0 : 0 ))` so an NTP step that moves the wall clock
# backwards between phase_start and phase_done cannot produce a negative
# `done in -Xs` reading. The ternary is a standard bash arithmetic-context
# operator (man bash, ARITHMETIC EVALUATION): `cond ? a : b` evaluates `a`
# when `cond` is non-zero, `b` otherwise.
# ---------------------------------------------------------------------------
phase_start() {
  local repo="$1" idx="$2" name="$3"
  echo "  [$repo] stage $idx/3 $name START" >&2
  date +%s
}
phase_done() {
  local repo="$1" idx="$2" name="$3" t0="$4" status="${5:-0}"
  local now elapsed
  now=$(date +%s)
  elapsed=$(( now >= t0 ? now - t0 : 0 ))
  echo "  [$repo] stage $idx/3 $name done in ${elapsed}s (rc=$status)" >&2
}

# Forward model selections via env vars to all child processes.
# claude_models.py honours these and falls back to its own defaults if unset.
[[ -n "$PLANNER_MODEL" ]]     && export HEPH_PLANNER_MODEL="$PLANNER_MODEL"
[[ -n "$REVIEWER_MODEL" ]]    && export HEPH_REVIEWER_MODEL="$REVIEWER_MODEL"
[[ -n "$IMPLEMENTER_MODEL" ]] && export HEPH_IMPLEMENTER_MODEL="$IMPLEMENTER_MODEL"

# Array form (rather than a string) so `"${DRY_RUN_FLAGS[@]}"` expands to
# zero argv elements in the off case under `set -u`, matching the
# `ISSUE_ARGS` pattern established in PR #543.
DRY_RUN_FLAGS=()
if [[ "$DRY_RUN" -eq 1 ]]; then
  DRY_RUN_FLAGS=(--dry-run)
fi

# ---------------------------------------------------------------------------
# Repos to process (all non-archived, excluding Odysseus),
# sorted ascending by open-issue count so smallest backlogs run first.
# ---------------------------------------------------------------------------
mapfile -t CANDIDATE_REPOS < <(
  gh repo list "$ORG" \
    --json name,isArchived \
    --limit 50 \
    --jq '.[] | select(.isArchived == false and .name != "Odysseus") | .name'
)

if [[ ${#CANDIDATE_REPOS[@]} -eq 0 ]]; then
  echo "ERROR: No repos returned from gh repo list — possible GitHub API rate limit." >&2
  echo "Check: gh api rate_limit" >&2
  exit 1
fi

echo "Counting open issues per repo to order by smallest backlog..."
# shellcheck source=scripts/shell/lib/repo_ordering.sh
source "$SCRIPT_DIR/shell/lib/repo_ordering.sh"
mapfile -t REPOS < <(sort_repos_by_open_count "$ORG" "${CANDIDATE_REPOS[@]}")

if [[ ${#REPOS[@]} -eq 0 ]]; then
  echo "ERROR: Failed to enumerate repos after issue-count sort." >&2
  exit 1
fi

# Preflight: every phase posts comments / creates PRs, so a token without
# write scope fails after long delays mid-run. Skip on dry-run since no writes
# happen then.
preflight_token_scopes() {
  local first_repo="${REPOS[0]}"
  local probe_err
  if ! probe_err=$(gh api -H "Accept: application/vnd.github+json" \
        "/repos/$ORG/$first_repo" --jq '.permissions' 2>&1); then
    cat >&2 <<EOF
ERROR: \`gh\` cannot read $ORG/$first_repo with the current token.

  $probe_err

  Required scopes for this script:
    - Classic PAT:   repo  (full)             — covers issue:write + pr:write
    - Fine-grained:  Issues:        Read & Write
                     Pull requests: Read & Write
                     Contents:      Read & Write   (if pushes are needed)

  How to fix:
    1. Check which token gh is using:  gh auth status
    2. If GITHUB_TOKEN is set in your env, it overrides gh's stored creds.
       Either:
         a) unset GITHUB_TOKEN  (lets gh use its own login), or
         b) regenerate the PAT with the scopes above:
            https://github.com/settings/tokens
    3. Re-run with:  GITHUB_TOKEN= $(basename "${BASH_SOURCE[0]}") …
       (the leading \`GITHUB_TOKEN=\` blanks the env var for one command)
EOF
    exit 1
  fi
  if [[ "$probe_err" == "null" ]] || [[ "$probe_err" == "{}" ]]; then
    echo "WARNING: token permissions on $ORG/$first_repo are empty; PR/issue writes will fail." >&2
  fi
}
[[ "$DRY_RUN" -eq 0 ]] && preflight_token_scopes

echo "Repos to process: ${REPOS[*]}"
echo "Loops: $LOOPS | Max workers: $MAX_WORKERS | Parallel repos: $PARALLEL_REPOS | Dry run: $DRY_RUN"
echo "Phases: $PHASES"
echo "Models: planner=${HEPH_PLANNER_MODEL:-<default>} reviewer=${HEPH_REVIEWER_MODEL:-<default>} implementer=${HEPH_IMPLEMENTER_MODEL:-<default>}"
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
# process_repo: 3-stage pipeline for a single repo.
# Runs in a subshell so multiple repos can execute in parallel.
# The plan and implement entrypoints auto-discover open issues via
# gh_list_open_issues(); drive-green is passed the discovered list explicitly.
# ---------------------------------------------------------------------------
process_repo() {
  local repo="$1"
  local loop="$2"
  local dir="$PROJECTS_DIR/$repo"

  # Defang errexit for the function body. Each stage below captures `rc=$?`
  # explicitly and logs a structured warning on non-zero — that is the
  # authoritative error handler. An unguarded infrastructure command
  # (`git fetch`, mapfile + process substitution under `set -m`, a `local`
  # assignment whose RHS happens to be non-zero) must NOT silently abort
  # the function and cause later stages to be skipped.
  #
  # Root cause of the original silent abort: bisected in #557.
  # The triggering command was `git -C "$dir" fetch origin --quiet` (pre-fix
  # scripts/run_automation_loop.sh:256, now line 376 with a `|| echo Warning…`
  # guard added by Bundle C / PR #570). When `$dir` referenced a clone whose
  # `origin` remote was absent or unreachable, git exited 128 ("'origin' does
  # not appear to be a git repository"). Under `set -euo pipefail` this
  # aborted process_repo before the implement stage's banner could fire,
  # matching the observed log signature: `Planning complete` (planner exited 0)
  # immediately followed by `Warning: repo job pid=… exited non-zero
  # (continuing)` and zero output for the remaining stages.
  #
  # Reproducer: tests/shell/scripts/test_run_automation_loop.bats includes
  # the `later stages still run when the plan stage exits non-zero` regression
  # test; the original bisect harness lived at build/bisect_repro.sh (not
  # committed — see PR description in #557 closure).
  #
  # The broad `set +e` here is defense-in-depth — Bundle C tightened the
  # specific offender (`git fetch`) but other unguarded infrastructure
  # commands (e.g. a future `gh api ...` probe) could exhibit the same
  # failure mode. Keeping `set +e` with explicit per-stage `rc=$?` capture
  # is the POLA choice: future maintainers can add new stages without
  # rediscovering the errexit landmine.
  #
  # The `trap 'set -e' RETURN` below is defensive for hypothetical future
  # synchronous call sites. It is currently redundant given the always-
  # backgrounded invocation at the main loop (`process_repo "$repo" "$loop" &`,
  # ~line 506): the `&` forks a subshell, so any shell-option mutation here
  # cannot propagate to the parent. Keeping the trap costs nothing and
  # documents the intended contract should a future maintainer call
  # process_repo synchronously.
  set +e
  trap 'set -e' RETURN

  # any_failure tracks whether any phase returned non-zero. process_repo
  # returns this value so the outer `wait` can detect hard crashes
  # (SIGSEGV=139, OOM=137) that the per-phase Warning lines would otherwise
  # bury inside the merged log stream.
  local rc t0 any_failure=0

  echo ""
  echo "── $repo ──────────────────────────────────────────────────────"

  # Rebase main before starting work
  echo "  [$repo] Rebasing main..."
  git -C "$dir" fetch origin --quiet \
    || echo "  [$repo] Warning: git fetch failed (continuing with stale refs)" >&2
  git -C "$dir" rebase origin/main --quiet 2>/dev/null \
    || git -C "$dir" reset --hard origin/main --quiet 2>/dev/null \
    || echo "  [$repo] Warning: could not rebase, continuing anyway" >&2

  # Capture the trunk SHA once per repo loop iteration. Every child phase
  # reads $HEPH_TRUNK_GITHASH to build deterministic Claude session IDs
  # (see hephaestus.automation.session_naming). When trunk advances on the
  # next iteration a fresh session family opens automatically.
  HEPH_TRUNK_GITHASH=$(git -C "$dir" rev-parse --short=7 HEAD 2>/dev/null || echo "unknown")
  export HEPH_TRUNK_GITHASH
  echo "  [$repo] trunk=$HEPH_TRUNK_GITHASH (loop $loop)"

  # On loop 3+, suppress follow-up issue filing to avoid noise.
  # Array form so `"${FOLLOW_UP_FLAG[@]}"` expands to zero elements when off.
  local FOLLOW_UP_FLAG=()
  if [[ "$loop" -ge 3 ]]; then
    FOLLOW_UP_FLAG=(--no-follow-up)
  fi

  # Discover this repo's open issues once per loop iteration. The drive-green
  # stage (ci_driver) declares --issues as REQUIRED in argparse and has no
  # auto-discovery — invoking it without --issues fails with exit code 2, which
  # the `|| echo Warning…` clause below would silently swallow. The `plan` and
  # `implement` stages intentionally do NOT consume this list — both
  # auto-discover via gh_list_open_issues() inside their main(), which lets them
  # pick up issues opened mid-loop by an earlier stage.
  local OPEN_ISSUES=()
  mapfile -t OPEN_ISSUES < <(
    gh issue list --repo "$ORG/$repo" --state open --limit 200 \
      --json number --jq '.[].number' 2>/dev/null
  )
  local ISSUE_ARGS=()
  if [[ ${#OPEN_ISSUES[@]} -gt 0 ]]; then
    ISSUE_ARGS=(--issues "${OPEN_ISSUES[@]}")
  fi

  # --- Stage 1: Plan (planner owns its plan-review loop internally) ---
  if phase_enabled plan; then
    t0=$(phase_start "$repo" 1 plan)
    (
      cd "$dir" || exit 1
      "$PLAN_BIN" \
        -v \
        "${DRY_RUN_FLAGS[@]}"
    )
    rc=$?
    if [[ $rc -ne 0 ]]; then
      echo "  [$repo] Warning: plan-issues exited rc=$rc (loop $loop)" >&2
      any_failure=1
    fi
    phase_done "$repo" 1 plan "$t0" "$rc"
  else
    echo "  [$repo] stage 1/3 plan SKIP (disabled by --phases)" >&2
  fi

  # --- Stage 2: Implement (absorbs PR-review + address-review in-loop) ---
  if phase_enabled implement; then
    t0=$(phase_start "$repo" 2 implement)
    (
      cd "$dir" || exit 1
      "$IMPL_BIN" \
        --max-workers "$MAX_WORKERS" \
        --no-ui \
        -v \
        "${FOLLOW_UP_FLAG[@]}" \
        "${DRY_RUN_FLAGS[@]}"
    )
    rc=$?
    if [[ $rc -ne 0 ]]; then
      echo "  [$repo] Warning: implement-issues exited rc=$rc (loop $loop)" >&2
      any_failure=1
    fi
    phase_done "$repo" 2 implement "$t0" "$rc"
  else
    echo "  [$repo] stage 2/3 implement SKIP (disabled by --phases)" >&2
  fi

  # --- Stage 3: Drive PRs to Green CI (final loop only) ---
  if ! phase_enabled drive-green; then
    echo "  [$repo] stage 3/3 drive-green SKIP (disabled by --phases)" >&2
  elif [[ "$loop" -ne "$LOOPS" ]]; then
    echo "  [$repo] stage 3/3 drive-green SKIP (not final loop)" >&2
  elif [[ ${#OPEN_ISSUES[@]} -eq 0 ]]; then
    echo "  [$repo] stage 3/3 drive-green SKIP (no open issues)" >&2
  else
    t0=$(phase_start "$repo" 3 drive-green)
    (
      cd "$dir" || exit 1
      # Defence-in-depth: ci_driver also checks these envs and refuses to run
      # unless HEPH_LOOP_INDEX == HEPH_TOTAL_LOOPS or --force-run is given.
      HEPH_LOOP_INDEX="$loop" HEPH_TOTAL_LOOPS="$LOOPS" \
      "$PYTHON" "$SCRIPT_DIR/drive_prs_green.py" \
        "${ISSUE_ARGS[@]}" \
        --max-workers "$MAX_WORKERS" \
        --no-ui \
        -v \
        "${DRY_RUN_FLAGS[@]}"
    )
    rc=$?
    if [[ $rc -ne 0 ]]; then
      echo "  [$repo] Warning: drive-prs-green exited rc=$rc (loop $loop)" >&2
      any_failure=1
    fi
    phase_done "$repo" 3 drive-green "$t0" "$rc"
  fi

  # Return non-zero if ANY phase tripped its rc!=0 branch above. The outer
  # `wait` (main loop) then propagates this so CI / monitoring see a hard
  # crash (SIGSEGV=139, OOM=137, plain Python exception=1). Per-phase
  # Warning lines + phase_done banners are still the authoritative log;
  # this return code is the at-a-glance health bit for the whole repo.
  return $any_failure
}

# ---------------------------------------------------------------------------
# Step 2: Main loop — PARALLEL_REPOS repos processed concurrently
# ---------------------------------------------------------------------------
for (( loop=1; loop<=LOOPS; loop++ )); do
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "▶ LOOP $loop / $LOOPS"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  ACTIVE_PIDS=()
  for repo in "${REPOS[@]}"; do
    process_repo "$repo" "$loop" &
    ACTIVE_PIDS+=($!)

    if [[ ${#ACTIVE_PIDS[@]} -ge "$PARALLEL_REPOS" ]]; then
      # process_repo() returns 0 only when EVERY phase returned 0; non-zero
      # means at least one phase failed (per-phase Warning + phase_done
      # banners above pinpoint which one). A non-zero from `wait` here is
      # the operator's at-a-glance signal that this repo's loop iteration
      # was not fully clean — drill into the phase banners for details.
      if ! wait "${ACTIVE_PIDS[0]}"; then
        echo "  Note: wait() non-zero for pid=${ACTIVE_PIDS[0]} — check phase banners above" >&2
      fi
      ACTIVE_PIDS=("${ACTIVE_PIDS[@]:1}")
    fi
  done

  # Drain any remaining background jobs
  for pid in "${ACTIVE_PIDS[@]}"; do
    if ! wait "$pid"; then
      echo "  Note: wait() non-zero for pid=$pid — check phase banners above" >&2
    fi
  done
  ACTIVE_PIDS=()

  echo ""
  echo "  Loop $loop complete."

  # Inter-loop GraphQL budget probe. Runs unless HEPHAESTUS_RATE_GUARD=0.
  # When the remaining budget would be exhausted by the next loop (default
  # threshold 200 calls), sleep until the upstream reset rather than burning
  # the next loop on retry storms.
  if [[ "${HEPHAESTUS_RATE_GUARD:-1}" != "0" && "$loop" -lt "$LOOPS" ]]; then
    threshold="${HEPHAESTUS_RATE_GUARD_THRESHOLD:-200}"
    remaining=$(gh api rate_limit --jq '.resources.graphql.remaining' 2>/dev/null || echo "")
    reset=$(gh api rate_limit --jq '.resources.graphql.reset' 2>/dev/null || echo "")
    if [[ -n "$remaining" && -n "$reset" && "$remaining" -lt "$threshold" ]]; then
      now=$(date +%s)
      wait=$((reset - now + 5))
      if (( wait > 0 )); then
        echo "  Rate budget low (${remaining}/${threshold} GraphQL remaining); sleeping ${wait}s until reset"
        sleep "$wait"
      fi
    fi
  fi
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✓ All $LOOPS loops complete across ${#REPOS[@]} repos."
