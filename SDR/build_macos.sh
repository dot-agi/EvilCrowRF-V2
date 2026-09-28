#!/bin/bash
# Build a self-contained app for this Mac's architecture.
set -euo pipefail
cd "$(dirname "$0")"
python_bin="${PYTHON:-../.venv/bin/python}"
"$python_bin" -c 'import tkinter, serial, numpy, esptool, PyInstaller'
export PYINSTALLER_CONFIG_DIR="$PWD/build/pyinstaller-cache"
"$python_bin" -m PyInstaller --noconfirm --clean --windowed --onedir \
  --name 'EvilCrow SDR' --osx-bundle-identifier org.evilcrow.sdr \
  --icon assets/EvilCrow.icns \
  --hidden-import evilcrow_sdr --hidden-import urh_bridge \
  --hidden-import gnuradio_source --hidden-import serial_utils \
  --hidden-import radio_integrations --add-data 'integration_templates:integration_templates' \
  --hidden-import firmware_tool --hidden-import usb_backend --collect-data esptool \
  sdr_launcher.py
echo "Built: $PWD/dist/EvilCrow SDR.app"
