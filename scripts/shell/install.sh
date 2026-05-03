#!/usr/bin/env bash
# HomericIntelligence Ecosystem Installer
#
# Installs and verifies all dependencies needed to participate in the
# HomericIntelligence distributed agent mesh.
#
# Usage:
#   bash scripts/shell/install.sh             # Check-only mode
#   bash scripts/shell/install.sh --install   # Check + install missing deps
#   bash scripts/shell/install.sh --role worker   # Worker-only dependencies
#   bash scripts/shell/install.sh --role control  # Control-plane deps (C++ build)
#   bash scripts/shell/install.sh --role all      # Everything (default)
#
# Exit codes:
#   0 — all checks passed (or installed successfully)
#   1 — one or more checks failed
#
# shellcheck disable=SC2015  # A && B || C patterns are intentional best-effort installs
set -uo pipefail

# ─── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

# ─── Counters ────────────────────────────────────────────────────────────────
_PASS=0; _FAIL=0; _WARN=0; _SKIP=0

check_pass() { _PASS=$((_PASS + 1)); echo -e "  ${GREEN}✓${NC} $1"; }
check_fail() { _FAIL=$((_FAIL + 1)); echo -e "  ${RED}✗${NC} $1"; }
check_warn() { _WARN=$((_WARN + 1)); echo -e "  ${YELLOW}⚠${NC} $1"; }
check_skip() { _SKIP=$((_SKIP + 1)); echo -e "  ${DIM}–${NC} $1 ${DIM}(skipped)${NC}"; }
section()    { echo -e "\n${BOLD}${CYAN}$1${NC}"; }

# ─── Argument Parsing ─────────────────────────────────────────────────────────
INSTALL=false
ROLE="all"  # all | worker | control

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install)   INSTALL=true; shift ;;
        --role)      ROLE="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: bash scripts/shell/install.sh [--install] [--role worker|control|all]"
            exit 0
            ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

# ─── Helpers ──────────────────────────────────────────────────────────────────
has_cmd()     { command -v "$1" &>/dev/null; }
get_version() { "$@" 2>&1 | head -1 | grep -oP '\d+\.\d+[\.\d]*' | head -1; }

# Compare versions: returns 0 if $1 >= $2
version_gte() { printf '%s\n%s\n' "$2" "$1" | sort -V | head -1 | grep -qF "$2"; }

# Append a line to ~/.bashrc if not already present, then source it in current shell
add_to_bashrc() {
    local line="$1"
    if ! grep -qF "$line" ~/.bashrc 2>/dev/null; then
        echo "$line" >> ~/.bashrc
        echo -e "    ${BLUE}→${NC} Added to ~/.bashrc: $line"
    fi
    # Apply to current shell immediately
    eval "$line" 2>/dev/null || true
}

# Install an apt package if --install was passed; return 0 on success
apt_install() {
    local pkg="$1"
    if $INSTALL; then
        echo -e "    ${BLUE}→${NC} Installing $pkg via apt..."
        sudo apt-get install -y "$pkg" >/dev/null 2>&1
        return $?
    fi
    return 1
}

should_check_worker()  { [[ "$ROLE" == "all" || "$ROLE" == "worker" ]]; }
should_check_control() { [[ "$ROLE" == "all" || "$ROLE" == "control" ]]; }

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

# ─── Banner ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}HomericIntelligence Ecosystem Installer${NC}"
echo "════════════════════════════════════════"
echo -e "  Role: ${CYAN}${ROLE}${NC}    Install: ${CYAN}${INSTALL}${NC}"

# ═════════════════════════════════════════════════════════════════════════════
# Section 0: Homebrew (Linux — all roles)
# Installed first so subsequent sections can fall back to `brew install`
# ═════════════════════════════════════════════════════════════════════════════
section "Homebrew"

# Ensure brew is on PATH even if just installed
_brew_path() {
    for _b in /home/linuxbrew/.linuxbrew/bin/brew ~/.linuxbrew/bin/brew; do
        [[ -x "$_b" ]] && echo "$_b" && return
    done
}

