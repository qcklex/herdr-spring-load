#!/bin/sh
# Manual install from a clone: links the plugin, then runs the same setup
# as herdr plugin install.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec sh "$root/scripts/build.sh" --link "$@"
