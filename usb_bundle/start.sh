#!/usr/bin/env bash
# Orbi USB bootstrap — picks the right installer based on what you're doing.
#
# Usage:  sudo bash start.sh
#
# Designed to be re-runnable. Idempotent. Safe to run as many times as needed.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "✗ Run with sudo:  sudo bash $0"
  exit 1
fi

USB_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$USB_DIR/source/orbi_web"
MODELS="$USB_DIR/models"

if [[ ! -d "$SOURCE" ]]; then
  echo "✗ Could not find $SOURCE"
  echo "  This script must run from the root of the Orbi USB drive."
  exit 2
fi

clear
cat <<BANNER
╔════════════════════════════════════════════════════════════════╗
║                      Orbi USB Installer                        ║
║                                                                ║
║  Source:  $SOURCE
║  Models:  $MODELS
╚════════════════════════════════════════════════════════════════╝

What are you installing on this computer?

  1) Brain machine     (Frank's centralized LLM server — Llama-3.1-8B,
                        Cloudflared tunnel, Stripe webhook, admin dashboard)

  2) Customer install  (One small business's Orbi: chat widget, phone,
                        owner dashboard, watchdog, local Llama-3.2-3B)

  3) Both (for testing — install brain + customer on the same machine
                        using different ports)

  4) Quit

BANNER

read -r -p "Pick 1, 2, 3 or 4: " choice
echo

case "$choice" in
  1)
    echo "==> Brain machine install"
    # Pre-stage the 8B model if it's on the drive so the installer skips download
    if [[ -f "$MODELS/llama-3.1-8b-instruct-q6_k.gguf" ]]; then
      echo "  Pre-staging bundled 8B model from USB (saves ~6.5GB download)"
      mkdir -p /opt/orbi-brain/models
      cp -v "$MODELS/llama-3.1-8b-instruct-q6_k.gguf" \
            /opt/orbi-brain/models/
    else
      echo "  (no 8B model on USB — installer will download it from HuggingFace)"
    fi
    exec bash "$SOURCE/brain/install_brain.sh" --source "$SOURCE"
    ;;
  2)
    echo "==> Customer install"
    # Pre-stage the 3B local model if it's on the drive
    if [[ -f "$MODELS/llama-3.2-3b-instruct-q4.gguf" ]]; then
      echo "  Pre-staging bundled 3B local model from USB"
      mkdir -p /opt/orbi/llm_local
      cp -v "$MODELS/llama-3.2-3b-instruct-q4.gguf" \
            /opt/orbi/llm_local/
    else
      echo "  (no 3B model on USB — local-tier fallback will be disabled)"
    fi
    exec bash "$SOURCE/customer_install/install.sh" --source "$SOURCE"
    ;;
  3)
    echo "==> Both (test mode)"
    echo "  Running brain install first..."
    bash "$SOURCE/brain/install_brain.sh" --source "$SOURCE" --no-tunnel
    echo
    echo "  Now running customer install..."
    bash "$SOURCE/customer_install/install.sh" --source "$SOURCE"
    ;;
  4|q|Q|exit)
    echo "  Cancelled. Run this again any time."
    exit 0
    ;;
  *)
    echo "✗ Invalid choice."
    exit 1
    ;;
esac