if has_cmd brew; then
    check_pass "brew $(get_version brew --version)"
else
    BREW_BIN=$(_brew_path)
    if [[ -n "$BREW_BIN" ]]; then
        check_pass "brew (found at $BREW_BIN)"
        add_to_bashrc "eval \"\$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)\""
        eval "$("$BREW_BIN" shellenv)" 2>/dev/null || true
    else
        check_fail "brew — NOT FOUND"
        if $INSTALL; then
            echo -e "    ${BLUE}→${NC} Installing Homebrew (Linuxbrew)..."
            NONINTERACTIVE=1 /bin/bash -c \
                "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
                </dev/null >/dev/null 2>&1
            BREW_BIN=$(_brew_path)
            if [[ -n "$BREW_BIN" ]]; then
                check_pass "brew installed"
                # Linuxbrew standard shellenv
                add_to_bashrc "eval \"\$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)\""
                eval "$("$BREW_BIN" shellenv)" 2>/dev/null || true
            else
                check_fail "brew — install failed (see https://brew.sh)"
            fi
        fi
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# Section 1: Core System Tooling (all roles)
# ═════════════════════════════════════════════════════════════════════════════
section "Core Tooling"

for pkg in git curl jq unzip vim universal-ctags libssl-dev libopenblas-dev; do
    if has_cmd "$pkg"; then
        check_pass "$pkg $(get_version "$pkg" --version)"
    else
        check_fail "$pkg — NOT FOUND"
        apt_install "$pkg" && check_pass "$pkg installed" || true
    fi
done

# just (task runner)
if has_cmd just; then
    check_pass "just $(get_version just --version)"
else
    check_fail "just — NOT FOUND"
    if $INSTALL; then
        echo -e "    ${BLUE}→${NC} Installing just..."
        if curl -sSf https://just.systems/install.sh | bash -s -- --to ~/.local/bin >/dev/null 2>&1; then
            check_pass "just installed to ~/.local/bin"
        else
            check_fail "just — install failed (manual: cargo install just)"
        fi
    fi
fi

# gh CLI (GitHub operations, PR creation, issue management)
if has_cmd gh; then
    check_pass "gh $(get_version gh --version)"
else
    check_fail "gh — NOT FOUND (required for PR/issue operations)"
    if $INSTALL; then
        _gh_installed=false
        # Try brew first if available (avoids apt keyring setup)
        if has_cmd brew; then
            echo -e "    ${BLUE}→${NC} Installing gh via brew..."
            brew install gh >/dev/null 2>&1 && _gh_installed=true
        fi
        if ! $_gh_installed; then
            echo -e "    ${BLUE}→${NC} Installing gh via apt..."
            if ! has_cmd gpg; then apt_install gnupg; fi
            (
                curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
                    | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null &&
                echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
                    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null &&
                sudo apt-get update -qq >/dev/null 2>&1 &&
                sudo apt-get install -y gh >/dev/null 2>&1
            ) && _gh_installed=true
        fi
        $_gh_installed && check_pass "gh installed" || check_fail "gh — install failed (see https://cli.github.com)"
    fi
fi

# Node.js + npm (required for Claude Code install)
if has_cmd npm; then
    check_pass "npm $(get_version npm --version)"
else
    check_fail "npm — NOT FOUND (required for Claude Code)"
    if $INSTALL; then
        _npm_installed=false
        if has_cmd brew; then
            echo -e "    ${BLUE}→${NC} Installing Node.js via brew..."
            brew install node >/dev/null 2>&1 && _npm_installed=true
        fi
        if ! $_npm_installed; then
            echo -e "    ${BLUE}→${NC} Installing Node.js via apt..."
            apt_install nodejs && apt_install npm && _npm_installed=true
        fi
        $_npm_installed && check_pass "npm installed" || check_fail "npm — install failed"
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# Section 2: Tailscale (mesh networking — all roles)
# ═════════════════════════════════════════════════════════════════════════════
section "Tailscale"

