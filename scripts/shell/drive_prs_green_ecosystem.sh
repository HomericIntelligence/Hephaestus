#!/usr/bin/env bash
# drive-prs-green-ecosystem.sh
#
# Drive failing PRs to green CI across every non-fork, non-archived repo in
# the HomericIntelligence org, with structured per-repo logging.
#
# Workaround for the structural bugs filed as #818–#821 in
# HomericIntelligence/Hephaestus:
#   - the CI driver has no --repos / --org flag (this script loops)
#   - issue-driven discovery only: PRs without `Closes #<open-issue>` are invisible
#
# Pre-reqs:
#   - gh authenticated (`gh auth status`)
#   - Repos cloned under $PROJECTS_ROOT (default /home/mvillmow/Projects)
#   - pixi env initialized in Hephaestus
#
# Usage:
#   ~/drive-prs-green-ecosystem.sh                     # real run, logs to ~/drive-prs-green-logs/<utc-ts>/
#   ~/drive-prs-green-ecosystem.sh --log-dir DIR       # write logs under DIR/<utc-ts>/
#   ~/drive-prs-green-ecosystem.sh --dry-run           # forward --dry-run to driver
#   ~/drive-prs-green-ecosystem.sh --org HomericIntelligence
#   ~/drive-prs-green-ecosystem.sh --project-root /path/to/Hephaestus
#   ~/drive-prs-green-ecosystem.sh --gh-bin hephaestus-gh
#   ~/drive-prs-green-ecosystem.sh --gh-global-rate 5  # tune shared gh throttle
#   ~/drive-prs-green-ecosystem.sh --gh-global-burst 20
#   ~/drive-prs-green-ecosystem.sh -- --max-workers 5  # everything after `--` goes to driver
#
# All non-script-flag args before `--` are forwarded to the CI driver as well.
#
# Log directory layout (run is anchored at <log-dir>/<UTC-timestamp>/):
#   _run.meta.json        — top-level run metadata (host, git rev, env, args)
#   _summary.log          — human-readable narration (what the script printed)
#   _summary.json         — machine-readable summary (repos: driven/failed/skipped + paths)
#   <repo>/repo.log       — driver stdout + stderr for that repo
#   <repo>/repo.meta.json — per-repo metadata (issues, exit code, durations)
#   <repo>/discovery.log  — `gh issue list` invocation + raw output
#
# Each log file starts with a self-describing banner so a future
# claude session can analyze the directory without external context.

set -uo pipefail   # no -e: keep iterating across per-repo failures

