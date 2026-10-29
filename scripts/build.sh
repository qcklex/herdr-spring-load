#!/bin/sh
# Install step for herdr plugin install ([[build]]), the setup pane and
# helper/install.sh; --uninstall and --status for the plugin actions. The core
# reads herdr's TOML config, so it needs Python 3.11+ (tomllib); macOS's
# /usr/bin/python3 is older, so look for a newer one first.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
script="$root/setup.py"
case "${1:-}" in
    --uninstall) shift; script="$root/uninstall.py" ;;
    --status) shift; script=status ;;
esac
# Fixed paths cover installs that are not on the PATH herdr was started with.
fallbacks=${SPRING_LOAD_PYTHON_FALLBACKS-/opt/homebrew/bin/python3 /usr/local/bin/python3}
for py in python3.14 python3.13 python3.12 python3.11 python3 $fallbacks; do
    command -v "$py" >/dev/null 2>&1 || continue
    if "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        if [ "$script" = status ]; then
            PYTHONPATH="$root" PYTHONDONTWRITEBYTECODE=1 exec "$py" -m spring_load status "$@"
        fi
        PYTHONDONTWRITEBYTECODE=1 exec "$py" "$script" "$@"
    fi
done
echo "herdr-spring-load needs Python 3.11 or newer (for tomllib); nothing was changed." >&2
echo "Install one (for example: brew install python), then install the plugin again." >&2
exit 1
