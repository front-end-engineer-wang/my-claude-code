#!/usr/bin/env bash
# Build a standalone executable for macOS / Linux.
# Requires: python3 + pip.  Output: dist/coding-assistant
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/3] Installing build tools..."
python3 -m pip install --upgrade pip
python3 -m pip install pyinstaller

echo "[2/3] Installing project dependencies..."
python3 -m pip install -r requirements.txt

echo "[3/3] Building executable..."
python3 -m PyInstaller --noconfirm --clean --onefile --console \
    --name coding-assistant \
    --collect-all coding_assistant \
    code.py

echo "Done! Executable is at: dist/coding-assistant"