if has_cmd tailscale; then
    check_pass "tailscale $(get_version tailscale --version)"
    if tailscale status >/dev/null 2>&1; then
        check_pass "tailscaled running"
    else
        check_warn "tailscaled is not active — run: sudo tailscale up"
    fi
else
    check_fail "tailscale — NOT FOUND"
    if $INSTALL; then
        echo -e "    ${BLUE}→${NC} Installing tailscale..."
        curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1 \
            && check_pass "tailscale installed" \
            || check_fail "tailscale — install failed (see https://tailscale.com/download)"
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# Section 3: Python + pixi (all roles)
# Required by: Myrmidons workers, Hermes bridge, console tools
# ═════════════════════════════════════════════════════════════════════════════
section "Python & pixi"

# Python 3.10+
if has_cmd python3; then
    PY_VER=$(get_version python3 --version)
    if version_gte "$PY_VER" "3.10"; then
        check_pass "python3 $PY_VER (>= 3.10)"
    else
        check_fail "python3 $PY_VER — need >= 3.10"
        if $INSTALL; then
            echo -e "    ${BLUE}→${NC} Installing python3.10..."
            apt_install python3.10 \
                && sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 10 >/dev/null 2>&1 \
                && check_pass "python3.10 installed" || check_fail "python3.10 — install failed"
        fi
    fi
else
    check_fail "python3 — NOT FOUND"
    apt_install python3 && check_pass "python3 installed" || true
fi

# pip3 (needed to install nats-py and other Python deps)
if has_cmd pip3; then
    check_pass "pip3 $(get_version pip3 --version)"
else
    check_fail "pip3 — NOT FOUND"
    if $INSTALL; then
        apt_install python3-pip && check_pass "pip3 installed" || \
            (python3 -m ensurepip --upgrade >/dev/null 2>&1 && check_pass "pip3 bootstrapped via ensurepip") || true
    fi
fi

# python3.12-venv (required for pixi and isolated virtual environments)
if python3 -c "import venv" 2>/dev/null; then
    check_pass "python3 venv module available"
else
    check_fail "python3-venv — NOT FOUND"
    if $INSTALL; then
        apt_install python3.12-venv \
            || apt_install python3-venv \
            && check_pass "python3-venv installed" \
            || check_fail "python3-venv — install failed"
    fi
fi

# huggingface-cli (model/dataset downloads from Hugging Face Hub)
if has_cmd huggingface-cli; then
    check_pass "huggingface-cli $(get_version huggingface-cli version)"
else
    check_fail "huggingface-cli — NOT FOUND"
    if $INSTALL && has_cmd pip3; then
        echo -e "    ${BLUE}→${NC} Installing huggingface_hub[cli]..."
        pip3 install --break-system-packages "huggingface_hub[cli]" >/dev/null 2>&1 \
            || pip3 install --user "huggingface_hub[cli]" >/dev/null 2>&1 \
            && check_pass "huggingface-cli installed" \
            || check_fail "huggingface-cli — install failed"
    fi
fi

# nats-py (NATS Python client used by all myrmidon workers)
if python3 -c "import nats" 2>/dev/null; then
    NATS_PY=$(python3 -c "import importlib.metadata; print(importlib.metadata.version('nats-py'))" 2>/dev/null || echo "installed")
    check_pass "nats-py $NATS_PY"
else
    check_fail "nats-py — NOT FOUND (required by myrmidon workers)"
    if $INSTALL; then
        echo -e "    ${BLUE}→${NC} Installing nats-py..."
        pip3 install --break-system-packages nats-py >/dev/null 2>&1 \
            || pip3 install --user nats-py >/dev/null 2>&1 \
            && check_pass "nats-py installed" \
            || check_fail "nats-py — install failed (try: pip3 install nats-py)"
    fi
fi

# pixi (Python environment manager for Hermes + ProjectHephaestus)
if has_cmd pixi; then
    check_pass "pixi $(get_version pixi --version)"
