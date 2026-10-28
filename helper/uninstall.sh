#!/bin/sh
# Stop and remove the image-drop-watch LaunchAgent.
set -eu

label="dev.qcklex.herdr-image-drop"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$label.plist"
rm -rf "$HOME/Library/Application Support/herdr-image-drop"
echo "image-drop-watch removed (log kept at ~/Library/Logs/herdr-image-drop.log)"
