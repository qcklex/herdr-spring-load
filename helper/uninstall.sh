#!/bin/sh
# Stop and remove the herdr-spring-load LaunchAgent.
set -eu

label="dev.qcklex.herdr-spring-load"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$label.plist"
rm -rf "$HOME/Library/Application Support/herdr-spring-load"
echo "herdr-spring-load removed (log kept at ~/Library/Logs/herdr-spring-load.log)"
