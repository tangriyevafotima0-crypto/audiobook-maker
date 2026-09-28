#!/usr/bin/env bash
#
# build_mac.sh -- one-time developer build of the packaged desktop app
# (macOS). Produces dist/AudiobookMaker.app, which end users can copy
# anywhere (e.g. the Desktop) and double-click -- no Python, pip, or
# ffmpeg setup required on their machine.
#
# This is a dev-only build script, not something end users run. It does
# not touch run.sh/run.bat/the .command file, or the venv those use --
# this is an additional, separate distribution path.
#
set -euo pipefail
cd "$(dirname "$0")"

BUILD_VENV=".build_venv"

echo "==> Creating a clean build virtual environment ($BUILD_VENV)..."
rm -rf "$BUILD_VENV"
python3 -m venv "$BUILD_VENV"
# shellcheck disable=SC1091
source "$BUILD_VENV/bin/activate"

echo "==> Installing dependencies (app requirements + PyInstaller + bundled ffmpeg)..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
pip install --quiet pyinstaller imageio-ffmpeg

echo "==> Running PyInstaller (onedir, macOS .app bundle)..."
rm -rf build dist
pyinstaller --noconfirm audiobook_maker_mac.spec

deactivate

echo
echo "==> Build complete."
echo "    App bundle: $(pwd)/dist/AudiobookMaker.app"
echo "    Copy AudiobookMaker.app anywhere (e.g. the Desktop) and double-click it."
echo "    First launch: right-click -> Open (it's unsigned -- same one-time"
echo "    step as the existing 'Start Audiobook Maker.command' file)."