else
    check_fail "pixi — NOT FOUND (required by Hermes bridge and ProjectHephaestus)"
    if $INSTALL; then
        echo -e "    ${BLUE}→${NC} Installing pixi..."
        curl -fsSL https://pixi.sh/install.sh | bash >/dev/null 2>&1 \
            && check_pass "pixi installed" \
            || check_fail "pixi — install failed (see https://pixi.sh)"
    fi
fi

# Mojo (ProjectOdyssey) — installed via pixi, not as a system binary
# The mojo binary lives in ProjectOdyssey/.pixi/envs/default/bin/mojo
ODYSSEY_DIR="${ODYSSEY_DIR:-$HOME/ProjectOdyssey}"
if has_cmd mojo; then
    check_pass "mojo $(get_version mojo --version)"
elif [[ -x "$ODYSSEY_DIR/.pixi/envs/default/bin/mojo" ]]; then
    check_pass "mojo (via pixi env at $ODYSSEY_DIR)"
elif [[ -d "$ODYSSEY_DIR" ]]; then
    check_warn "mojo — pixi env not initialized in $ODYSSEY_DIR"
    if $INSTALL && has_cmd pixi; then
        echo -e "    ${BLUE}→${NC} Running pixi install in $ODYSSEY_DIR..."
        pixi install -q --manifest-path "$ODYSSEY_DIR/pixi.toml" >/dev/null 2>&1 \
            && check_pass "mojo installed via pixi" \
            || check_warn "mojo — pixi install failed (check $ODYSSEY_DIR/pixi.toml)"
    fi
else
    check_skip "mojo — $ODYSSEY_DIR not found (clone ProjectOdyssey first)"
fi

# ═════════════════════════════════════════════════════════════════════════════
# Section 4: Go (Atlas dashboard — worker role)
# Required by: infrastructure/ProjectArgus/dashboard (Atlas, port 3002)
# ═════════════════════════════════════════════════════════════════════════════
if should_check_worker; then
    section "Go (Atlas Dashboard)"

    GO_MIN="1.23"
    _go_installed_now=false

    _install_go() {
        echo -e "    ${BLUE}→${NC} Installing Go 1.23.8..."
        GOARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
        GO_PKG="go1.23.8.linux-${GOARCH}.tar.gz"
        if curl -fsSL "https://go.dev/dl/$GO_PKG" -o "/tmp/$GO_PKG" \
            && sudo rm -rf /usr/local/go \
            && sudo tar -C /usr/local -xzf "/tmp/$GO_PKG" \
            && rm "/tmp/$GO_PKG"; then
            check_pass "Go 1.23.8 installed to /usr/local/go"
            add_to_bashrc "export PATH=\$PATH:/usr/local/go/bin"
            _go_installed_now=true
        else
            check_fail "Go — install failed"
        fi
    }

    if has_cmd go || [[ -x /usr/local/go/bin/go ]]; then
        GO_BIN=$(has_cmd go && echo "go" || echo "/usr/local/go/bin/go")
        GO_VER=$(get_version "$GO_BIN" version)
        if version_gte "$GO_VER" "$GO_MIN"; then
            check_pass "go $GO_VER (>= $GO_MIN)"
        else
            check_fail "go $GO_VER — need >= $GO_MIN (templ requires 1.23+)"
            $INSTALL && _install_go
        fi
    else
        check_fail "go — NOT FOUND"
        $INSTALL && _install_go
    fi

    # templ (Go HTML template generator used by Atlas)
    # Use /usr/local/go/bin/go directly in case Go was just installed and PATH not yet refreshed
    GO_BIN_FOR_TEMPL=$(has_cmd go && echo "go" || echo "/usr/local/go/bin/go")
    if has_cmd templ; then
        check_pass "templ $(get_version templ version)"
    else
        check_fail "templ — NOT FOUND (required by Atlas dashboard)"
        if $INSTALL && [[ -x "$GO_BIN_FOR_TEMPL" ]]; then
            echo -e "    ${BLUE}→${NC} Installing templ..."
            GOBIN=$HOME/.local/bin "$GO_BIN_FOR_TEMPL" install github.com/a-h/templ/cmd/templ@latest >/dev/null 2>&1 \
                && check_pass "templ installed to ~/.local/bin" \
                || check_fail "templ — install failed"
        elif $INSTALL; then
            check_fail "templ — skipped (go not available)"
        fi
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# Section 5: NATS Server (hub host)
# Required by: hub host only — native binary to avoid slirp4netns
# ═════════════════════════════════════════════════════════════════════════════
section "NATS Server"

