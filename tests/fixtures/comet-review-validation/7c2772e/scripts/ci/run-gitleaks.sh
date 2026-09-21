#!/usr/bin/env bash
# Purpose: Scan the complete checkout ancestry with the reviewed Gitleaks release.
# Input: RUNNER_TEMP is a private temporary directory from the CI runner.
# Output: Validated finding metadata and fixed failure categories.
# Prerequisites: The checkout contains full Git history and the host can download HTTPS files.
# Prerequisites: Python 3 is installed.
# Failure: Keep scan failures. Reject invalid reports and failed cleanup.
set -uo pipefail

version="8.24.3"
archive="gitleaks_8.24.3_linux_x64.tar.gz"
archive_sha256="9991e0b2903da4c8f6122b5c3186448b927a5da4deef1fe45271c3793f4ee29c"
install_root=""

cleanup() {
  local prior_status=$? cleanup_status=0 command_status
  trap - EXIT INT TERM
  if [[ -n "${install_root}" ]]; then
    if rm -f -- "${install_root}/${archive}" "${install_root}/gitleaks" \
      "${install_root}/report.json" "${install_root}/metadata.json" >/dev/null 2>&1; then
      :
    else
      cleanup_status=$?
    fi
    if rmdir -- "${install_root}" >/dev/null 2>&1; then
      :
    else
      command_status=$?
      if [[ ${cleanup_status} -eq 0 ]]; then
        cleanup_status=${command_status}
      fi
    fi
  fi
  if [[ ${cleanup_status} -ne 0 ]]; then
    printf '%s\n' 'comet-gitleaks: cleanup-failed' >&2
    if [[ ${prior_status} -eq 0 ]]; then
      prior_status=${cleanup_status}
    fi
  fi
  exit "${prior_status}"
}

interrupted() {
  trap - INT TERM
  printf '%s\n' 'comet-gitleaks: interrupted' >&2
  exit "$1"
}
trap cleanup EXIT
trap 'interrupted 130' INT
trap 'interrupted 143' TERM

if [[ $# -ne 0 || -z "${RUNNER_TEMP:-}" || ! -d "${RUNNER_TEMP}" ]]; then
  printf '%s\n' 'comet-gitleaks: setup-failed' >&2
  exit 2
fi
if umask 077 >/dev/null 2>&1; then
  :
else
  status=$?
  printf '%s\n' 'comet-gitleaks: setup-failed' >&2
  exit "${status}"
fi
if repository_root="$(
  { cd "${BASH_SOURCE[0]%/*}/../.." && pwd; } 2>/dev/null
)"; then
  :
else
  status=$?
  printf '%s\n' 'comet-gitleaks: setup-failed' >&2
  exit "${status}"
fi
for git_variable in "${!GIT_@}"; do
  if unset "${git_variable}" 2>/dev/null; then
    :
  else
    status=$?
    printf '%s\n' 'comet-gitleaks: setup-failed' >&2
    exit "${status}"
  fi
done
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_NO_REPLACE_OBJECTS=1 GIT_GRAFT_FILE=/dev/null GIT_NO_LAZY_FETCH=1
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=advice.graftFileDeprecated
export GIT_CONFIG_VALUE_0=false

if scan_commit="$(git -C "${repository_root}" rev-parse --verify 'HEAD^{commit}' 2>/dev/null)" &&
  shallow="$(git -C "${repository_root}" rev-parse --is-shallow-repository 2>/dev/null)"; then
  :
else
  status=$?
  printf '%s\n' 'comet-gitleaks: setup-failed' >&2
  exit "${status}"
fi
if [[ ! "${scan_commit}" =~ ^[0-9a-f]{40}$ || "${shallow}" != false ]]; then
  printf '%s\n' 'comet-gitleaks: setup-failed' >&2
  exit 2
fi
if git -C "${repository_root}" config --get-regexp \
  '^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$' >/dev/null 2>&1; then
  printf '%s\n' 'comet-gitleaks: setup-failed' >&2
  exit 2
else
  status=$?
  if [[ ${status} -ne 1 ]]; then
    printf '%s\n' 'comet-gitleaks: setup-failed' >&2
    exit "${status}"
  fi
fi
if git -C "${repository_root}" -c core.commitGraph=false \
  rev-list --objects --missing=error "${scan_commit}" >/dev/null 2>&1; then
  :
else
  status=$?
  printf '%s\n' 'comet-gitleaks: setup-failed' >&2
  exit "${status}"
fi
if runner_temp="$( { cd "${RUNNER_TEMP}" && pwd; } 2>/dev/null)"; then
  :
else
  status=$?
  printf '%s\n' 'comet-gitleaks: setup-failed' >&2
  exit "${status}"
fi
if install_root="$(mktemp -d "${runner_temp}/comet-gitleaks.XXXXXX" 2>/dev/null)"; then
  :
else
  status=$?
  install_root=""
  printf '%s\n' 'comet-gitleaks: setup-failed' >&2
  exit "${status}"
fi
archive_path="${install_root}/${archive}"

if {
  curl -sSfL -o "${archive_path}" \
    "https://github.com/gitleaks/gitleaks/releases/download/v${version}/${archive}" &&
    printf '%s  %s\n' "${archive_sha256}" "${archive_path}" | sha256sum -c - &&
    tar -xzf "${archive_path}" -C "${install_root}" gitleaks &&
    cd "${repository_root}"
} >/dev/null 2>&1; then
  :
else
  status=$?
  printf '%s\n' 'comet-gitleaks: setup-failed' >&2
  exit "${status}"
fi

if "${install_root}/gitleaks" git --redact --no-banner --exit-code 1 \
  --log-opts "--full-history ${scan_commit}" \
  --report-format json --report-path "${install_root}/report.json" . >/dev/null 2>&1; then
  scan_status=0
else
  scan_status=$?
fi
if [[ ${scan_status} -ne 0 && ${scan_status} -ne 1 ]]; then
  printf '%s\n' 'comet-gitleaks: scan-failed' >&2
  exit "${scan_status}"
fi
if python3 "${repository_root}/scripts/ci/gitleaks-diagnostics.py" \
  "${install_root}/report.json" "${install_root}/metadata.json" "${scan_status}" \
  >/dev/null 2>&1; then
  if cat -- "${install_root}/metadata.json" 2>/dev/null; then
    exit "${scan_status}"
  fi
fi
printf '%s\n' 'comet-gitleaks: report-invalid' >&2
if [[ ${scan_status} -ne 0 ]]; then
  exit "${scan_status}"
fi
exit 2
