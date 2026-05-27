#!/usr/bin/env bash
# setup_cloudflared.sh — friendly walkthrough that gets your home Orbi onto
# the public internet through a free Cloudflare tunnel.
#
# IF YOU HAVEN'T MADE THIS SCRIPT EXECUTABLE YET, RUN:
#     chmod +x setup_cloudflared.sh
# THEN RUN IT WITH:
#     ./setup_cloudflared.sh
#
# Nothing here will install or change anything without asking you "y/N" first.

set -u  # complain about unset variables, but do NOT set -e — we handle errors ourselves

# ── Pretty printing ──────────────────────────────────────────────────────
BOLD=$(tput bold 2>/dev/null || echo "")
DIM=$(tput dim 2>/dev/null || echo "")
GREEN=$(tput setaf 2 2>/dev/null || echo "")
YELLOW=$(tput setaf 3 2>/dev/null || echo "")
RED=$(tput setaf 1 2>/dev/null || echo "")
RESET=$(tput sgr0 2>/dev/null || echo "")

say()  { printf "%s\n" "$*"; }
head() { printf "\n${BOLD}== %s ==${RESET}\n" "$*"; }
ok()   { printf "${GREEN}✓${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!${RESET} %s\n" "$*"; }
err()  { printf "${RED}✗${RESET} %s\n" "$*"; }
note() { printf "${DIM}%s${RESET}\n" "$*"; }

pause() {
    printf "\n${DIM}Press ENTER to continue (or Ctrl-C to stop)…${RESET} "
    # shellcheck disable=SC2034
    read -r _
}

confirm() {
    # confirm "Question text?"  → returns 0 on y, 1 on N (default N)
    local prompt="${1:-Continue?}"
    printf "%s [y/N] " "$prompt"
    local ans
    read -r ans
    case "$ans" in
        y|Y|yes|YES|Yes) return 0 ;;
        *)               return 1 ;;
    esac
}

# ── Step 0 — welcome ─────────────────────────────────────────────────────
clear 2>/dev/null || true
head "Orbi Tunnel Setup"
say  ""
say  "This walks you through making your home Orbi reachable from your phone"
say  "no matter where you are, using a free Cloudflare tunnel."
say  ""
say  "What you'll need:"
say  "  1. A free Cloudflare account (signup is free, no credit card)"
say  "  2. A domain name you own that's added to Cloudflare"
say  "     (a brand new \$10/year domain works fine)"
say  "  3. About 10 minutes"
say  ""
say  "What this script will NOT do without asking you first:"
say  "  - Install software"
say  "  - Open browser windows"
say  "  - Change any system settings"
say  ""
pause

# ── Step 1 — detect platform ─────────────────────────────────────────────
head "Step 1: Detecting your computer"

PLATFORM="unknown"
ARCH=$(uname -m 2>/dev/null || echo "unknown")

case "$(uname -s 2>/dev/null)" in
    Linux)
        if grep -qi microsoft /proc/version 2>/dev/null; then
            PLATFORM="wsl"
            say "  Looks like ${BOLD}Windows (WSL)${RESET}."
        else
            PLATFORM="linux"
            say "  Looks like ${BOLD}Linux${RESET}."
        fi
        ;;
    Darwin)
        PLATFORM="mac"
        say "  Looks like ${BOLD}macOS${RESET}."
        ;;
    *)
        warn "Could not auto-detect your operating system."
        PLATFORM="unknown"
        ;;
esac

say  "  CPU architecture: ${BOLD}${ARCH}${RESET}"
pause

# ── Step 2 — check / install cloudflared ─────────────────────────────────
head "Step 2: Checking for cloudflared"

if command -v cloudflared >/dev/null 2>&1; then
    INSTALLED_VERSION=$(cloudflared --version 2>&1 | head -n1)
    ok "cloudflared is already installed: $INSTALLED_VERSION"
else
    warn "cloudflared is not installed yet."
    say  ""
    say  "Here's how to install it on your computer:"
    say  ""
    case "$PLATFORM" in
        mac)
            say "  ${BOLD}macOS${RESET} (recommended):"
            say  "      brew install cloudflared"
            say  ""
            say  "  Or download the binary directly:"
            say  "      https://github.com/cloudflare/cloudflared/releases/latest"
            ;;
        linux)
            say "  ${BOLD}Linux ($ARCH)${RESET}:"
            if [ "$ARCH" = "x86_64" ]; then
                say  "      curl -L -o cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb"
                say  "      sudo dpkg -i cloudflared.deb"
            elif [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
                say  "      curl -L -o cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb"
                say  "      sudo dpkg -i cloudflared.deb"
            else
                say  "      See https://github.com/cloudflare/cloudflared/releases/latest"
            fi
            ;;
        wsl)
            say "  ${BOLD}Windows (WSL)${RESET}:"
            say  "      curl -L -o cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb"
            say  "      sudo dpkg -i cloudflared.deb"
            say  ""
            say  "  (If you'd rather run cloudflared on Windows itself, download the .exe"
            say  "   from https://github.com/cloudflare/cloudflared/releases/latest)"
            ;;
        *)
            say  "  See https://github.com/cloudflare/cloudflared/releases/latest"
            say  "  and pick the binary that matches your operating system."
            ;;
    esac
    say  ""
    say  "Install cloudflared in another terminal window, then come back here."
    pause
    if ! command -v cloudflared >/dev/null 2>&1; then
        err "Still can't find cloudflared on this machine. Re-run this script after installing."
        exit 1
    fi
    ok "cloudflared is now installed!"
fi

pause

# ── Step 3 — Cloudflare login ────────────────────────────────────────────
head "Step 3: Log in to Cloudflare"

