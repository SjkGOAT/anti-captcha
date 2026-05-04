#!/usr/bin/env bash
# Run this script once on your Ubuntu Server to install everything.
# Usage: sudo bash install.sh

set -e

INSTALL_DIR="/opt/anti-captcha"

echo "==> Installing system dependencies..."
apt-get update -qq
apt-get install -y python3 python3-venv python3-pip \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2

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