NATS_MIN="2.10"
NATS_BIN="${NATS_SERVER_BIN:-${HOME}/.local/bin/nats-server}"

if has_cmd nats-server || [[ -x "$NATS_BIN" ]]; then
    NATS_EXEC=$(has_cmd nats-server && echo "nats-server" || echo "$NATS_BIN")
    NATS_VER=$(get_version "$NATS_EXEC" --version 2>/dev/null || "$NATS_EXEC" -v 2>&1 | grep -oP '\d+\.\d+[\.\d]*' | head -1)
    if version_gte "$NATS_VER" "$NATS_MIN"; then
        check_pass "nats-server $NATS_VER (>= $NATS_MIN)"
    else
        check_fail "nats-server $NATS_VER — need >= $NATS_MIN"
    fi
else
    check_fail "nats-server — NOT FOUND"
    if $INSTALL; then
        echo -e "    ${BLUE}→${NC} Installing nats-server 2.10.24..."
        GOARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
        NATS_PKG="nats-server-v2.10.24-linux-${GOARCH}.tar.gz"
        NATS_URL="https://github.com/nats-io/nats-server/releases/download/v2.10.24/$NATS_PKG"
        mkdir -p ~/.local/bin
        curl -fsSL "$NATS_URL" -o "/tmp/$NATS_PKG" \
            && tar -xzf "/tmp/$NATS_PKG" -C /tmp \
            && mv "/tmp/nats-server-v2.10.24-linux-${GOARCH}/nats-server" ~/.local/bin/nats-server \
            && chmod +x ~/.local/bin/nats-server \
            && rm -rf "/tmp/$NATS_PKG" "/tmp/nats-server-v2.10.24-linux-${GOARCH}" \
            && check_pass "nats-server 2.10.24 installed to ~/.local/bin" \
            || check_fail "nats-server — install failed"
        echo -e "    ${DIM}Ensure ~/.local/bin is in PATH${NC}"
    fi
fi

# nats CLI (JetStream management and monitoring)
if has_cmd nats; then
    check_pass "nats CLI $(get_version nats --version)"
