#!/bin/sh
# Runs inside the test image (see run.sh): an X server, a window manager, xterm
# running herdr, a GTK file drag source and the Linux sensor. xdotool then
# drives a real XDND drag through XTest from the source over xterm and drops
# it there, followed by an ordinary click. check_drag.py compares the sensor's
# output with the ground truth collected here.
set -eu
repo=/repo
out=${OUT:-/out}
mkdir -p "$out"
cd "$HOME"

fail() { echo "run_in_container.sh: $*" >&2; exit 1; }

Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >"$out/xvfb.log" 2>&1 &
xvfb=$!
ready=
for _ in $(seq 100); do
  if xdpyinfo >/dev/null 2>&1; then ready=1; break; fi
  kill -0 "$xvfb" 2>/dev/null || break
  sleep 0.1
done
[ -n "$ready" ] || { cat "$out/xvfb.log" >&2; fail "Xvfb did not come up on $DISPLAY"; }
openbox >"$out/openbox.log" 2>&1 &
sleep 1

touch /tmp/drop-me.txt
xterm -u8 -name herdrxterm -geometry 120x36+400+150 -fa 'DejaVu Sans Mono' -fs 10 -e herdr &
python3 "$repo/tests/linux/drag_source.py" /tmp/drop-me.txt >"$out/source.log" 2>&1 &
ready=
for _ in $(seq 200); do
  if pgrep -x herdr >/dev/null && xdotool search --classname herdrxterm >/dev/null 2>&1 \
      && xdotool search --name drag-source >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.1
done
[ -n "$ready" ] || fail "xterm with herdr, or the drag source, did not appear within 20 s"
sleep 3   # let herdr start its server and draw

python3 "$repo/sensors/linux/build.py" /tmp/herdr-spring-load-sensor-linux
mkfifo /tmp/sensor-stdin
/tmp/herdr-spring-load-sensor-linux --verbose </tmp/sensor-stdin >"$out/sensor.ndjson" 2>"$out/sensor.err" &
sensor=$!
exec 3>/tmp/sensor-stdin   # hold the sensor's stdin open
sleep 1

term=$(xdotool search --classname herdrxterm | head -1)
src=$(xdotool search --name drag-source | head -1)
[ -n "$term" ] && [ -n "$src" ] || fail "no xterm or drag-source window (xterm: '$term', source: '$src')"
eval "$(xdotool getwindowgeometry --shell "$src")"; sx=$((X + WIDTH / 2)); sy=$((Y + HEIGHT / 2))
eval "$(xdotool getwindowgeometry --shell "$term")"; tx=$((X + WIDTH / 2)); ty=$((Y + HEIGHT / 2))
xterm_pid=$(pgrep -x xterm | head -1 || true)
[ -n "$xterm_pid" ] || fail "no xterm process"
client=
for _ in $(seq 50); do
  client=$(ps -o pid=,tty=,comm= --ppid "$xterm_pid" | awk '$3 == "herdr" && $2 ~ /pts/ {print $1, "/dev/" $2; exit}')
  [ -n "$client" ] && break
  sleep 0.1
done
[ -n "$client" ] || fail "no herdr client with a pts below xterm $xterm_pid"
printf '{"window":"%s","xterm_pid":%s,"client_pid":%s,"client_tty":"%s","target":[%s,%s],"source":[%s,%s]}\n' \
  "$term" "$xterm_pid" "${client% *}" "${client#* }" "$tx" "$ty" "$sx" "$sy" >"$out/truth.json"

# The drag: press on the source, move past GTK's drag threshold, glide over xterm,
# dwell, move inside it, release (the drop).
xdotool mousemove "$sx" "$sy"; sleep 0.3
xdotool mousedown 1
for i in $(seq 1 25); do
  xdotool mousemove $((sx + (tx - sx) * i / 25)) $((sy + (ty - sy) * i / 25)); sleep 0.04
done
sleep 0.8
xdotool mousemove $((tx + 60)) $((ty + 40)); sleep 0.6
import -window root "$out/during-drag.png" 2>/dev/null || true
xdotool mouseup 1
sleep 1

# An ordinary click inside xterm, for the calibration event.
xdotool mousemove $((tx - 100)) $((ty - 50)); sleep 0.2
xdotool click 1
sleep 0.6

exec 3>&-   # EOF on stdin: the sensor must exit 0 by itself
for _ in $(seq 30); do kill -0 "$sensor" 2>/dev/null || break; sleep 0.1; done
if kill -0 "$sensor" 2>/dev/null; then echo 'sensor did not exit on stdin EOF' >"$out/sensor-exit.txt"; kill "$sensor"
else wait "$sensor"; echo "sensor exited with $?" >"$out/sensor-exit.txt"; fi
ps -eo pid,ppid,pgid,tpgid,tty,comm,args >"$out/ps.txt"

python3 "$repo/tests/linux/check_drag.py" "$out"
