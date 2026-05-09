#!/usr/bin/env bash
# Run this script once on your Ubuntu Server to install everything.
# Usage: sudo bash install.sh

set -e

INSTALL_DIR="/opt/anti-captcha"

echo "==> Installing base system packages..."
apt-get update -qq
apt-get install -y python3 python3-venv python3-pip

echo "==> Copying project files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r . "$INSTALL_DIR/"

echo "==> Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

echo "==> Installing Playwright browser (Chromium)..."
"$INSTALL_DIR/venv/bin/playwright" install chromium

echo "==> Installing Playwright browser system dependencies (Ubuntu 24.04 compatible)..."
apt-get install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0t64 \
    libatk-bridge2.0-0t64 \
    libatspi2.0-0t64 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2t64 \
    libcups2t64 \
    libglib2.0-0t64 \
    libx11-6 \
    libxcb1 \
    libxext6

echo "==> Installing systemd service..."
cp "$INSTALL_DIR/anticaptcha.service" /etc/systemd/system/anticaptcha.service
systemctl daemon-reload
systemctl enable anticaptcha.service

echo ""
echo "Done! Fill in $INSTALL_DIR/.env then run:"
echo "  sudo systemctl start anticaptcha"
echo "  sudo journalctl -u anticaptcha -f   # to watch logs"