else
    check_warn "nats CLI — NOT FOUND (optional, useful for stream inspection)"
    if $INSTALL; then
        echo -e "    ${BLUE}→${NC} Installing nats CLI 0.1.5..."
        GOARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
        NATS_CLI_PKG="nats-0.1.5-linux-${GOARCH}.zip"
        NATS_CLI_URL="https://github.com/nats-io/natscli/releases/download/v0.1.5/$NATS_CLI_PKG"
        mkdir -p ~/.local/bin
        curl -fsSL "$NATS_CLI_URL" -o "/tmp/$NATS_CLI_PKG" \
            && unzip -q "/tmp/$NATS_CLI_PKG" -d /tmp/nats-cli \
            && mv /tmp/nats-cli/nats-*/nats ~/.local/bin/nats \
            && chmod +x ~/.local/bin/nats \
            && rm -rf "/tmp/$NATS_CLI_PKG" /tmp/nats-cli \
            && check_pass "nats CLI installed to ~/.local/bin" \
            || check_warn "nats CLI — install failed (optional)"
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# Section 6: Container Runtime (worker role — AchaeanFleet compose stack)
# ═════════════════════════════════════════════════════════════════════════════
if should_check_worker; then
    section "Container Runtime"

    if has_cmd podman; then
        check_pass "podman $(get_version podman --version)"
    else
        check_fail "podman — NOT FOUND"
        apt_install podman && check_pass "podman installed" || true
    fi

    if has_cmd podman && podman compose version >/dev/null 2>&1; then
        COMPOSE_VER=$(podman compose version 2>&1 | grep -oP '\d+\.\d+[\.\d]*' | head -1)
        check_pass "podman compose $COMPOSE_VER"
    else
        check_fail "podman compose — NOT FOUND"
        apt_install podman-compose && check_pass "podman-compose installed" || true
    fi

    # Podman socket (required for some compose operations)
    PODMAN_SOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
    if [[ -S "$PODMAN_SOCK" ]]; then
        check_pass "podman socket active"
    else
        check_warn "podman socket not active — run: systemctl --user enable --now podman.socket"
        if $INSTALL; then
            systemctl --user enable --now podman.socket 2>/dev/null \
                && check_pass "podman socket enabled" \
                || check_warn "podman socket — could not enable (may need desktop session)"
        fi
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# Section 7: C++ Build Chain (control role — Agamemnon + Nestor)
# ═════════════════════════════════════════════════════════════════════════════
if should_check_control; then
    section "C++ Build Chain (Agamemnon / Nestor)"

    CMAKE_MIN="3.20"
    if has_cmd cmake; then
        CMAKE_VER=$(get_version cmake --version)
        if version_gte "$CMAKE_VER" "$CMAKE_MIN"; then
            check_pass "cmake $CMAKE_VER (>= $CMAKE_MIN)"
        else
            check_fail "cmake $CMAKE_VER — need >= $CMAKE_MIN"
            apt_install cmake && check_pass "cmake installed" || true
        fi
    else
        check_fail "cmake — NOT FOUND"
        apt_install cmake && check_pass "cmake installed" || true
    fi

    if has_cmd ninja; then
        check_pass "ninja $(get_version ninja --version)"
    else
        check_fail "ninja — NOT FOUND"
        apt_install ninja-build && check_pass "ninja installed" || true
    fi

    for pkg in gcc g++ libssl-dev clang clang-format clang-tidy gdb valgrind lcov gcovr cppcheck ccache; do
        if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
            check_pass "$pkg"
        else
            check_fail "$pkg — NOT FOUND"
            apt_install "$pkg" && check_pass "$pkg installed" || true
        fi
    done

    # Conan (C++ package manager — used by Agamemnon/Nestor)
    _conan_ensure_profile() {
        local conan_bin="${1:-conan}"
        if "$conan_bin" profile show default >/dev/null 2>&1; then
            check_pass "conan default profile exists"
        else
            check_fail "conan default profile missing"
            if $INSTALL; then
                "$conan_bin" profile detect --force >/dev/null 2>&1 \
                    && check_pass "conan profile created" \
                    || check_fail "conan profile detect failed"
            fi
        fi
    }

    if has_cmd conan; then
        check_pass "conan $(get_version conan --version)"
        _conan_ensure_profile conan
    else
        check_fail "conan — NOT FOUND"
        if $INSTALL && has_cmd pip3; then
            pip3 install --break-system-packages conan >/dev/null 2>&1 \
                || pip3 install --user conan >/dev/null 2>&1
            # Locate the freshly-installed conan (may be in ~/.local/bin or pip prefix)
            CONAN_BIN=$(command -v conan 2>/dev/null \
                || python3 -c "import sysconfig; print(sysconfig.get_path('scripts'))" 2>/dev/null | xargs -I{} ls {}/conan 2>/dev/null \
                || echo "")
            if [[ -n "$CONAN_BIN" && -x "$CONAN_BIN" ]]; then
                check_pass "conan installed"
                _conan_ensure_profile "$CONAN_BIN"
            else
                check_fail "conan — install failed"
            fi
        fi
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# Section 8: Claude Code + plugins (all roles)
# ═════════════════════════════════════════════════════════════════════════════
section "Claude Code"

if has_cmd claude; then
    check_pass "claude $(claude --version 2>&1 | head -1)"
