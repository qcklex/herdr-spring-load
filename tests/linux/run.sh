#!/bin/sh
# Build the Linux test image and run the real XDND drag test in it.
#   sh tests/linux/run.sh [OUT_DIR]
# DOCKER_CONTEXT selects the engine (for example colima-springload).
# The container writes its output inside itself and docker cp copies it out
# afterwards, so the host uid (CI runners, macOS) never has to match the
# container's user.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out=${1:-$repo/tests/linux/out}
mkdir -p "$out"
docker build -q -t herdr-spring-load-linux-test "$here" >/dev/null
name=herdr-spring-load-drag-$$
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
status=0
docker run --name "$name" -e OUT=/tmp/out -v "$repo:/repo:ro" herdr-spring-load-linux-test \
    sh /repo/tests/linux/run_in_container.sh || status=$?
docker cp "$name:/tmp/out/." "$out/" || echo "run.sh: could not copy the container's output to $out" >&2
exit "$status"
