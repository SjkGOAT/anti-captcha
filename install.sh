#!/usr/bin/env bash
# Run this script once on your Ubuntu Server to install everything.
# Usage: sudo bash install.sh

set -e

INSTALL_DIR="/opt/anti-captcha"

echo "==> Installing system dependencies..."
apt-get update -qq
# Ubuntu 24.04 renamed libasound2 to libasound2t64 — pick whichever apt knows about
if apt-cache show libasound2t64 &>/dev/null 2>&1; then
    LIBASOUND="libasound2t64"
else
    LIBASOUND="libasound2"
fi

apt-get install -y python3 python3-venv python3-pip \
    libnss3 libnspr4 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 "$LIBASOUND"

echo "==> Copying project files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r . "$INSTALL_DIR/"

echo "==> Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

echo "==> Installing Playwright browser (Chromium)..."
"$INSTALL_DIR/venv/bin/playwright" install chromium

echo "==> Installing systemd service..."
cp "$INSTALL_DIR/anticaptcha.service" /etc/systemd/system/anticaptcha.service
systemctl daemon-reload
systemctl enable anticaptcha.service

echo ""
echo "Done! Fill in $INSTALL_DIR/.env then run:"
echo "  sudo systemctl start anticaptcha"
echo "  sudo journalctl -u anticaptcha -f   # to watch logs"