else
    check_fail "claude — NOT FOUND"
    if $INSTALL; then
        if has_cmd npm; then
            echo -e "    ${BLUE}→${NC} Installing Claude Code via npm..."
            npm install -g @anthropic-ai/claude-code >/dev/null 2>&1 \
                && check_pass "claude installed" \
                || check_fail "claude — install failed (see https://claude.ai/code)"
        else
            check_fail "claude — skipped (npm not found; install Node.js first)"
        fi
    fi
fi

if has_cmd claude; then
    # Ensure required marketplaces are registered
    declare -A MARKETPLACES=(
        [claude-plugins-official]="anthropics/claude-plugins-official"
        [cc-marketplace]="kenryu42/cc-marketplace"
        [ProjectHephaestus]="HomericIntelligence/ProjectHephaestus"
    )
    for mkt_name in "${!MARKETPLACES[@]}"; do
        mkt_source="${MARKETPLACES[$mkt_name]}"
        if claude plugin marketplace list 2>/dev/null | grep -qF "$mkt_name"; then
            check_pass "marketplace $mkt_name"
        else
            check_fail "marketplace $mkt_name — NOT REGISTERED"
            if $INSTALL; then
                claude plugin marketplace add "https://github.com/$mkt_source" --name "$mkt_name" 2>/dev/null \
                    && check_pass "marketplace $mkt_name added" \
                    || check_fail "marketplace $mkt_name — add failed"
            fi
        fi
    done

    # User-scoped plugins (enabled across all projects)
    declare -A USER_PLUGINS=(
        [clangd-lsp]="claude-plugins-official"
        [code-review]="claude-plugins-official"
        [commit-commands]="claude-plugins-official"
        [feature-dev]="claude-plugins-official"
        [pyright-lsp]="claude-plugins-official"
        [safety-net]="cc-marketplace"
        [security-guidance]="claude-plugins-official"
    )
    for plugin_name in "${!USER_PLUGINS[@]}"; do
        mkt="${USER_PLUGINS[$plugin_name]}"
        if claude plugin list 2>/dev/null | grep -qE "^\s+[>❯]\s+${plugin_name}@"; then
            check_pass "plugin $plugin_name (user)"
        else
            check_fail "plugin $plugin_name — NOT INSTALLED"
            if $INSTALL; then
                claude plugin install "${plugin_name}@${mkt}" --scope user 2>/dev/null \
                    && check_pass "plugin $plugin_name installed" \
                    || check_fail "plugin $plugin_name — install failed"
            fi
        fi
    done

    # Project-scoped plugins (installed per-project in the repo's .claude-plugin)
    declare -A PROJECT_PLUGINS=(
        [hephaestus]="ProjectHephaestus"
    )
    for plugin_name in "${!PROJECT_PLUGINS[@]}"; do
        mkt="${PROJECT_PLUGINS[$plugin_name]}"
        if claude plugin list 2>/dev/null | grep -qE "^\s+[>❯]\s+${plugin_name}@"; then
            check_pass "plugin $plugin_name (project)"
        else
            check_fail "plugin $plugin_name — NOT INSTALLED"
            if $INSTALL; then
                claude plugin install "${plugin_name}@${mkt}" --scope project 2>/dev/null \
                    && check_pass "plugin $plugin_name installed" \
                    || check_fail "plugin $plugin_name — install failed"
            fi
        fi
    done
fi

# ═════════════════════════════════════════════════════════════════════════════
# Section 9: Python dev toolchain (all roles)
# pre-commit, ruff, mypy are required by every project's CI hooks
# ═════════════════════════════════════════════════════════════════════════════
section "Python Dev Toolchain"

for pytool in pre-commit ruff mypy; do
    if has_cmd "$pytool"; then
        check_pass "$pytool $(get_version "$pytool" --version 2>/dev/null || echo installed)"
    else
        check_fail "$pytool — NOT FOUND"
        if $INSTALL && has_cmd pip3; then
            pip3 install --break-system-packages "$pytool" >/dev/null 2>&1 \
                || pip3 install --user "$pytool" >/dev/null 2>&1 \
                && check_pass "$pytool installed" \
                || check_fail "$pytool — install failed"
        fi
    fi
