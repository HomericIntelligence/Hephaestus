#!/usr/bin/env bash
# scripts/shell/lib/install_helpers.sh
#
# Sourceable helper library for HomericIntelligence installer scripts.
# Provides color vars, counters, and purely functional helpers.
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/install_helpers.sh"
#
# Safe to source multiple times — guarded by INSTALL_HELPERS_LOADED.
set -uo pipefail

[[ -n "${INSTALL_HELPERS_LOADED:-}" ]] && return 0
INSTALL_HELPERS_LOADED=1

# ─── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

# ─── Counters ────────────────────────────────────────────────────────────────
_PASS=0; _FAIL=0; _WARN=0; _SKIP=0

# ─── INSTALL default ─────────────────────────────────────────────────────────
INSTALL="${INSTALL:-false}"

# ─── Output helpers ──────────────────────────────────────────────────────────
check_pass() { _PASS=$((_PASS + 1)); echo -e "  ${GREEN}✓${NC} $1"; }
check_fail() { _FAIL=$((_FAIL + 1)); echo -e "  ${RED}✗${NC} $1"; }
check_warn() { _WARN=$((_WARN + 1)); echo -e "  ${YELLOW}⚠${NC} $1"; }
check_skip() { _SKIP=$((_SKIP + 1)); echo -e "  ${DIM}–${NC} $1 ${DIM}(skipped)${NC}"; }
section()    { echo -e "\n${BOLD}${CYAN}$1${NC}"; }

# ─── Utility helpers ─────────────────────────────────────────────────────────
has_cmd()     { command -v "$1" &>/dev/null; }
get_version() {
    # Capture the command's output once, then probe it; this avoids
    # `cmd | grep | head || true` swallowing real exit codes from cmd.
    # Empty result means "no version found".
    local _out
    _out="$("$@" 2>&1)" || _out=''
    printf '%s\n' "$_out" | grep -oP '\d+\.\d+[\.\d]*' | head -1 || printf ''
}

# Compare versions: returns 0 if $1 >= $2
version_gte() { printf '%s\n%s\n' "$2" "$1" | sort -V | head -1 | grep -qF "$2"; }

# Install an apt package if INSTALL=true; return 0 on success
apt_install() {
    local pkg="$1"
    if $INSTALL; then
        echo -e "    ${BLUE}→${NC} Installing $pkg via apt..."
        sudo apt-get install -y "$pkg" >/dev/null 2>&1
        return $?
    fi
    return 1
}

# Install a binary from a GitHub release to ~/.local/bin
# Usage: install_github_binary <owner/repo> <tag> <asset_glob> <binary_name>
install_github_binary() {
    local repo="$1" tag="$2" asset_glob="$3" binary="$4"
    local url
    url=$(curl -fsSL "https://api.github.com/repos/$repo/releases/tags/$tag" \
        | grep "browser_download_url" | grep "$asset_glob" | head -1 \
        | cut -d '"' -f 4)
    if [[ -z "$url" ]]; then
        echo -e "    ${RED}→${NC} Could not resolve download URL for $repo@$tag ($asset_glob)" >&2
        return 1
    fi
    mkdir -p ~/.local/bin
    curl -fsSL "$url" -o "/tmp/$binary" && chmod +x "/tmp/$binary" && mv "/tmp/$binary" ~/.local/bin/
}
