#!/usr/bin/env bash
#
# Orbi customer install script.
#
# Run as root on a fresh Ubuntu 24.04 LTS box.
# Assumes /home/frank/orbi_web (or wherever the source lives) is reachable.
#
# Usage:
#   sudo ./install.sh [--source /path/to/orbi_web] [--token <install_token>]
#
# What it does:
#   1. Installs system deps (python3, pip, cloudflared, ffmpeg, sqlite3, tar)
#   2. Creates the 'orbi' system user
#   3. Copies files into /opt/orbi/
#   4. Installs Python deps
#   5. Initializes data/ from templates
#   6. If --token is given, verifies it against brain.twickell.com
#      and pre-populates the api_key + owner_email automatically
#   7. Prompts for owner email + password and writes them into config.json
#   8. Prompts for brain URL, API key, HuggingFace token (skipped if token used)
#   9. Installs systemd units for orbi + watchdog
#  10. Starts everything
#  11. Prints the health-check URL
#
# The --token flag is the bridge from this bash installer to the newer
# Stripe-checkout flow. It lets Frank (or a customer) run this script with
# the token they received in their Stripe receipt email — no manual brain
# URL/API-key pasting.
#
# Exit codes:
#   0  success
#   1  must run as root
#   2  source path not found
#   3  install step failed

set -euo pipefail

SOURCE="/home/frank/orbi_web"
INSTALL_DIR="/opt/orbi"
ORBI_USER="orbi"
INSTALL_TOKEN=""
BILLING_URL="${ORBI_BILLING_URL:-https://brain.twickell.com}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --token)  INSTALL_TOKEN="$2"; shift 2 ;;
    --help|-h)
      sed -n '1,40p' "$0"
      exit 0 ;;
    *) echo "Unknown argument: $1"; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "✗ This script must be run as root (use sudo)."
  exit 1
fi

if [[ ! -d "$SOURCE/customer_install" ]]; then
  echo "✗ Source not found at $SOURCE/customer_install"
  echo "  Pass --source /path/to/orbi_web"
  exit 2
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
step() { echo; echo "▶ $1"; }
ok()   { echo "  ✓ $1"; }
warn() { echo "  ⚠ $1"; }
fail() { echo "  ✗ $1"; exit 3; }

# ---------------------------------------------------------------------------
# 1. System deps
# ---------------------------------------------------------------------------
step "Installing system dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-pip python3-venv \
  sqlite3 tar curl wget ffmpeg \
  ca-certificates lsb-release \
  > /dev/null
ok "apt packages installed"

# cloudflared (separate install since it's not in the default Ubuntu repos)
if ! command -v cloudflared >/dev/null 2>&1; then
  step "Installing cloudflared"
  mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | \
    tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
    | tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
  apt-get update -qq
  apt-get install -y -qq cloudflared >/dev/null
  ok "cloudflared installed"
else
  ok "cloudflared already installed"
fi

# ---------------------------------------------------------------------------
# 2. Create orbi user
# ---------------------------------------------------------------------------
step "Creating orbi user"
if ! id "$ORBI_USER" >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin -d "$INSTALL_DIR" "$ORBI_USER"
  ok "user '$ORBI_USER' created"
else
  ok "user '$ORBI_USER' already exists"
fi

# ---------------------------------------------------------------------------
# 3. Copy files
# ---------------------------------------------------------------------------
step "Copying files to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# Customer install Python code
cp -r "$SOURCE/customer_install/"*.py        "$INSTALL_DIR/"
cp -r "$SOURCE/customer_install/modules"     "$INSTALL_DIR/"
cp -r "$SOURCE/customer_install/tools"       "$INSTALL_DIR/"
cp -r "$SOURCE/customer_install/static"      "$INSTALL_DIR/"
cp -r "$SOURCE/customer_install/desktop_shortcuts" "$INSTALL_DIR/"
cp    "$SOURCE/customer_install/requirements.txt" "$INSTALL_DIR/"
# Shared assets
cp -r "$SOURCE/pwa"               "$INSTALL_DIR/"
cp -r "$SOURCE/owner_dashboard"   "$INSTALL_DIR/"
cp    "$SOURCE/watchdog/watchdog.py" "$INSTALL_DIR/"
# Empty dirs
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/snapshots" "$INSTALL_DIR/llm_local" "$INSTALL_DIR/tunnel"
# Data templates → real files if missing
for f in business_info memory notes messages; do
  if [[ ! -f "$INSTALL_DIR/data/${f}.json" ]]; then
    cp "$SOURCE/customer_install/data/${f}.json.template" "$INSTALL_DIR/data/${f}.json"
  fi
done
# Config template → real config if missing
if [[ ! -f "$INSTALL_DIR/config.json" ]]; then
  cp "$SOURCE/customer_install/config.json.template" "$INSTALL_DIR/config.json"
