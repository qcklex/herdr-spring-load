#!/bin/sh
# Simulated Linux install: systemd container (tests/linux/install/), fresh HOME,
# the manifest's [[build]] for linux, the systemd user unit, the status action.
#   sh tests/linux/run_install.sh        (DOCKER_CONTEXT selects the engine)
# Also runs [[build]] in the plain image, which has no systemd user manager.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
docker build -q -t herdr-spring-load-linux-test "$here" >/dev/null
docker build -q -t herdr-spring-load-linux-install "$here/install" >/dev/null
name=spring-load-install-$$
docker run -d --name "$name" --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    --tmpfs /run --tmpfs /run/lock -v "$repo:/repo:ro" herdr-spring-load-linux-install >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1' EXIT
for i in $(seq 60); do
    docker exec "$name" test -S /run/user/1000/bus 2>/dev/null && break; sleep 1
done
docker exec -u tester "$name" sh /repo/tests/linux/install/install_sim.sh
echo "== without a systemd user manager (plain image)"
docker run --rm -v "$repo:/repo:ro" herdr-spring-load-linux-test \
    sh -c 'cp -r /repo ~/plugin && cd ~/plugin && sh scripts/build.sh; echo "exit=$?"; ls ~/.local/share/herdr-spring-load 2>&1'
