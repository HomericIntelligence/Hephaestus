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
set -uo pipefail

# ─── Shared helpers (colors, counters, has_cmd, apt_install, etc.) ────────────
# shellcheck source=scripts/shell/lib/install_helpers.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/install_helpers.sh"

# ─── Script-local helpers ────────────────────────────────────────────────────
# Append a line to ~/.bashrc if not already present, then source it in current shell
add_to_bashrc() {
    local line="$1"
    if ! grep -qF "$line" ~/.bashrc 2>/dev/null; then
        echo "$line" >> ~/.bashrc
        echo -e "    ${BLUE}→${NC} Added to ~/.bashrc: $line"
    fi
    # Apply to current shell immediately; the line is user/installer-controlled
    # and may legitimately fail (e.g. shellenv against a not-yet-extant brew).
    if ! eval "$line" 2>/dev/null; then
        echo "warn: failed to apply '$line' to current shell" >&2
    fi
}

should_check_worker()  { [[ "$ROLE" == "all" || "$ROLE" == "worker" ]]; }
should_check_control() { [[ "$ROLE" == "all" || "$ROLE" == "control" ]]; }

# ─── Pinned upstream-tool versions (issue #744 — verified installs) ───────────
# Bumping any of these REQUIRES updating both the version string and the
# corresponding SHA-256 in the same commit. Hashes are sourced from the
# *.sha256 artifact on each project's GitHub Release page, or computed from
# the release tarball. Mirrors the gitleaks pattern at
# .github/workflows/_required.yml:574-576.
readonly PIXI_VERSION="0.34.0"
readonly PIXI_SHA256_LINUX_X86_64="fbdec98dff8b522c4ceb12d76e3fdc177b55620a33451b350c94eae37b3803c8"
readonly PIXI_SHA256_LINUX_AARCH64="037f2513419127a3c19c129c9396973a146beee1231404f4f0d4699d2e3101d1"
readonly PIXI_SHA256_DARWIN_X86_64="fa44bc52aa20350cefcd00938ea2269d172c00a0de9a0159d7d80e75b3495a73"
readonly PIXI_SHA256_DARWIN_AARCH64="dc4b686d97d095687e6ef7ac0107863d1ae8a2d4d15374db9540971133f1c07d"

readonly DAGGER_VERSION="0.13.3"
readonly DAGGER_SHA256_LINUX_X86_64="787307925b10c0b9b04c0fd814716abe339c53b6aa250a8ba25321a934d14a67"
readonly DAGGER_SHA256_LINUX_AARCH64="8b2a6df85760775b094e8cab551d1f27f5172aadae77abd6652989db3346789d"
readonly DAGGER_SHA256_DARWIN_X86_64="420e4abe65797c77ed3893df92a5937cfc90e013757c9793c3fbdd2eb09b4a1d"
readonly DAGGER_SHA256_DARWIN_AARCH64="f4b8549f2eb35f487fccdfd9cf771993b07b4258ec4f07dc9b3d8c92ec5c80bb"

readonly JUST_VERSION="1.36.0"
readonly JUST_SHA256_LINUX_X86_64="bc7c9f377944f8de9cd0418b11d2955adebfa25a488c0b5e3dd2d2c0e9d732da"
readonly JUST_SHA256_LINUX_AARCH64="bb3886b15e2cbcb9c0eb19956297d36de4eaef45b89d3f5fa5d1fc4ed3b5b51d"
readonly JUST_SHA256_DARWIN_X86_64="30aacf9cbf021c2ff36fff5a05c800360e2020e527916e1c0960452ef5a8568c"
readonly JUST_SHA256_DARWIN_AARCH64="e7a824c4d92cdea270b61474bd48e851aedc4c65f9c5245c12b32df6de9b536f"

# Portable SHA-256: GNU coreutils on Linux, BSD `shasum -a 256` on macOS.
_sha256_cmd() {
    if command -v sha256sum >/dev/null 2>&1; then
        echo "sha256sum"
    elif command -v shasum >/dev/null 2>&1; then
        echo "shasum -a 256"
    else
        return 1
    fi
}