fi
chown -R "$ORBI_USER:$ORBI_USER" "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR/data"
chmod 600 "$INSTALL_DIR/config.json"
ok "files in place"

# ---------------------------------------------------------------------------
# 4. Python deps
# ---------------------------------------------------------------------------
step "Installing Python dependencies"
pip3 install --quiet --break-system-packages -r "$INSTALL_DIR/requirements.txt" \
  || pip3 install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "python deps installed"

# ---------------------------------------------------------------------------
# 5a. Optional: verify install token from Stripe webhook
# ---------------------------------------------------------------------------
TOKEN_API_KEY=""
TOKEN_OWNER_EMAIL=""
TOKEN_TIER=""
if [[ -n "$INSTALL_TOKEN" ]]; then
  step "Verifying install token with $BILLING_URL"
  # Token should look like inst_<32 hex>. Reject obvious garbage early.
  if [[ ! "$INSTALL_TOKEN" =~ ^[A-Za-z0-9_]{16,80}$ ]]; then
    fail "install token has invalid shape (expected inst_<hex32>)"
  fi
  TOKEN_RESP=$(curl -sS -m 15 -w "\n%{http_code}" \
    "${BILLING_URL%/}/api/verify/${INSTALL_TOKEN}") || \
    fail "could not reach billing service at $BILLING_URL"
  TOKEN_BODY=$(echo "$TOKEN_RESP" | sed '$d')
  TOKEN_CODE=$(echo "$TOKEN_RESP" | tail -n1)
  if [[ "$TOKEN_CODE" != "200" ]]; then
    fail "billing rejected token (HTTP $TOKEN_CODE) — already used or invalid"
  fi
  # Parse with python so we don't need jq on a barebones box
  TOKEN_API_KEY=$(echo "$TOKEN_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_key',''))")
  TOKEN_OWNER_EMAIL=$(echo "$TOKEN_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('owner_email',''))")
  TOKEN_TIER=$(echo "$TOKEN_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tier',''))")
  if [[ -z "$TOKEN_API_KEY" || -z "$TOKEN_OWNER_EMAIL" ]]; then
    fail "billing response missing api_key/owner_email"
  fi
  ok "token verified — owner=$TOKEN_OWNER_EMAIL tier=$TOKEN_TIER"
fi

# ---------------------------------------------------------------------------
# 5 + 6 + 7. Onboarding prompts (owner credentials + brain + HF)
# ---------------------------------------------------------------------------
if [[ ! -t 0 ]]; then
  warn "No interactive terminal — skipping onboarding prompts."
  warn "Edit $INSTALL_DIR/config.json manually then run:"
  warn "  sudo systemctl restart orbi orbi-watchdog"