done

# Dagger CLI (ProjectProteus pipeline engine)
if has_cmd dagger; then
    check_pass "dagger $(get_version dagger version 2>/dev/null || echo installed)"
else
    check_warn "dagger — NOT FOUND (required by ProjectProteus)"
    if $INSTALL; then
        echo -e "    ${BLUE}→${NC} Installing Dagger CLI..."
        curl -fsSL https://dl.dagger.io/dagger/install.sh | BIN_DIR=~/.local/bin sh >/dev/null 2>&1 \
            && check_pass "dagger installed to ~/.local/bin" \
            || check_warn "dagger — install failed (see https://dagger.io)"
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# Section 10: Observability stack (worker role — ProjectArgus)
# Checks that container images are available for the monitoring stack
# ═════════════════════════════════════════════════════════════════════════════
if should_check_worker; then
    section "Observability (ProjectArgus)"

    # The observability stack runs via podman/docker compose — just verify
    # the container runtime is available (already checked in Section 6).
    # We warn rather than fail since these pull automatically on first compose up.
    OBS_IMAGES=(
        "prom/prometheus:v2.54.1"
        "grafana/loki:3.1.2"
        "grafana/grafana:11.2.2"
        "grafana/promtail:3.1.2"
        "nginx:1.27-alpine"
    )
    OBS_RUNTIME=""
    has_cmd podman && OBS_RUNTIME="podman"
    has_cmd docker && [[ -z "$OBS_RUNTIME" ]] && OBS_RUNTIME="docker"

    if [[ -n "$OBS_RUNTIME" ]]; then
        for img in "${OBS_IMAGES[@]}"; do
            if "$OBS_RUNTIME" image exists "$img" 2>/dev/null \
               || "$OBS_RUNTIME" image inspect "$img" >/dev/null 2>&1; then
                check_pass "$img (cached)"
            else
                check_warn "$img — not pulled (run: $OBS_RUNTIME pull $img)"
            fi
        done
    else
        check_warn "observability images — no container runtime found (podman or docker required)"
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
# Section 11: PATH sanity check
# ═════════════════════════════════════════════════════════════════════════════
section "PATH"

MISSING_PATHS=()
for dir in "$HOME/.local/bin" "/usr/local/go/bin"; do
    if [[ ":$PATH:" != *":$dir:"* ]]; then
        MISSING_PATHS+=("$dir")
    fi
done

if [[ ${#MISSING_PATHS[@]} -eq 0 ]]; then
    check_pass "PATH includes ~/.local/bin and /usr/local/go/bin"
else
    for dir in "${MISSING_PATHS[@]}"; do
        if $INSTALL; then
            add_to_bashrc "export PATH=\$PATH:$dir"
            check_pass "$dir added to PATH (via ~/.bashrc)"
        else
            check_warn "$dir not in PATH"
            echo -e "    ${DIM}Run with --install to add to ~/.bashrc, or: export PATH=\$PATH:$dir${NC}"
        fi
    done
fi

# ═════════════════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}Summary${NC}"
echo "═══════"
echo -e "  ${GREEN}✓${NC} Passed:  $_PASS"
[[ $_FAIL -gt 0 ]]  && echo -e "  ${RED}✗${NC} Failed:  $_FAIL"
[[ $_WARN -gt 0 ]]  && echo -e "  ${YELLOW}⚠${NC} Warnings: $_WARN"
[[ $_SKIP -gt 0 ]]  && echo -e "  ${DIM}–${NC} Skipped: $_SKIP"
echo ""

if [[ $_FAIL -gt 0 ]]; then
    if $INSTALL; then
        echo -e "${YELLOW}Some installs may require opening a new shell to take effect (PATH changes).${NC}"
    else
        echo -e "${YELLOW}Run with --install to attempt automatic installation of missing dependencies.${NC}"
    fi
    exit 1
fi

echo -e "${GREEN}All checks passed.${NC}"
