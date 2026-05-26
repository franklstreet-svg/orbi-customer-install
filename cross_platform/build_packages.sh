#!/usr/bin/env bash
# Build the Windows and Mac install packages.
#
# Output:
#   /home/frank/orbi_web/cross_platform/dist/orbi-windows.zip
#   /home/frank/orbi_web/cross_platform/dist/orbi-mac.zip
#
# These ZIPs are what Frank emails / hands on USB to a customer.
# Customer extracts the ZIP and double-clicks "Install Orbi" inside.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DIST="$HERE/dist"
APP_SRC="$ROOT/customer_install"

mkdir -p "$DIST"

# ------------------------------------------------------------------
# Build the shared "app" directory (same for all OSes)
# ------------------------------------------------------------------
echo "Building shared app/ tree..."
APP_TMP="$DIST/_app_tmp"
rm -rf "$APP_TMP"
mkdir -p "$APP_TMP"
# Python code
cp -r "$APP_SRC/"*.py             "$APP_TMP/"
cp -r "$APP_SRC/modules"          "$APP_TMP/"
cp -r "$APP_SRC/tools"            "$APP_TMP/"
cp -r "$APP_SRC/static"           "$APP_TMP/"
cp    "$APP_SRC/requirements.txt" "$APP_TMP/"
cp    "$APP_SRC/config.json.template" "$APP_TMP/"
# Data templates
mkdir -p "$APP_TMP/data_templates"
cp "$APP_SRC/data/"*.template "$APP_TMP/data_templates/"
# Shared assets
cp -r "$ROOT/pwa"             "$APP_TMP/"
cp -r "$ROOT/owner_dashboard" "$APP_TMP/"
# NOTE: watchdog.py now lives at customer_install/watchdog.py and is
# already pulled in by the "$APP_SRC/*.py" glob above. Removed the
# explicit copy from $ROOT/watchdog/watchdog.py — that path holds an
# older May-24 version that would otherwise overwrite our current one.
# Helper
cp "$HERE/shared/install_helper.py" "$APP_TMP/"
# Strip __pycache__
find "$APP_TMP" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$APP_TMP" -name "*.pyc" -delete 2>/dev/null || true

# ------------------------------------------------------------------
# Optional: copy the bundled 3B model if it exists
# ------------------------------------------------------------------
MODEL_3B="/home/frank/models/Llama-3.2-3B-Instruct-Q4_K_M.gguf"
INCLUDE_MODEL=0
if [ -f "$MODEL_3B" ]; then
    INCLUDE_MODEL=1
    MODEL_SIZE=$(du -h "$MODEL_3B" | cut -f1)
    echo "  Bundling local 3B model ($MODEL_SIZE)"
else
    echo "  Skipping local 3B model (not on dev box) — install will rely on brain + HuggingFace"
fi

# ------------------------------------------------------------------
# Build WINDOWS package
# ------------------------------------------------------------------
echo
echo "Building Windows package..."
WIN_TMP="$DIST/_win_tmp"
rm -rf "$WIN_TMP" "$DIST/orbi-windows.zip"
mkdir -p "$WIN_TMP/app" "$WIN_TMP/bin" "$WIN_TMP/models"
cp -r "$APP_TMP/." "$WIN_TMP/app/"
cp "$HERE/windows/Install Orbi.bat"   "$WIN_TMP/"
cp "$HERE/windows/Uninstall Orbi.bat" "$WIN_TMP/"

# Windows binaries (Frank needs to drop these into cross_platform/bin/windows/)
WIN_BIN="$HERE/bin/windows"
mkdir -p "$WIN_BIN"
for binary in nssm.exe cloudflared.exe llama-server.exe; do
    if [ -f "$WIN_BIN/$binary" ]; then
        cp "$WIN_BIN/$binary" "$WIN_TMP/bin/"
        echo "  ✓ included $binary"
    else
        echo "  ⚠ missing $WIN_BIN/$binary  (download separately — see README)"
    fi
done

[ $INCLUDE_MODEL -eq 1 ] && cp "$MODEL_3B" "$WIN_TMP/models/llama-3.2-3b-instruct-q4.gguf"

# README inside the ZIP
cat > "$WIN_TMP/README.txt" <<'WINREADME'
ORBI FOR WINDOWS
================

To install:
  1. Right-click "Install Orbi.bat"
  2. Choose "Run as administrator"
  3. Follow the prompts