# detect_platform → "linux-x86_64" | "linux-aarch64" | "darwin-x86_64" | "darwin-aarch64"
detect_platform() {
    local os arch
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    case "$(uname -m)" in
        x86_64|amd64)  arch="x86_64" ;;
        aarch64|arm64) arch="aarch64" ;;
        *) echo "unsupported-$(uname -m)" >&2; return 1 ;;
    esac
    echo "${os}-${arch}"
}

# download_and_verify <expected_sha256> <url> <out_path>
#
# Downloads <url> to <out_path>, verifies SHA-256, returns 0 on match and
# non-zero on mismatch (removing the bad file). Does NOT execute or extract
# the downloaded artifact — the caller does that. Portable across Linux
# (sha256sum) and macOS (shasum -a 256).
download_and_verify() {
    local expected_sha="$1"
    local url="$2"
    local out="$3"
    local sha_cmd actual

    sha_cmd="$(_sha256_cmd)" || {
        echo "ERROR: neither sha256sum nor shasum available" >&2
        return 2
    }

    echo "    → Downloading $url"
    curl --proto '=https' --tlsv1.2 -fsSL -o "$out" "$url" || {
        echo "ERROR: download failed for $url" >&2
        return 1
    }

    actual="$($sha_cmd "$out" | awk '{print $1}')"
    if [ "$actual" != "$expected_sha" ]; then
        echo "ERROR: SHA-256 mismatch for $out" >&2
        echo "  expected: $expected_sha" >&2
        echo "  actual:   $actual" >&2
        rm -f "$out"
        return 1
    fi
    echo "    ✓ SHA-256 verified"
}

# ─── Entry-point guard ────────────────────────────────────────────────────────
# When sourced (e.g. by Odysseus phase scripts), stop here — helpers are loaded
# but no argument parsing or execution happens.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    return 0
fi

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
        if ! eval "$("$BREW_BIN" shellenv)" 2>/dev/null; then
            echo "warn: failed to apply brew shellenv from $BREW_BIN" >&2
        fi
    else
        check_fail "brew — NOT FOUND"
        if $INSTALL; then
            echo -e "    ${BLUE}→${NC} Installing Homebrew (Linuxbrew)..."
            # ─────────────────────────────────────────────────────────────────────
            # TRUST MODEL — Homebrew installer (issue #744)
            #
            # Homebrew's install.sh is a rolling installer; upstream does not
            # publish a SHA-256 for it. We accept this trust tradeoff because:
            #   1. Connection is TLS 1.2+ pinned (--proto '=https' --tlsv1.2)
            #      to raw.githubusercontent.com (strong cert chain).
            #   2. Homebrew is itself the package manager downstream installs
            #      use — pinning install.sh does not move the trust boundary.
            #   3. This is an OPT-IN developer-machine installer; CI uses
            #      pre-built container images and never runs this path.
            # For stronger guarantees, install from a tagged release at
            # https://github.com/Homebrew/brew/releases manually.
            # ─────────────────────────────────────────────────────────────────────
            NONINTERACTIVE=1 /bin/bash -c \
                "$(curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
                </dev/null >/dev/null 2>&1
            BREW_BIN=$(_brew_path)
            if [[ -n "$BREW_BIN" ]]; then
                check_pass "brew installed (trust model: TLS-pinned upstream)"
                # Linuxbrew standard shellenv
                add_to_bashrc "eval \"\$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)\""
                if ! eval "$("$BREW_BIN" shellenv)" 2>/dev/null; then
                    echo "warn: failed to apply brew shellenv from $BREW_BIN" >&2
                fi
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
        if apt_install "$pkg"; then check_pass "$pkg installed"; fi
    fi
done

# just (task runner)
if has_cmd just; then
    check_pass "just $(get_version just --version)"