# ── Flag parsing ─────────────────────────────────────────────────────────────
LOG_ROOT_DEFAULT="$HOME/drive-prs-green-logs"
LOG_ROOT="${DRIVE_GREEN_LOG_ROOT:-$LOG_ROOT_DEFAULT}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ORG="HomericIntelligence"
GH_BIN="hephaestus-gh"
DRIVER_ARGS=()
GH_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --log-dir)
      if [[ $# -lt 2 ]]; then echo "ERROR: --log-dir requires a path" >&2; exit 2; fi
      LOG_ROOT="$2"; shift 2
      ;;
    --log-dir=*)
      LOG_ROOT="${1#--log-dir=}"; shift
      ;;
    --org)
      if [[ $# -lt 2 ]]; then echo "ERROR: --org requires a value" >&2; exit 2; fi
      ORG="$2"; shift 2
      ;;
    --org=*)
      ORG="${1#--org=}"; shift
      ;;
    --project-root)
      if [[ $# -lt 2 ]]; then echo "ERROR: --project-root requires a path" >&2; exit 2; fi
      PROJECT_ROOT="$2"; shift 2
      ;;
    --project-root=*)
      PROJECT_ROOT="${1#--project-root=}"; shift
      ;;
    --gh-bin)
      if [[ $# -lt 2 ]]; then echo "ERROR: --gh-bin requires a value" >&2; exit 2; fi
      GH_BIN="$2"; shift 2
      ;;
    --gh-bin=*)
      GH_BIN="${1#--gh-bin=}"; shift
      ;;
    --gh-global-rate|--gh-global-burst)
      if [[ $# -lt 2 ]]; then echo "ERROR: $1 requires a value" >&2; exit 2; fi
      GH_ARGS+=("$1" "$2"); shift 2
      ;;
    --gh-global-rate=*|--gh-global-burst=*)
      GH_ARGS+=("${1%%=*}" "${1#*=}"); shift
      ;;
    --)
      shift; DRIVER_ARGS+=("$@"); break
      ;;
    -h|--help)
      awk '
        /^# Usage:/ {show=1}
        show && /^# Log directory layout/ {exit}
        show {sub(/^# ?/, ""); print}
      ' "$0"
      exit 0
      ;;
    *)
      DRIVER_ARGS+=("$1"); shift
      ;;
  esac
done

# ── Environment ──────────────────────────────────────────────────────────────
PROJECTS_ROOT="${PROJECTS_ROOT:-/home/mvillmow/Projects}"
# Canonical entry point; the legacy wrapper was removed.
DRIVER_MODULE="hephaestus.automation.ci_driver"
SCRIPT_PATH="$(readlink -f "$0")"
SCRIPT_VERSION="$(cd "$(dirname "$SCRIPT_PATH")" && md5sum "$(basename "$SCRIPT_PATH")" 2>/dev/null | cut -d' ' -f1 || echo unknown)"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$LOG_ROOT/$RUN_ID"
mkdir -p "$RUN_DIR"

SUMMARY_LOG="$RUN_DIR/_summary.log"
SUMMARY_JSON="$RUN_DIR/_summary.json"
META_JSON="$RUN_DIR/_run.meta.json"

hephaestus_gh() {
  "$GH_BIN" "${GH_ARGS[@]}" "$@"
}

# ── Sanity checks ────────────────────────────────────────────────────────────
if [[ ! -f "$PROJECT_ROOT/pyproject.toml" ]]; then
  echo "ERROR: Hephaestus root not found at $PROJECT_ROOT" >&2
  exit 1
fi
if ! hephaestus_gh --version >/dev/null 2>&1; then
  echo "ERROR: GitHub CLI wrapper unavailable: $GH_BIN" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq not found on PATH (needed for summary JSON)" >&2
  exit 1
fi

# ── Helpers ──────────────────────────────────────────────────────────────────
ts_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log()    { printf '%s\n' "$*" | tee -a "$SUMMARY_LOG"; }
note()   { printf '[%s] %s\n' "$(ts_utc)" "$*" | tee -a "$SUMMARY_LOG"; }

write_run_meta() {
  # Top-level metadata so an analysis agent has the full provenance.
  local hep_rev
  hep_rev="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

  jq -n \
    --arg run_id "$RUN_ID" \
    --arg started_at "$(ts_utc)" \
    --arg host "$(hostname)" \
    --arg user "${USER:-unknown}" \
    --arg org "$ORG" \
    --arg projects_root "$PROJECTS_ROOT" \
    --arg project_root "$PROJECT_ROOT" \
    --arg hephaestus_rev "$hep_rev" \
    --arg driver_module "$DRIVER_MODULE" \
    --arg log_root "$LOG_ROOT" \
    --arg script_path "$SCRIPT_PATH" \
    --arg script_md5 "$SCRIPT_VERSION" \
    --argjson driver_args "$( ((${#DRIVER_ARGS[@]})) && printf '%s\n' "${DRIVER_ARGS[@]}" | jq -R . | jq -s . || echo '[]' )" \
    '{
      run_id: $run_id,
      started_at: $started_at,
      host: $host,
      user: $user,
      org: $org,
      projects_root: $projects_root,
      project_root: $project_root,
      hephaestus_rev: $hephaestus_rev,
      driver_module: $driver_module,
      log_root: $log_root,
      script_path: $script_path,
      script_md5: $script_md5,
      driver_args: $driver_args
    }' > "$META_JSON"
}

