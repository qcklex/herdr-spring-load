"""Exclusive, non-blocking file locks on every OS: fcntl.flock on macOS and
Linux, msvcrt.locking on Windows (which has no fcntl). The core holds one while
it runs (one helper per user, and how setup and status tell it is running);
setup and uninstall hold another so they never run at the same time."""
import os

def try_lock(handle):
    """Lock the open file `handle` for this process. False when another
    process holds the lock; the lock goes away with the handle."""
    if os.name == 'nt':
        import msvcrt
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False

def held(path):
    """Whether another process holds the lock on `path` right now."""
    try:
        with open(path, 'a') as handle:
            return not try_lock(handle)
    except OSError:
        return False
