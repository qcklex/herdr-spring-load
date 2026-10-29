#!/bin/sh
# Build the Linux test image and run the real XDND drag test in it.
#   sh tests/linux/run.sh [OUT_DIR]
# DOCKER_CONTEXT selects the engine (for example colima-springload).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out=${1:-$repo/tests/linux/out}
mkdir -p "$out"
docker build -q -t herdr-spring-load-linux-test "$here" >/dev/null
docker run --rm -v "$repo:/repo:ro" -v "$out:/out" herdr-spring-load-linux-test sh /repo/tests/linux/run_in_container.sh