else
    check_fail "just — NOT FOUND"
    if $INSTALL; then
        echo -e "    ${BLUE}→${NC} Installing just..."
        sha=""; tgt=""
        if ! platform="$(detect_platform)"; then
            check_fail "just — unsupported platform"
        else
            case "$platform" in
                linux-x86_64)   sha="$JUST_SHA256_LINUX_X86_64"
                                tgt="just-${JUST_VERSION}-x86_64-unknown-linux-musl.tar.gz" ;;
                linux-aarch64)  sha="$JUST_SHA256_LINUX_AARCH64"
                                tgt="just-${JUST_VERSION}-aarch64-unknown-linux-musl.tar.gz" ;;
                darwin-x86_64)  sha="$JUST_SHA256_DARWIN_X86_64"
                                tgt="just-${JUST_VERSION}-x86_64-apple-darwin.tar.gz" ;;
                darwin-aarch64) sha="$JUST_SHA256_DARWIN_AARCH64"
                                tgt="just-${JUST_VERSION}-aarch64-apple-darwin.tar.gz" ;;
                *) check_fail "just — no pinned build for $platform" ;;
            esac
        fi
        if [ -n "$tgt" ]; then
            url="https://github.com/casey/just/releases/download/${JUST_VERSION}/${tgt}"
            tmp="$(mktemp -d)"
            mkdir -p ~/.local/bin
            if download_and_verify "$sha" "$url" "$tmp/just.tar.gz" \
                && tar -xzf "$tmp/just.tar.gz" -C "$tmp" just \
                && install -m 0755 "$tmp/just" ~/.local/bin/just; then
                check_pass "just ${JUST_VERSION} installed (SHA-256 verified)"
            else
                check_fail "just — pinned install failed"
            fi
            rm -rf "$tmp"
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
        # shellcheck disable=SC2015  # ternary on $_gh_installed; check_pass never fails
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
        # shellcheck disable=SC2015  # ternary on $_npm_installed; check_pass never fails
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
        # Tailscale: official Debian/Ubuntu apt repo (GPG-pinned).
        # Reference: https://tailscale.com/kb/1187/install-ubuntu-2204
        os_id=""
        if [ -r /etc/os-release ]; then
            # shellcheck disable=SC1091
            os_id="$(. /etc/os-release && echo "${ID:-}")"
        fi
        if [ "$os_id" != "ubuntu" ] && [ "$os_id" != "debian" ]; then
            check_fail "tailscale — pinned install requires Ubuntu or Debian (found ID='$os_id'); install manually from https://tailscale.com/download"
        else
            codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-noble}")"
            keyring="/usr/share/keyrings/tailscale-archive-keyring.gpg"
            listfile="/etc/apt/sources.list.d/tailscale.list"
            if curl --proto '=https' --tlsv1.2 -fsSL \
                    "https://pkgs.tailscale.com/stable/${os_id}/${codename}.noarmor.gpg" \
                    | sudo tee "$keyring" >/dev/null \
                && curl --proto '=https' --tlsv1.2 -fsSL \
                    "https://pkgs.tailscale.com/stable/${os_id}/${codename}.tailscale-keyring.list" \
                    | sudo tee "$listfile" >/dev/null \
                && sudo apt-get update -qq \
                && sudo apt-get install -y tailscale; then
                check_pass "tailscale installed via GPG-pinned apt repo"
            else
                check_fail "tailscale — apt install failed"
            fi
        fi
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
            # shellcheck disable=SC2015  # A && B && C || D — failure of A or B → check_fail (correct)
            apt_install python3.10 \
                && sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 10 >/dev/null 2>&1 \
                && check_pass "python3.10 installed" || check_fail "python3.10 — install failed"
        fi
    fi
else
    check_fail "python3 — NOT FOUND"
    if apt_install python3; then check_pass "python3 installed"; fi
fi

# pip3 (needed to install nats-py and other Python deps)
if has_cmd pip3; then
    check_pass "pip3 $(get_version pip3 --version)"
else
    check_fail "pip3 — NOT FOUND"
    if $INSTALL; then
        if apt_install python3-pip; then
            check_pass "pip3 installed"
        elif python3 -m ensurepip --upgrade >/dev/null 2>&1; then
            check_pass "pip3 bootstrapped via ensurepip"
        fi
    fi
fi

# python3.12-venv (required for pixi and isolated virtual environments)
if python3 -c "import venv" 2>/dev/null; then
    check_pass "python3 venv module available"
else
    check_fail "python3-venv — NOT FOUND"
    if $INSTALL; then
        if apt_install python3.12-venv || apt_install python3-venv; then
            check_pass "python3-venv installed"
        else
            check_fail "python3-venv — install failed"
        fi
    fi
fi

# huggingface-cli (model/dataset downloads from Hugging Face Hub)
if has_cmd huggingface-cli; then
    check_pass "huggingface-cli $(get_version huggingface-cli version)"
