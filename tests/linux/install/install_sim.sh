#!/bin/sh
# Run as tester inside the systemd container (tests/linux/run_install.sh).
# Simulates herdr plugin install on Linux: a fresh copy of the plugin, the
# manifest's [[build]] for linux, the service, the status action, removal.
set -u
export XDG_RUNTIME_DIR=/run/user/$(id -u) LIBGL_ALWAYS_SOFTWARE=1
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
cd "$HOME"
support=$HOME/.local/share/herdr-spring-load
unit=$HOME/.config/systemd/user/dev.qcklex.herdr-spring-load.service
echo "== scratch HOME: $HOME ($(ls -A "$HOME" | tr '\n' ' '))"
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 1; openbox >/tmp/openbox.log 2>&1 &
sleep 1; alacritty -e herdr >/tmp/alacritty.log 2>&1 &
for i in $(seq 60); do [ -S "$HOME/.config/herdr/herdr.sock" ] && break; sleep 0.5; done
echo "herdr socket: $(ls "$HOME/.config/herdr/herdr.sock" 2>&1)"
cp -r /repo "$HOME/plugin" && rm -rf "$HOME/plugin/.git" "$HOME/plugin/tests/linux/out"
cd "$HOME/plugin"
echo "== [[build]] for linux: $(python3 -c 'import tomllib;b=[b for b in tomllib.load(open("herdr-plugin.toml","rb"))["build"] if "linux" in b["platforms"]];print(b[0]["command"])')"
sh scripts/build.sh; echo "exit=$?"
echo "== unit $unit"; cat "$unit"
echo "== systemctl --user is-active: $(systemctl --user is-active dev.qcklex.herdr-spring-load.service)  is-enabled: $(systemctl --user is-enabled dev.qcklex.herdr-spring-load.service)"
sleep 3
echo "== processes (user manager: $(pgrep -u tester -x systemd))"; ps -eo pid,ppid,user,args | grep -E 'spring_load|sensor-linux' | grep -v grep
echo "== status action"; sh scripts/build.sh --status; echo "exit=$?"
echo "== core log"; cat "$support/herdr-spring-load.log"
echo "== re-run [[build]] (update in place)"; sh scripts/build.sh >/dev/null; echo "exit=$?  active: $(systemctl --user is-active dev.qcklex.herdr-spring-load.service)"
echo "== remove-helper action"; sh helper/uninstall.sh; echo "exit=$?"
echo "after removal: unit $(test -e "$unit" && echo present || echo gone), active: $(systemctl --user is-active dev.qcklex.herdr-spring-load.service), core $(pgrep -f 'spring_load run' >/dev/null && echo running || echo stopped)"