Frank will give you the brain URL, API key, and tunnel URL during onboarding.

To uninstall later:
  Right-click "Uninstall Orbi.bat" → Run as administrator
  Your data folder will be moved to your Desktop as a backup.

If Windows blocks the installer:
  Click "More info" then "Run anyway" — Orbi is not signed yet
  (signing planned for v1.1).

Questions? Email franklstreet@yahoo.com or call 888-616-4997.
WINREADME

python3 -c "
import zipfile, os, sys
src = '$WIN_TMP'
out = '$DIST/orbi-windows.zip'
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as z:
    for root, _, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src)
            z.write(full, rel)
"
rm -rf "$WIN_TMP"
WIN_SIZE=$(du -h "$DIST/orbi-windows.zip" | cut -f1)
echo "  → orbi-windows.zip ($WIN_SIZE)"

# ------------------------------------------------------------------
# Build MAC package
# ------------------------------------------------------------------
echo
echo "Building Mac package..."
MAC_TMP="$DIST/_mac_tmp"
rm -rf "$MAC_TMP" "$DIST/orbi-mac.zip"
mkdir -p "$MAC_TMP/app" "$MAC_TMP/bin" "$MAC_TMP/models"
cp -r "$APP_TMP/." "$MAC_TMP/app/"
cp "$HERE/mac/Install Orbi.command"   "$MAC_TMP/"
cp "$HERE/mac/Uninstall Orbi.command" "$MAC_TMP/"
chmod +x "$MAC_TMP/Install Orbi.command" "$MAC_TMP/Uninstall Orbi.command"

MAC_BIN="$HERE/bin/mac"
mkdir -p "$MAC_BIN"
for binary in cloudflared-mac llama-server-mac; do
    if [ -f "$MAC_BIN/$binary" ]; then
        cp "$MAC_BIN/$binary" "$MAC_TMP/bin/"
        echo "  ✓ included $binary"
    else
        echo "  ⚠ missing $MAC_BIN/$binary  (download separately — see README)"
    fi
done

[ $INCLUDE_MODEL -eq 1 ] && cp "$MODEL_3B" "$MAC_TMP/models/llama-3.2-3b-instruct-q4.gguf"

cat > "$MAC_TMP/README.txt" <<'MACREADME'
ORBI FOR MAC
============

To install:
  1. Double-click "Install Orbi.command"

If macOS says "cannot be opened because it is from an unidentified developer":
  1. Right-click "Install Orbi.command"
  2. Choose "Open"
  3. Click "Open" in the dialog
  4. (Or: System Settings → Privacy & Security → "Open Anyway")

Frank will give you the brain URL, API key, and tunnel URL during onboarding.

To uninstall later:
  Double-click "Uninstall Orbi.command"
  Your data folder will be moved to your Desktop as a backup.

Questions? Email franklstreet@yahoo.com or call 888-616-4997.
MACREADME

python3 -c "
import zipfile, os, sys
src = '$MAC_TMP'
out = '$DIST/orbi-mac.zip'
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as z:
    for root, _, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src)
            z.write(full, rel)
"
rm -rf "$MAC_TMP"
MAC_SIZE=$(du -h "$DIST/orbi-mac.zip" | cut -f1)
echo "  → orbi-mac.zip ($MAC_SIZE)"

# Cleanup shared tmp
rm -rf "$APP_TMP"

echo
echo "============================================"
echo " Packages built:"
echo "   $DIST/orbi-windows.zip ($WIN_SIZE)"
echo "   $DIST/orbi-mac.zip     ($MAC_SIZE)"
echo "============================================"
echo
echo "Next steps:"
if [ $INCLUDE_MODEL -eq 0 ]; then
    echo " • Add /home/frank/models/Llama-3.2-3B-Instruct-Q4_K_M.gguf if you want local fallback bundled"
fi
echo " • Drop Windows binaries (nssm.exe, cloudflared.exe, llama-server.exe) in:"
echo "     $HERE/bin/windows/"
echo " • Drop Mac binaries (cloudflared-mac, llama-server-mac) in:"
echo "     $HERE/bin/mac/"
echo " • Rebuild after dropping binaries: bash $HERE/build_packages.sh"
echo " • Upload ZIPs somewhere customers can download:"
echo "     https://orbi.frank.com/download/orbi-windows.zip"
echo "     https://orbi.frank.com/download/orbi-mac.zip"
