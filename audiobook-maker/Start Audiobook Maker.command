#!/usr/bin/env bash
#
# Start Audiobook Maker.command
#
# Double-click this file in Finder to launch the app — no need to open
# Terminal or type any commands. macOS may ask for confirmation the first
# time you open it (right-click → Open handles that, see README).
#
# This just calls run.sh, which handles all setup automatically.
#
cd "$(dirname "$0")"
./run.sh

# Keep the window open after the app closes (or if something failed) so
# any messages are visible instead of the window vanishing instantly.
echo
echo "Press any key to close this window..."
read -n 1 -s
