#!/bin/sh
# Build the Linux test image and run the end-to-end test (tests/linux/e2e.py) in it.
#   sh tests/linux/run_e2e.sh [OUT_DIR]
# DOCKER_CONTEXT selects the engine (for example colima-springload).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out=${1:-$repo/tests/linux/out/e2e}
rm -rf "$out"; mkdir -p "$out"
docker build -q -t herdr-spring-load-linux-test "$here" >/dev/null
docker run --rm -v "$repo:/repo:ro" -v "$out:/out" herdr-spring-load-linux-test python3 /repo/tests/linux/e2e.py
