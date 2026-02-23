#!/bin/bash
# setup.sh -- One-shot setup script for AGSHome on Raspberry Pi
#
# Run as the pi user (not root):
#   bash setup.sh
#
# What this does:
#   1. Installs system packages (Python, libcamera dependencies)
#   2. Creates a Python virtual environment
#   3. Installs Python dependencies
#   4. Installs the systemd service for auto-start on boot
#   5. Prompts to configure config.json

set -e

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_FILE="$INSTALL_DIR/agshome.service"
SYSTEMD_DIR="/etc/systemd/system"
VENV_DIR="$INSTALL_DIR/venv"
USERNAME="$(whoami)"

echo ""
echo "============================================"
echo "  AGSHome Pi Setup"
echo "  Install dir: $INSTALL_DIR"
echo "  User: $USERNAME"
echo "============================================"
echo ""

# ---- 1. System packages ----
echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv \
    avahi-daemon avahi-utils

# Ensure Avahi (mDNS) is running
sudo systemctl enable avahi-daemon
sudo systemctl start avahi-daemon
echo "      Avahi mDNS daemon enabled."

# ---- 2. Virtual environment ----
echo "[2/5] Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
echo "      venv created at $VENV_DIR"

# ---- 3. Python dependencies ----
echo "[3/5] Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements-nocamera.txt"
echo "      Dependencies installed."

# ---- 4. systemd service ----
echo "[4/5] Installing systemd service..."

# Patch the service file with the correct user and path
sed \
    -e "s|User=pi|User=$USERNAME|g" \
    -e "s|/home/pi/agshome|$INSTALL_DIR|g" \
    "$SERVICE_FILE" | sudo tee "$SYSTEMD_DIR/agshome.service" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable agshome.service
echo "      systemd service installed and enabled."

# ---- 5. Config ----
echo "[5/5] Checking config.json..."
CONFIG_FILE="$INSTALL_DIR/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    cp "$INSTALL_DIR/config_template.json" "$CONFIG_FILE"
    echo ""
    echo "  *** config.json created from template. ***"
    echo "  Edit it now with your hub credentials:"
    echo "    nano $CONFIG_FILE"
    echo ""
else
    echo "      config.json already exists — skipping."
fi

# ---- Done ----
echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Edit config.json with your hub details:"
echo "       nano $CONFIG_FILE"
echo ""
echo "  2. Start the service:"
echo "       sudo systemctl start agshome"
echo ""
echo "  3. Check it's running:"
echo "       sudo systemctl status agshome"
echo "       journalctl -u agshome -f"
echo ""
echo "  4. Access from your phone:"
echo "       http://agshome.local:5000"
echo "============================================"
echo ""