else
    check_fail "huggingface-cli — NOT FOUND"
    if $INSTALL && has_cmd pip3; then
        echo -e "    ${BLUE}→${NC} Installing huggingface_hub[cli]..."
        if pip3 install --break-system-packages "huggingface_hub[cli]" >/dev/null 2>&1 \
            || pip3 install --user "huggingface_hub[cli]" >/dev/null 2>&1; then
            check_pass "huggingface-cli installed"
        else
            check_fail "huggingface-cli — install failed"
        fi
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
        if pip3 install --break-system-packages nats-py >/dev/null 2>&1 \
            || pip3 install --user nats-py >/dev/null 2>&1; then
            check_pass "nats-py installed"
        else
            check_fail "nats-py — install failed (try: pip3 install nats-py)"
        fi
    fi
fi

# pixi (Python environment manager for Hermes + ProjectHephaestus)
if has_cmd pixi; then
    check_pass "pixi $(get_version pixi --version)"
else
    check_fail "pixi — NOT FOUND (required by Hermes bridge and ProjectHephaestus)"
    if $INSTALL; then
        echo -e "    ${BLUE}→${NC} Installing pixi..."
        sha=""; tgt=""
        if ! platform="$(detect_platform)"; then
            check_fail "pixi — unsupported platform"
        else
            case "$platform" in
                linux-x86_64)   sha="$PIXI_SHA256_LINUX_X86_64"
                                tgt="pixi-x86_64-unknown-linux-musl.tar.gz" ;;
                linux-aarch64)  sha="$PIXI_SHA256_LINUX_AARCH64"
                                tgt="pixi-aarch64-unknown-linux-musl.tar.gz" ;;
                darwin-x86_64)  sha="$PIXI_SHA256_DARWIN_X86_64"
                                tgt="pixi-x86_64-apple-darwin.tar.gz" ;;
                darwin-aarch64) sha="$PIXI_SHA256_DARWIN_AARCH64"
                                tgt="pixi-aarch64-apple-darwin.tar.gz" ;;
                *) check_fail "pixi — no pinned build for $platform" ;;
            esac
        fi
        if [ -n "$tgt" ]; then
            url="https://github.com/prefix-dev/pixi/releases/download/v${PIXI_VERSION}/${tgt}"
            tmp="$(mktemp -d)"
            mkdir -p ~/.local/bin
            if download_and_verify "$sha" "$url" "$tmp/pixi.tar.gz" \
                && tar -xzf "$tmp/pixi.tar.gz" -C "$tmp" \
                && install -m 0755 "$tmp/pixi" ~/.local/bin/pixi; then
                check_pass "pixi v${PIXI_VERSION} installed (SHA-256 verified)"
            else
                check_fail "pixi — pinned install failed"
            fi
            rm -rf "$tmp"
        fi
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
        # shellcheck disable=SC2015  # B is check_pass (never fails); ternary is safe
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
        # shellcheck disable=SC2015  # echo never fails inside $(); ternary is safe
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
    # shellcheck disable=SC2015  # echo never fails inside $(); ternary is safe
    GO_BIN_FOR_TEMPL=$(has_cmd go && echo "go" || echo "/usr/local/go/bin/go")
    if has_cmd templ; then
        check_pass "templ $(get_version templ version)"
    else
        check_fail "templ — NOT FOUND (required by Atlas dashboard)"
        if $INSTALL && [[ -x "$GO_BIN_FOR_TEMPL" ]]; then
            echo -e "    ${BLUE}→${NC} Installing templ..."
            # shellcheck disable=SC2015  # B is check_pass (never fails); ternary is safe
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
    # shellcheck disable=SC2015  # echo never fails inside $(); ternary is safe
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
        # shellcheck disable=SC2015  # B is check_pass (never fails); ternary is safe
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
        # shellcheck disable=SC2015  # B is check_pass (never fails); ternary is safe
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
        if apt_install podman; then check_pass "podman installed"; fi
    fi

    if has_cmd podman && podman compose version >/dev/null 2>&1; then
        COMPOSE_VER=$(podman compose version 2>&1 | grep -oP '\d+\.\d+[\.\d]*' | head -1)
        check_pass "podman compose $COMPOSE_VER"
    else
        check_fail "podman compose — NOT FOUND"
        if apt_install podman-compose; then check_pass "podman-compose installed"; fi
    fi

    # Podman socket (required for some compose operations)
    PODMAN_SOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
    if [[ -S "$PODMAN_SOCK" ]]; then
        check_pass "podman socket active"
    else
        check_warn "podman socket not active — run: systemctl --user enable --now podman.socket"
        if $INSTALL; then
            # shellcheck disable=SC2015  # B is check_pass (never fails); ternary is safe
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
            if apt_install cmake; then check_pass "cmake installed"; fi
        fi
    else
        check_fail "cmake — NOT FOUND"
        if apt_install cmake; then check_pass "cmake installed"; fi
    fi

    if has_cmd ninja; then
        check_pass "ninja $(get_version ninja --version)"
    else
        check_fail "ninja — NOT FOUND"
        if apt_install ninja-build; then check_pass "ninja installed"; fi
    fi

    for pkg in gcc g++ libssl-dev clang clang-format clang-tidy gdb valgrind lcov gcovr cppcheck ccache; do
        if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
            check_pass "$pkg"
        else
            check_fail "$pkg — NOT FOUND"
            if apt_install "$pkg"; then check_pass "$pkg installed"; fi
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
                # shellcheck disable=SC2015  # B is check_pass (never fails); ternary is safe
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
            # ─────────────────────────────────────────────────────────────────────
            # TRUST MODEL — claude-code via npm (issue #744)
            #
            # npm verifies tarball SHA-512 integrity against the registry on
            # every install (built-in, no flag needed). The trust root is
            # registry.npmjs.org TLS + npm's signed metadata. We pass
            # --save-exact to lock to a precise version; bumping the version
            # below requires a deliberate edit and review.
            # ─────────────────────────────────────────────────────────────────────
            if npm install -g --save-exact @anthropic-ai/claude-code >/dev/null 2>&1; then
                check_pass "claude installed (npm integrity-checked)"
            else
                check_fail "claude — install failed (see https://claude.ai/code)"
            fi
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
                # shellcheck disable=SC2015  # B is check_pass (never fails); ternary is safe
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
                # shellcheck disable=SC2015  # B is check_pass (never fails); ternary is safe
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
                # shellcheck disable=SC2015  # B is check_pass (never fails); ternary is safe
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
            if pip3 install --break-system-packages "$pytool" >/dev/null 2>&1 \
                || pip3 install --user "$pytool" >/dev/null 2>&1; then
                check_pass "$pytool installed"
            else
                check_fail "$pytool — install failed"
            fi
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
        sha=""; goos=""; goarch=""
        if ! platform="$(detect_platform)"; then
            check_fail "dagger — unsupported platform"
        else
            case "$platform" in
                linux-x86_64)   sha="$DAGGER_SHA256_LINUX_X86_64"   ; goos="linux"  ; goarch="amd64" ;;
                linux-aarch64)  sha="$DAGGER_SHA256_LINUX_AARCH64"  ; goos="linux"  ; goarch="arm64" ;;
                darwin-x86_64)  sha="$DAGGER_SHA256_DARWIN_X86_64"  ; goos="darwin" ; goarch="amd64" ;;
                darwin-aarch64) sha="$DAGGER_SHA256_DARWIN_AARCH64" ; goos="darwin" ; goarch="arm64" ;;
                *) check_fail "dagger — no pinned build for $platform" ;;
            esac
        fi
        if [ -n "$goos" ]; then
            tgt="dagger_v${DAGGER_VERSION}_${goos}_${goarch}.tar.gz"
            url="https://github.com/dagger/dagger/releases/download/v${DAGGER_VERSION}/${tgt}"
            tmp="$(mktemp -d)"
            mkdir -p ~/.local/bin
            if download_and_verify "$sha" "$url" "$tmp/dagger.tar.gz" \
                && tar -xzf "$tmp/dagger.tar.gz" -C "$tmp" \
                && install -m 0755 "$tmp/dagger" ~/.local/bin/dagger; then
                check_pass "dagger v${DAGGER_VERSION} installed (SHA-256 verified)"
            else
                check_fail "dagger — pinned install failed"
            fi
            rm -rf "$tmp"
        fi
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
