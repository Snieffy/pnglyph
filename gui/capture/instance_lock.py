"""
A global (not per-project) lock file, held only for as long as a startup
crash-recovery prompt/review is actually in progress -- not for the whole
app's lifetime. A second PNGlyph launch that finds a LIVE lock is refused
and told why; a launch that finds a STALE one (owning process no longer
running -- e.g. that process itself crashed mid-recovery) removes it and
proceeds normally, since otherwise a crash during recovery would
permanently brick the app with no instance left alive to ever clear it.

Global rather than per-project to match the single global "last project"
settings pointer this guards (see settings.py) -- the accepted cost is
that a second instance opening a completely unrelated project is also
blocked while someone else's recovery is unresolved, which is expected to
be rare in practice.
"""
from __future__ import annotations

import os
import sys
import tempfile

_LOCK_PATH = os.path.join(tempfile.gettempdir(), "pnglyph_recovery.lock")


def _pid_running(pid: int) -> bool:
    """Cross-platform "is this PID still alive" check with no extra
    dependencies."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True   # exists, just owned by someone else -- still "running"
        return True


def is_locked_by_live_process() -> bool:
    """True only if a lock file exists AND its recorded PID is still
    running. A stale lock (dead PID) is treated as not locked -- and is
    removed here, since finding out it's stale IS the moment to clean it
    up, not a separate pass."""
    if not os.path.exists(_LOCK_PATH):
        return False
    try:
        with open(_LOCK_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        # Unreadable/corrupt lock file -- can't trust it, treat as stale.
        _remove_lock_file()
        return False
    if _pid_running(pid):
        return True
    _remove_lock_file()
    return False


def acquire() -> bool:
    """Call only after is_locked_by_live_process() has returned False.
    Returns True once this process's PID is recorded as the lock owner."""
    try:
        with open(_LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False   # best-effort -- a failed write here shouldn't crash startup


def release() -> None:
    """Removes the lock ONLY if it's still this process's own -- a safety
    check against removing a lock some other, newer instance may have
    since acquired (e.g. after this process's own lock went stale for
    some external reason)."""
    try:
        with open(_LOCK_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return
    if pid == os.getpid():
        _remove_lock_file()


def _remove_lock_file() -> None:
    try:
        os.remove(_LOCK_PATH)
    except OSError:
        pass