else
  step "Onboarding — fill in owner credentials and brain settings"
  read -r -p "  Business name: " BIZ_NAME
  if [[ -n "$TOKEN_OWNER_EMAIL" ]]; then
    OWNER_EMAIL="$TOKEN_OWNER_EMAIL"
    echo "  Owner email (from install token): $OWNER_EMAIL"
  else
    read -r -p "  Owner email: " OWNER_EMAIL
  fi
  while true; do
    read -r -s -p "  Owner password (8+ chars): " OWNER_PW; echo
    [[ ${#OWNER_PW} -ge 8 ]] && break
    echo "  Password must be at least 8 characters."
  done
  read -r -p "  Brain URL (e.g. https://brain.twickell.com) [skip]: " BRAIN_URL
  if [[ -n "$TOKEN_API_KEY" ]]; then
    BRAIN_KEY="$TOKEN_API_KEY"
    echo "  Brain API key (from install token): ${BRAIN_KEY:0:14}…"
  else
    read -r -p "  Brain API key (from Stripe webhook) [skip]: " BRAIN_KEY
  fi
  read -r -p "  HuggingFace token (hf_...) [skip]: " HF_TOKEN
  read -r -p "  Twilio phone number (e.g. +14155551234) [skip]: " TWILIO_NUM
  read -r -p "  Tunnel URL for this customer (e.g. https://joes.orbi.frank.com) [skip]: " TUNNEL_URL

  # Update config.json via python so JSON stays valid
  PYTHONPATH="$INSTALL_DIR" sudo -u "$ORBI_USER" python3 - <<PYEOF
import json, sys, time
sys.path.insert(0, "$INSTALL_DIR")
import auth

with open("$INSTALL_DIR/config.json", "r") as f:
    cfg = json.load(f)

if "$BIZ_NAME":
    cfg.setdefault("business", {})["name"] = "$BIZ_NAME"
cfg.setdefault("owner", {})["email"] = "$OWNER_EMAIL"
cfg["owner"]["name"] = cfg["owner"].get("name") or "$OWNER_EMAIL".split("@")[0].title()
cfg["owner"]["_password_hash"] = auth.hash_password("$OWNER_PW")

if "$BRAIN_URL":
    cfg.setdefault("brain", {})["url"] = "$BRAIN_URL"
if "$BRAIN_KEY":
    cfg.setdefault("brain", {})["api_key"] = "$BRAIN_KEY"
if "$HF_TOKEN":
    cfg.setdefault("huggingface", {})["api_key"] = "$HF_TOKEN"
if "$TWILIO_NUM":
    cfg.setdefault("phone", {})["twilio_number"] = "$TWILIO_NUM"
if "$TUNNEL_URL":
    cfg.setdefault("server", {})["tunnel_url"] = "$TUNNEL_URL"

cfg["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

# Seed business_info.json with the name too
try:
    with open("$INSTALL_DIR/data/business_info.json", "r") as f:
        biz = json.load(f)
    if "$BIZ_NAME":
        biz["name"] = "$BIZ_NAME"
        with open("$INSTALL_DIR/data/business_info.json", "w") as f:
            json.dump(biz, f, indent=2)
except Exception as e:
    print(f"warning: could not update business_info.json: {e}")

with open("$INSTALL_DIR/config.json", "w") as f:
    json.dump(cfg, f, indent=2)
print("config.json written")
PYEOF
  ok "config saved"
fi

# ---------------------------------------------------------------------------
# 8. Systemd units
# ---------------------------------------------------------------------------
step "Installing systemd units"
cp "$SOURCE/watchdog/orbi.service"          /etc/systemd/system/
cp "$SOURCE/watchdog/orbi-watchdog.service" /etc/systemd/system/
chmod 644 /etc/systemd/system/orbi*.service
systemctl daemon-reload
ok "systemd units installed"

# ---------------------------------------------------------------------------
# 9. Start
# ---------------------------------------------------------------------------
step "Starting services"
systemctl enable --now orbi.service orbi-watchdog.service
sleep 3
if systemctl is-active --quiet orbi.service; then ok "orbi running"
else fail "orbi failed to start — see: journalctl -u orbi -n 50"; fi
if systemctl is-active --quiet orbi-watchdog.service; then ok "watchdog running"
else fail "watchdog failed to start — see: journalctl -u orbi-watchdog -n 50"; fi

# ---------------------------------------------------------------------------
# 10. Verify
# ---------------------------------------------------------------------------
step "Verifying"
sleep 1
if curl -sf http://127.0.0.1:5050/health > /tmp/orbi_health.json; then
  ok "/health returned 200"
  cat /tmp/orbi_health.json
  echo
else
  warn "health endpoint not responding yet — try in a few seconds"
fi

# ---------------------------------------------------------------------------
# 11. Create desktop shortcut (so the customer has a clickable icon)
# ---------------------------------------------------------------------------
step "Creating desktop shortcut for owner"
# Find the desktop user (not root) — usually the human who'll click the icon
DESKTOP_USER=""
for u in $(getent passwd | awk -F: '$3 >= 1000 && $3 < 65000 && $7 ~ /(bash|zsh|sh)$/ {print $1}'); do
    if [[ -d "/home/$u/Desktop" || -d "/home/$u" ]]; then
        DESKTOP_USER="$u"
        break
    fi
done
if [[ -n "$DESKTOP_USER" ]]; then
    sudo -u "$DESKTOP_USER" \
        ORBI_URL="${TUNNEL_URL:-http://localhost:5050/}" \
        bash "$INSTALL_DIR/desktop_shortcuts/install-shortcut-linux.sh" || \
        warn "shortcut install failed — you can create it manually later"
    ok "desktop shortcut created for user '$DESKTOP_USER'"
else
    warn "no human user found — skipping desktop shortcut"
fi

cat <<MSG

╔════════════════════════════════════════════════════════════════╗
║  Orbi is installed and running on this box.                   ║
╠════════════════════════════════════════════════════════════════╣
║  Status:        systemctl status orbi orbi-watchdog            ║
║  Logs (live):   journalctl -u orbi -f                          ║
║  Logs (watch):  journalctl -u orbi-watchdog -f                 ║
║  Health check:  curl http://127.0.0.1:5050/health              ║
║  Owner login:   http://127.0.0.1:5050/owner/login              ║
║  Customer chat: http://127.0.0.1:5050/                         ║
║                                                                ║
║  Next steps:                                                   ║
║    1. Set up cloudflared tunnel to expose this box publicly    ║
║    2. Configure Twilio voice webhook to <tunnel>/voice/incoming║
║    3. Log into the owner dashboard and fill in business info   ║
╚════════════════════════════════════════════════════════════════╝
MSG
