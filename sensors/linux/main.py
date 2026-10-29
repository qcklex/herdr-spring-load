"""herdr-spring-load Linux sensor (docs/SENSOR.md): picks the X11 or Hyprland backend.

herdr-spring-load-sensor-linux [--check] [--verbose]
"""
import os
import sys

import common


def session(env=os.environ):
    """'hyprland', 'wayland' (any other compositor), 'x11' or None."""
    if env.get('WAYLAND_DISPLAY') or env.get('XDG_SESSION_TYPE') == 'wayland':
        return 'hyprland' if env.get('HYPRLAND_INSTANCE_SIGNATURE') else 'wayland'
    if env.get('DISPLAY'):
        return 'x11'
    return None


def inert(sensor, message):
    common.hello(sensor, 'x11-root', [])
    common.status('error', message)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    common.set_verbose('--verbose' in argv)
    kind = session()
    if '--check' in argv:
        if kind == 'x11':
            import x11
            common.hello('linux-x11', 'x11-root', x11.CAPABILITIES)
        elif kind == 'hyprland':
            import hyprland
            common.hello('linux-hyprland', hyprland.COORDS, hyprland.CAPABILITIES)
        else:
            common.hello('linux', 'x11-root', [])
        return 0
    common.watch_stdin_after_hello()
    if kind == 'x11':
        import x11
        x11.run(os.environ.get('DISPLAY'))
    elif kind == 'hyprland':
        import hyprland
        hyprland.run()
    elif kind == 'wayland':
        inert('linux-wayland', 'Wayland without Hyprland: GNOME needs a Shell extension and KDE a KWin '
                               'script to report the pointer and windows; spring-loading is off')
    else:
        inert('linux', 'no X11 or Wayland session (DISPLAY and WAYLAND_DISPLAY unset); sensor inert')
    # An inert sensor stays alive until the core closes stdin, as the contract requires.
    common.wait_for_stdin_eof()
    return 0


if __name__ == '__main__':
    sys.exit(main())