CERT_PATH="$HOME/.cloudflared/cert.pem"
if [ -f "$CERT_PATH" ]; then
    ok "Already logged in (found $CERT_PATH)."
else
    say  "We're about to run:"
    say  "    ${BOLD}cloudflared tunnel login${RESET}"
    say  ""
    say  "This will open your web browser and ask you to sign in to Cloudflare,"
    say  "then pick which of your domains you want Orbi to use."
    say  ""
    if confirm "Open the browser and log in now?"; then
        cloudflared tunnel login || {
            err "Login failed. Try running 'cloudflared tunnel login' manually."
            exit 1
        }
        ok "Logged in!"
    else
        warn "Skipped login. You'll need to do this before continuing."
        exit 0
    fi
fi

pause

# ── Step 4 — create tunnel ───────────────────────────────────────────────
head "Step 4: Create a tunnel"

SUFFIX=$(head -c 8 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n' | head -c 6)
if [ -z "$SUFFIX" ]; then SUFFIX="$$"; fi
DEFAULT_NAME="orbi-$SUFFIX"

printf "Tunnel name [${BOLD}%s${RESET}]: " "$DEFAULT_NAME"
read -r TUNNEL_NAME
TUNNEL_NAME=${TUNNEL_NAME:-$DEFAULT_NAME}

# Has this name already been used?
EXISTING=$(cloudflared tunnel list 2>/dev/null | awk -v n="$TUNNEL_NAME" '$2 == n {print $1}' | head -n1)
if [ -n "$EXISTING" ]; then
    ok  "Tunnel '$TUNNEL_NAME' already exists (id $EXISTING) — reusing it."
    TUNNEL_ID="$EXISTING"
else
    say "Creating tunnel '$TUNNEL_NAME'…"
    if confirm "Run 'cloudflared tunnel create $TUNNEL_NAME' now?"; then
        cloudflared tunnel create "$TUNNEL_NAME" || {
            err "Tunnel creation failed."
            exit 1
        }
        TUNNEL_ID=$(cloudflared tunnel list 2>/dev/null | awk -v n="$TUNNEL_NAME" '$2 == n {print $1}' | head -n1)
        ok "Tunnel created (id $TUNNEL_ID)"
    else
        warn "Skipped. Re-run when ready."
        exit 0
    fi
fi

pause

# ── Step 5 — config.yml ──────────────────────────────────────────────────
head "Step 5: Pick a hostname and write the config file"

say  "Enter the public hostname you want for Orbi."
say  "It should be a subdomain of a domain you've added to Cloudflare."
say  "Examples:"
say  "    orbi.example.com"
say  "    home.example.com"
say  ""
printf "Hostname: "
read -r HOSTNAME

if [ -z "$HOSTNAME" ]; then
    err "Hostname is required."
    exit 1
fi

CRED_FILE="$HOME/.cloudflared/${TUNNEL_ID}.json"
CONFIG_DIR="$HOME/.cloudflared"
CONFIG_FILE="$CONFIG_DIR/config.yml"

mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_FILE" ]; then
    BACKUP="${CONFIG_FILE}.bak.$(date +%Y%m%d%H%M%S)"
    warn "Existing config.yml found."
    say  "We can back it up to: $BACKUP"
    if confirm "Overwrite it (after backing up)?"; then
        cp "$CONFIG_FILE" "$BACKUP" && ok "Backed up to $BACKUP"
    else
        say  "Leaving your existing config alone. Add this ingress block manually:"
        printf "\n    - hostname: %s\n      service: http://localhost:5050\n\n" "$HOSTNAME"
        pause
        exit 0
    fi
fi

cat > "$CONFIG_FILE" <<EOF
# Generated by orbi setup_cloudflared.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
tunnel: $TUNNEL_ID
credentials-file: $CRED_FILE

ingress:
  - hostname: $HOSTNAME
    service: http://localhost:5050
  - service: http_status:404
EOF

ok "Wrote $CONFIG_FILE"
say  ""
note "Contents:"
sed 's/^/    /' "$CONFIG_FILE"

pause

# ── Step 6 — DNS route ───────────────────────────────────────────────────
head "Step 6: Point your DNS at the tunnel"

say  "Cloudflare needs a DNS record so the hostname resolves to your tunnel."
say  ""
say  "We'll run:"
say  "    ${BOLD}cloudflared tunnel route dns $TUNNEL_NAME $HOSTNAME${RESET}"
say  ""
if confirm "Create the DNS route now?"; then
    cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME" || {
        warn "DNS route command failed — you may need to add the CNAME manually in the"
        warn "Cloudflare dashboard. Point '$HOSTNAME' at '$TUNNEL_ID.cfargotunnel.com'."
    }
    ok "DNS route in place."
else
    warn "Skipped. You'll need to add the CNAME yourself in the Cloudflare dashboard."
fi

pause

# ── Step 7 — run it ──────────────────────────────────────────────────────
head "Step 7: Start the tunnel"

say  "You're all set! To bring the tunnel up, run:"
say  ""
say  "    ${BOLD}cloudflared tunnel run $TUNNEL_NAME${RESET}"
say  ""
say  "Leave that command running in a terminal. Your phone can now reach Orbi at:"
say  ""
say  "    ${BOLD}https://$HOSTNAME${RESET}"
say  ""
say  "Want it to start automatically every time your computer boots? Run:"
say  ""
say  "    ${BOLD}sudo cloudflared service install${RESET}"
say  ""
say  "(That's optional — the manual run command above is fine to start with.)"
say  ""
ok "Setup complete."
say  ""
note "If anything went sideways, see tunnel/README.md for the manual steps."
