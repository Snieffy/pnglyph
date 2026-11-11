"""
Tracks the last active project's source_dir in QSettings, cleared only on
a NORMAL shutdown (see main_window.MainWindow.closeEvent). If this pointer
is still present at the next startup, the previous run didn't end
normally, and _check_capture_recovery() checks that source_dir for a
leftover .capture_session to offer resuming or discarding.

This intentionally tracks source_dir, not a saved .pnglyph.json path --
capture is valid against a project that has never been saved yet (a fresh
"New Project" or a freshly-imported folder), and a saved project's own
JSON path (when one exists) is recorded alongside it so recovery can
reload the exact same project rather than only reconstructing one via a
folder re-scan.

No settings persistence existed anywhere in this codebase before this --
this is new, small infrastructure, not a replacement for anything.
"""
from __future__ import annotations

from PySide6.QtCore import QSettings

_ORG = "PNGlyph"
_APP = "PNGlyph"

_KEY_SOURCE_DIR = "last_project/source_dir"
_KEY_SAVED_PATH = "last_project/saved_path"


def _settings() -> QSettings:
    # Explicit org/app args -- works even called before a QApplication
    # instance exists (QSettings only needs QCoreApplication for the
    # zero-arg constructor form; this isn't that form).
    return QSettings(_ORG, _APP)


def remember_last_project(source_dir: str, saved_path: str | None) -> None:
    s = _settings()
    s.setValue(_KEY_SOURCE_DIR, source_dir)
    s.setValue(_KEY_SAVED_PATH, saved_path or "")
    s.sync()


def forget_last_project() -> None:
    """Called whenever the currently-tracked project's crash-recovery
    relevance definitively ends -- a confirmed-normal shutdown
    (MainWindow.closeEvent), the startup recovery check resolving one
    way or another (_check_capture_recovery, whether resumed,
    discarded, or deferred -- the pointer's job ends there either way),
    or a from-scratch project being fully reverted/discarded before
    anything was ever committed to it (there's nothing left to recover
    if the app crashed right after). Its presence at the NEXT startup
    is what signals an abnormal previous exit with something still
    genuinely worth recovering, so this must only be called once one
    of those is definitively true -- never speculatively or from
    anywhere the project might still need tracking."""
    s = _settings()
    s.remove(_KEY_SOURCE_DIR)
    s.remove(_KEY_SAVED_PATH)
    s.sync()


def get_last_project() -> tuple[str, str] | None:
    """Returns (source_dir, saved_path) if a pointer is present (meaning
    the last run did not end normally), else None. saved_path may be ""
    (project was never saved before the abnormal exit)."""
    s = _settings()
    source_dir = s.value(_KEY_SOURCE_DIR, "", str)
    if not source_dir:
        return None
    saved_path = s.value(_KEY_SAVED_PATH, "", str)
    return source_dir, saved_path
