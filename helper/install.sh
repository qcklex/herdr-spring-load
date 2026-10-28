#!/bin/sh
# Build image-drop-watch and run it as a per-user LaunchAgent.
# Usage: sh helper/install.sh [--terminal BUNDLE_ID]...
set -eu

here=$(cd "$(dirname "$0")" && pwd)
label="dev.qcklex.herdr-image-drop"
support="$HOME/Library/Application Support/herdr-image-drop"
bin="$support/image-drop-watch"
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$HOME/Library/Logs/herdr-image-drop.log"

command -v swiftc >/dev/null 2>&1 || {
    echo "swiftc not found; install the Xcode command line tools: xcode-select --install" >&2
    exit 1
}
herdr=$(command -v herdr || true)
[ -n "$herdr" ] || { echo "herdr not found on PATH" >&2; exit 1; }

mkdir -p "$support" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
swiftc -O "$here/DropLogic.swift" "$here/main.swift" -o "$bin"

extra=""
while [ $# -gt 0 ]; do
    case "$1" in
        --terminal) extra="$extra    <string>--terminal</string>
    <string>$2</string>
"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$bin</string>
    <string>--herdr</string>
    <string>$herdr</string>
$extra  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/herdr-image-drop.stderr.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "image-drop-watch is running (log: $log)"