write_repo_meta() {
  local repo="$1" status="$2" rc="$3" started="$4" ended="$5"
  local issues_json="$6" repo_dir="$7" log_path="$8"
  local started_epoch="" ended_epoch="" duration=""

  # Compute duration in shell rather than jq — strptime portability is fragile
  # and bash `date -d` is simpler. Falls back to null if either ts is empty.
  if [[ -n "$started" && -n "$ended" ]]; then
    started_epoch="$(date -d "$started" +%s 2>/dev/null || echo)"
    ended_epoch="$(date -d "$ended" +%s 2>/dev/null || echo)"
    if [[ -n "$started_epoch" && -n "$ended_epoch" ]]; then
      duration="$(( ended_epoch - started_epoch ))"
    fi
  fi

  jq -n \
    --arg repo "$repo" \
    --arg status "$status" \
    --arg rc "$rc" \
    --arg started "$started" \
    --arg ended "$ended" \
    --arg duration "$duration" \
    --arg repo_dir "$repo_dir" \
    --arg log_path "$log_path" \
    --argjson issues "$issues_json" \
    '{
      repo: $repo,
      status: $status,
      rc: ($rc | tonumber? // null),
      started_at: $started,
      ended_at: $ended,
      duration_seconds: ($duration | tonumber? // null),
      issues: $issues,
      repo_dir: $repo_dir,
      log_path: $log_path
    }' > "$RUN_DIR/$repo/repo.meta.json"
}

banner() {
  # Self-describing banner at the top of each log file so an analyzing
  # agent can read just one file and understand its context.
  local file="$1" repo="$2" kind="$3" issues="$4"
  {
    printf '═══════════════════════════════════════════════════════════════\n'
    printf 'drive-prs-green-ecosystem.sh — %s log\n' "$kind"
    printf '═══════════════════════════════════════════════════════════════\n'
    printf 'run_id      : %s\n' "$RUN_ID"
    printf 'repo        : %s\n' "$repo"
    printf 'started_at  : %s\n' "$(ts_utc)"
    printf 'host        : %s\n' "$(hostname)"
    printf 'org         : %s\n' "$ORG"
    printf 'log_kind    : %s\n' "$kind"
    [[ -n "$issues" ]] && printf 'issues      : %s\n' "$issues"
    printf 'driver_args : %s\n' "${DRIVER_ARGS[*]:-<none>}"
    printf 'run_meta    : %s\n' "$META_JSON"
    printf '═══════════════════════════════════════════════════════════════\n\n'
  } > "$file"
}

# ── Begin ────────────────────────────────────────────────────────────────────
write_run_meta
note "▶ drive-prs-green-ecosystem run $RUN_ID"
note "  log_dir=$RUN_DIR  meta=$META_JSON"
note "  org=$ORG  projects_root=$PROJECTS_ROOT  project_root=$PROJECT_ROOT"
note "  driver_args=${DRIVER_ARGS[*]:-<none>}"

note "▶ Enumerating non-fork, non-archived repos in $ORG ..."
mapfile -t REPOS < <(
  hephaestus_gh repo list "$ORG" --no-archived --limit 200 --json name,isFork \
    --jq '.[] | select(.isFork == false) | .name'
)
if [[ ${#REPOS[@]} -eq 0 ]]; then
  note "ERROR: no repos found in $ORG (gh auth issue?)"
  exit 1
fi
note "  ${#REPOS[@]} repo(s): ${REPOS[*]}"
log ""

# ── Per-repo loop ────────────────────────────────────────────────────────────
DRIVEN=()
FAILED=()
SKIPPED_NOT_CLONED=()
SKIPPED_NO_ISSUES=()
declare -A REPO_ISSUES_JSON
declare -A REPO_START
declare -A REPO_END

for REPO in "${REPOS[@]}"; do
  REPO_DIR="$PROJECTS_ROOT/$REPO"
  REPO_LOG_DIR="$RUN_DIR/$REPO"
  REPO_LOG="$REPO_LOG_DIR/repo.log"
  DISCOVERY_LOG="$REPO_LOG_DIR/discovery.log"
  mkdir -p "$REPO_LOG_DIR"

  REPO_START["$REPO"]="$(ts_utc)"
  banner "$DISCOVERY_LOG" "$REPO" "discovery" ""

  if [[ ! -d "$REPO_DIR/.git" ]]; then
    note "── SKIP $REPO (not cloned at $REPO_DIR) ──"
    SKIPPED_NOT_CLONED+=("$REPO")
    REPO_END["$REPO"]="$(ts_utc)"
    REPO_ISSUES_JSON["$REPO"]="[]"
    {
      printf 'reason : not cloned at %s\n' "$REPO_DIR"
    } >> "$DISCOVERY_LOG"
    write_repo_meta "$REPO" "skipped-not-cloned" "" "${REPO_START[$REPO]}" "${REPO_END[$REPO]}" "[]" "$REPO_DIR" "$REPO_LOG"
    continue
  fi

  # ── Discover open issues for this repo (unbounded via --paginate) ────────
  # ``gh issue list --limit N`` is a HARD cap, not a page size (#848 mirrors
  # the PR-list bug fixed in #839). ``gh api --paginate`` walks Link headers
  # so we enumerate every open issue regardless of count.
  {
    printf 'command : gh api --paginate /repos/%s/%s/issues?state=open&per_page=100 (issues only)\n' "$ORG" "$REPO"
    printf 'started : %s\n\n' "$(ts_utc)"
  } >> "$DISCOVERY_LOG"

  # ``/repos/.../issues`` returns BOTH issues and PRs; filter via the
  # ``pull_request`` field which only PRs carry.
  mapfile -t ISSUES < <(
    hephaestus_gh api --paginate "/repos/$ORG/$REPO/issues?state=open&per_page=100" \
      --jq '.[] | select(.pull_request | not) | .number' 2>>"$DISCOVERY_LOG"
  )
  printf '\nfound   : %d open issue(s): %s\n' "${#ISSUES[@]}" "${ISSUES[*]:-<none>}" >> "$DISCOVERY_LOG"
  if ((${#ISSUES[@]})); then
    REPO_ISSUES_JSON["$REPO"]="$(printf '%s\n' "${ISSUES[@]}" | jq -R '. | tonumber? // empty' | jq -s .)"
  else
    REPO_ISSUES_JSON["$REPO"]="[]"
  fi

  # ── Discover open bot-authored PRs (#848) ─────────────────────────────────
  # The driver itself unions bot PRs into its work set (CIDriverOptions
  # .include_bot_prs, default True). We still probe here so a repo with
  # zero open issues but non-zero open bot PRs is correctly classified as
  # "drive" rather than "skip — no issues" in the per-repo log.
  BOT_PR_COUNT=$(
    hephaestus_gh api --paginate "/repos/$ORG/$REPO/pulls?state=open&per_page=100" \
      --jq '[.[] | select(.user.type == "Bot")] | length' 2>>"$DISCOVERY_LOG" || echo 0
  )
  BOT_PR_COUNT="${BOT_PR_COUNT:-0}"
  printf 'bot PRs : %s\n' "$BOT_PR_COUNT" >> "$DISCOVERY_LOG"

  if [[ ${#ISSUES[@]} -eq 0 && "$BOT_PR_COUNT" -eq 0 ]]; then
    note "── SKIP $REPO (no open issues, no open bot PRs) ──"
    SKIPPED_NO_ISSUES+=("$REPO")
    REPO_END["$REPO"]="$(ts_utc)"
    write_repo_meta "$REPO" "skipped-no-issues" "" "${REPO_START[$REPO]}" "${REPO_END[$REPO]}" "${REPO_ISSUES_JSON[$REPO]}" "$REPO_DIR" "$REPO_LOG"
    continue
  fi

  note "── DRIVING $REPO  (${#ISSUES[@]} issues, $BOT_PR_COUNT bot PR(s) → $REPO_LOG) ──"
  banner "$REPO_LOG" "$REPO" "driver" "${ISSUES[*]}"
  {
    printf 'repo_dir : %s\n' "$REPO_DIR"
    printf 'driver   : python -m %s\n' "$DRIVER_MODULE"
    printf 'issues   : %s\n' "${ISSUES[*]:-<none>}"
    printf 'bot PRs  : %s\n' "$BOT_PR_COUNT"
    printf '\n══════ driver stdout+stderr below ══════\n\n'
  } >> "$REPO_LOG"

  # python -u: line-buffered so `tail -f` shows live progress. When the
  # issue list is empty but bot PRs exist, invoke WITHOUT --issues so the
  # driver's bot-PR enumeration is the sole work source (#848).
  if (
    cd "$REPO_DIR"
    if ((${#ISSUES[@]})); then
      pixi run --manifest-path "$PROJECT_ROOT/pixi.toml" python -u \
        -m "$DRIVER_MODULE" \
        --issues "${ISSUES[@]}" \
        --no-ui \
        "${GH_ARGS[@]}" \
        "${DRIVER_ARGS[@]}"
    else
      pixi run --manifest-path "$PROJECT_ROOT/pixi.toml" python -u \
        -m "$DRIVER_MODULE" \
        --no-ui \
        "${GH_ARGS[@]}" \
        "${DRIVER_ARGS[@]}"
    fi
  ) >> "$REPO_LOG" 2>&1; then
    rc=0
    note "  ✓ $REPO complete"
    DRIVEN+=("$REPO")
  else
    rc=$?
    note "  !! $REPO FAILED rc=$rc"
    FAILED+=("$REPO")
  fi
  REPO_END["$REPO"]="$(ts_utc)"

  printf '\n══════ driver exit code: %s @ %s ══════\n' "$rc" "$(ts_utc)" >> "$REPO_LOG"

  status_label="driven"; [[ "$rc" -ne 0 ]] && status_label="failed"
  write_repo_meta "$REPO" "$status_label" "$rc" "${REPO_START[$REPO]}" "${REPO_END[$REPO]}" "${REPO_ISSUES_JSON[$REPO]}" "$REPO_DIR" "$REPO_LOG"
done

# ── Summary ──────────────────────────────────────────────────────────────────
log ""
log "═══════════════════════════════════════════════════════════════"
log "Summary (run $RUN_ID)"
log "═══════════════════════════════════════════════════════════════"
log "  Driven:           ${DRIVEN[*]:-<none>}"
log "  Failed:           ${FAILED[*]:-<none>}"
log "  Skip (no clone):  ${SKIPPED_NOT_CLONED[*]:-<none>}"
log "  Skip (no issues): ${SKIPPED_NO_ISSUES[*]:-<none>}"
log ""
log "Per-repo logs in $RUN_DIR"

# Machine-readable summary for downstream analysis.
arr_to_json() {
  # Convert a bash array (passed by reference name) to a JSON array.
  local -n _arr="$1"
  if ((${#_arr[@]})); then
    printf '%s\n' "${_arr[@]}" | jq -R . | jq -s .
  else
    echo '[]'
  fi
}

jq -n \
  --arg run_id "$RUN_ID" \
  --arg ended_at "$(ts_utc)" \
  --arg log_dir "$RUN_DIR" \
  --arg meta_path "$META_JSON" \
  --argjson driven "$(arr_to_json DRIVEN)" \
  --argjson failed "$(arr_to_json FAILED)" \
  --argjson skipped_not_cloned "$(arr_to_json SKIPPED_NOT_CLONED)" \
  --argjson skipped_no_issues "$(arr_to_json SKIPPED_NO_ISSUES)" \
  '{
    run_id: $run_id,
    ended_at: $ended_at,
    log_dir: $log_dir,
    meta_path: $meta_path,
    counts: {
      driven: ($driven | length),
      failed: ($failed | length),
      skipped_not_cloned: ($skipped_not_cloned | length),
      skipped_no_issues: ($skipped_no_issues | length)
    },
    driven: $driven,
    failed: $failed,
    skipped_not_cloned: $skipped_not_cloned,
    skipped_no_issues: $skipped_no_issues
  }' > "$SUMMARY_JSON"

log ""
log "Machine-readable summary: $SUMMARY_JSON"
log "To hand off for analysis, share:  $RUN_DIR"

if [[ ${#FAILED[@]} -gt 0 ]]; then
  exit 1
fi
