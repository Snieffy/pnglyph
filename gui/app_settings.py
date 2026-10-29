"""
A single, shared "last browsed folder" setting used by every folder/file
dialog in the app (New Project's folder picker, Import Folder, Save/Load
Project, Add Glyph's source-PNG picker) -- one shared value rather than
a separate one per dialog, so wherever you last were is what every one
of them opens to next, matching how most desktop apps behave.

Distinct from capture/settings.py, which is specifically the crash-
recovery "last active project" pointer -- that one has very different
semantics (cleared only on a normal shutdown, checked at startup) and
shouldn't be conflated with this general browsing convenience.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QSettings, QSize

_ORG = "PNGlyph"
_APP = "PNGlyph"
_KEY_LAST_DIR = "browse/last_dir"
_KEY_CAPTURE_W = "capture/last_width"
_KEY_CAPTURE_H = "capture/last_height"
_KEY_CAPTURE_SNAP = "capture/last_snap_enabled"


def _settings() -> QSettings:
    return QSettings(_ORG, _APP)


def normalize_path(path: str) -> str:
    """Qt's own dialogs (QFileDialog.getExistingDirectory/getOpenFileName/
    getSaveFileName) return paths using forward slashes UNCONDITIONALLY,
    even on Windows -- that's Qt's own internal path convention, only
    converted to the platform's native separator via
    QDir.toNativeSeparators(), which nothing calling these dialogs here
    does. Combined LATER with an os.path.join/os.path.dirname call
    (which use the platform-native separator), the result mixes both --
    confirmed as a real, reported bug (a pre-filled folder field showing
    a mix of "/" and "\\"), and the same mechanism was already visible,
    unnoticed, in a real project's own saved source_dir value earlier in
    this project's history. Every path received directly from a Qt
    dialog should be passed through this immediately, before it's
    stored, displayed, or combined with anything else -- fixing it once
    at the point of receipt rather than wherever it later happens to be
    displayed or joined."""
    return os.path.normpath(path) if path else path


def get_last_browsed_dir() -> str:
    """Returns the last remembered directory, or "" if none yet (or if
    it no longer exists -- a stale remembered path is worse than no
    default at all, since most dialogs handle a missing starting
    directory gracefully but not all handle a bogus one the same way)."""
    d = _settings().value(_KEY_LAST_DIR, "", str)
    d = normalize_path(d)   # cleans up any pre-existing mixed-separator value already stored
    return d if d and os.path.isdir(d) else ""


def remember_last_browsed_dir(path: str) -> None:
    """path may be a file OR a directory -- the containing directory is
    what actually gets remembered either way, since that's the
    meaningful "location" for a future dialog to reopen at."""
    if not path:
        return
    path = normalize_path(path)
    d = path if os.path.isdir(path) else os.path.dirname(path)
    if not d:
        return
    s = _settings()
    s.setValue(_KEY_LAST_DIR, d)
    s.sync()


def get_last_capture_size() -> QSize | None:
    """The most recently used capture rectangle size -- shared by BOTH
    the single-shot Capture button and the full multi-capture session,
    persisted across app restarts (not just within one run), so
    whichever flow was used most recently is what the OTHER one starts
    from too. Returns None if nothing has ever been captured yet."""
    s = _settings()
    w = s.value(_KEY_CAPTURE_W, 0, int)
    h = s.value(_KEY_CAPTURE_H, 0, int)
    return QSize(w, h) if w > 0 and h > 0 else None


def remember_last_capture_size(size: QSize) -> None:
    if size is None or size.width() <= 0 or size.height() <= 0:
        return
    s = _settings()
    s.setValue(_KEY_CAPTURE_W, size.width())
    s.setValue(_KEY_CAPTURE_H, size.height())
    s.sync()


def get_last_capture_snap_enabled() -> bool:
    return _settings().value(_KEY_CAPTURE_SNAP, False, bool)


def remember_last_capture_snap_enabled(enabled: bool) -> None:
    s = _settings()
    s.setValue(_KEY_CAPTURE_SNAP, bool(enabled))
    s.sync()


def clear_all_persistent_settings() -> None:
    """Wipes every persistent value this application has ever written
    -- capture size/snap, last browsed directory, the crash-recovery
    project pointer, font-combobox preferences, all of it -- not a
    curated subset. gui.capture.settings shares this exact same
    QSettings org/app ("PNGlyph"/"PNGlyph"), just under different key
    prefixes within it, so this one clear() reaches everything either
    module has ever written, not just this module's own keys. Used by
    MainWindow._clear_project's own "also reset persistent settings"
    checkbox."""
    s = _settings()
    s.clear()
    s.sync()
