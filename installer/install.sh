#!/bin/bash
# BNG Stack Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/hendroaritonang/bng-stack/main/installer/install.sh | bash
#   or:  curl -fsSL https://raw.githubusercontent.com/hendroaritonang/bng-stack/main/installer/install.sh | bash -s -- --non-interactive

set -euo pipefail

REPO_URL="https://raw.githubusercontent.com/hendroaritonang/bng-stack/main"
APT_REPO_URL="https://hendroaritonang.github.io/bng-stack"
APT_REPO_DIST="stable"
APT_REPO_COMP="main"
KEYRING_URL="${APT_REPO_URL}/bng-stack-archive-keyring.gpg"
APT_LIST="/etc/apt/sources.list.d/bng-stack.list"
KEYRING_PATH="/usr/share/keyrings/bng-stack-archive-keyring.gpg"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

die()  { echo -e "${RED}FATAL: $*${NC}" >&2; exit 1; }
info() { echo -e "${CYAN}  → $*${NC}"; }
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $*${NC}"; }

NON_INTERACTIVE=0
SKIP_CONFIGURE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --non-interactive) NON_INTERACTIVE=1 ;;
        --skip-configure)  SKIP_CONFIGURE=1  ;;
    esac
    shift
done

# ── banner ─────────────────────────────────────────────────────────────
clear
echo ""
echo -e "${BOLD}${CYAN}"
echo "  ██████╗ ███╗   ██╗ ██████╗     ███████╗████████╗ █████╗  ██████╗██╗  ██╗"
echo "  ██╔══██╗████╗  ██║██╔════╝     ██╔════╝╚══██╔══╝██╔══██╗██╔════╝██║ ██╔╝"
echo "  ██████╔╝██╔██╗ ██║██║  ███╗    ███████╗   ██║   ███████║██║     █████╔╝ "
echo "  ██╔══██╗██║╚██╗██║██║   ██║    ╚════██║   ██║   ██╔══██║██║     ██╔═██╗ "
echo "  ██████╔╝██║ ╚████║╚██████╔╝    ███████║   ██║   ██║  ██║╚██████╗██║  ██╗"
echo "  ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝     ╚══════╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝"
echo -e "${NC}"
echo -e "  ${BOLD}BNG Stack Installer${NC} — VPP + accel-pppd + FRR + bng-monitor"
echo ""

# ── preflight checks ───────────────────────────────────────────────────
info "Checking prerequisites ..."

[ "$(id -u)" -eq 0 ] || die "Must run as root. Use: sudo bash"

# Source os-release into current shell so VERSION_ID is available
ID=""
VERSION_ID=""
[ -f /etc/os-release ] && . /etc/os-release
OS="${ID}${VERSION_ID}"
case "$OS" in
    ubuntu22.04|ubuntu24.04) ok "OS: Ubuntu ${VERSION_ID}" ;;
    *) warn "Untested OS: ${OS:-unknown}. Proceeding anyway..." ;;
esac

# Check VPP is installed (custom build required)
if ! dpkg -l vpp 2>/dev/null | grep -q '^ii'; then
    warn "VPP is not installed."
    echo ""
    echo "  BNG Stack requires the custom VyOS VPP build."
    echo "  Install it first, then re-run this installer."
    echo ""
    echo "  See: https://github.com/hendroaritonang/bng-stack#prerequisites"
    echo ""
    if [ "$NON_INTERACTIVE" != "1" ]; then
        echo -ne "  Continue anyway? [y/N]: "
        # Read from /dev/tty explicitly so it works even when piped via curl | bash
        read -r c </dev/tty; [[ "$c" =~ ^[Yy]$ ]] || exit 0
    fi
fi

for cmd in curl gpg dpkg-deb python3 openssl; do
    command -v "$cmd" >/dev/null 2>&1 || die "Required command not found: $cmd"
done
ok "Prerequisites satisfied"

# ── add apt repository ─────────────────────────────────────────────────
info "Adding bng-stack apt repository ..."

curl -fsSL "$KEYRING_URL" | gpg --dearmor -o "$KEYRING_PATH" 2>/dev/null || {
    # If GPG key not yet available (first publish), create unsigned
    warn "GPG key not available — adding unsigned repo (OK for testing)"
    cat > "$APT_LIST" <<LIST
deb [trusted=yes] ${APT_REPO_URL} ${APT_REPO_DIST} ${APT_REPO_COMP}
LIST
}

if [ -f "$KEYRING_PATH" ]; then
    cat > "$APT_LIST" <<LIST
deb [arch=amd64 signed-by=${KEYRING_PATH}] ${APT_REPO_URL} ${APT_REPO_DIST} ${APT_REPO_COMP}
LIST
fi

apt-get update -qq 2>/dev/null || warn "apt update had warnings (may be ok)"
ok "Repository added"

# ── install packages ───────────────────────────────────────────────────
info "Installing bng-stack-runtime ..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bng-stack-runtime
ok "bng-stack-runtime installed"

info "Installing bng-monitor ..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bng-monitor
ok "bng-monitor installed"

# ── run configure wizard ───────────────────────────────────────────────
if [ "$SKIP_CONFIGURE" = "1" ]; then
    warn "Skipping configuration (--skip-configure). Run bng-stack-configure manually."
else
    echo ""
    echo -e "${BOLD}Running configuration wizard...${NC}"
    echo ""
    if [ "$NON_INTERACTIVE" = "1" ]; then
        bng-stack-configure --non-interactive
    else
        bng-stack-configure
    fi
fi

# ── done ───────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║   BNG Stack installation complete!       ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""
echo "  Commands:"
echo "    bng-stack-configure   — re-run setup wizard"
echo "    bng-stack-start       — start all services"
echo "    bng-stack-status      — check service status"
echo ""
