#!/usr/bin/env bash
# Purpose: Scan the complete repository history with the reviewed Gitleaks release.
# Input: RUNNER_TEMP is a private temporary directory from the CI runner.
# Output: Gitleaks reports findings for the checked-out repository.
# Prerequisites: The checkout contains full Git history and the host can download HTTPS files.
# Failure: A download, checksum, extraction, or scan error stops the script.
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"

version="8.24.3"
archive="gitleaks_8.24.3_linux_x64.tar.gz"
archive_sha256="9991e0b2903da4c8f6122b5c3186448b927a5da4deef1fe45271c3793f4ee29c"
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
install_root="$(mktemp -d "${RUNNER_TEMP}/comet-gitleaks.XXXXXX")"
archive_path="${install_root}/${archive}"

cleanup() {
  rm -f -- "${archive_path}" "${install_root}/gitleaks"
  rmdir "${install_root}" 2>/dev/null || true
}
trap cleanup EXIT

curl -sSfL \
  -o "${archive_path}" \
  "https://github.com/gitleaks/gitleaks/releases/download/v${version}/${archive}"
printf '%s  %s\n' "${archive_sha256}" "${archive_path}" | sha256sum -c -
tar -xzf "${archive_path}" -C "${install_root}" gitleaks

cd "${repository_root}"
"${install_root}/gitleaks" git --redact --no-banner --exit-code 1 .
