#!/bin/sh
# Build the Linux test image and run the end-to-end test (tests/linux/e2e.py) in it.
#   sh tests/linux/run_e2e.sh [OUT_DIR]
# DOCKER_CONTEXT selects the engine (for example colima-springload).
# Output is written inside the container and copied out with docker cp, so the
# host uid never has to match the container's user.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out=${1:-$repo/tests/linux/out/e2e}
rm -rf "$out"; mkdir -p "$out"
docker build -q -t herdr-spring-load-linux-test "$here" >/dev/null
name=herdr-spring-load-e2e-$$
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
status=0
docker run --name "$name" -e OUT=/tmp/out -v "$repo:/repo:ro" herdr-spring-load-linux-test \
    python3 /repo/tests/linux/e2e.py || status=$?
docker cp "$name:/tmp/out/." "$out/" || echo "run_e2e.sh: could not copy the container's output to $out" >&2
exit "$status"
