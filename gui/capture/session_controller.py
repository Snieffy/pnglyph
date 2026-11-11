"""
Owns one capture session's moving parts (the capture window, the overlay,
CaptureSessionStore) and the transitions between them. MainWindow talks to
this through a small, deliberately narrow surface:

    controller = CaptureSessionController(main_window)
    controller.start_or_resume()

everything else happens through Qt signals internally. MainWindow's own
involvement is limited to hide()/show() of itself (done here, since the
controller is what knows which state that belongs to) and the actual
project.add_glyph work on commit (main_window._commit_capture_session --
kept there, not here, since it needs the SAME overwrite-conflict flow
_add_blank_glyph already uses).
"""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QObject, QRect, QSize, QByteArray, QBuffer, QIODevice, QEventLoop, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import QMessageBox, QApplication

from .manifest import CaptureSessionStore
from .overlay import CaptureOverlay
from .capture_window import CaptureWindow
from .. import app_settings


def _show_and_activate(window) -> None:
    """show()/raise_() happen immediately; actual OS-level window
    activation is deferred one event-loop tick via
    QTimer.singleShot(0, ...) instead of being called synchronously
    right here. Confirmed as a real, reported cause of ESC/CTRL
    appearing to do nothing on a freshly-shown capture overlay until
    some OTHER interaction happened to establish real focus first:
    calling activateWindow() immediately after show() can be a no-op
    on some platforms, since the native window isn't always fully
    mapped by the OS at that exact instant -- especially when the
    window being left behind is a still-open MODAL dialog (e.g.
    AddGlyphDialog for the single-capture flow), which tends to hold
    onto activation more stubbornly than an ordinary window would.
    Deferring to the next tick gives the OS a moment to finish mapping
    the window before asking it to activate."""
    window.show()
    window.raise_()

    def _activate():
        window.activateWindow()
        window.setFocus()
    QTimer.singleShot(0, _activate)


def _dwm_flush() -> None:
    """Blocks the calling thread, synchronously, until DWM (the Windows
    Desktop Window Manager -- the compositor) has actually finished
    presenting its current frame. NOT the actual fix for the hide/grab
    problem this file has been chasing -- confirmed via a standalone
    diagnostic, completely separate from this application, that
    DwmFlush() reporting success (twice) still left a hidden window
    visible in a subsequent grabWindow() capture. That rules out timing
    entirely: QScreen.grabWindow() on Windows appears to use the legacy
    GDI BitBlt capture path, a documented case of not reliably
    reflecting DWM's actual composited output regardless of how long a
    caller waits for DWM itself. See _exclude_from_capture below for what
    the SAME diagnostic confirmed actually works.

    Kept and still called as a harmless, cheap extra safety margin
    alongside the real fix, not because it's been shown to matter on
    its own -- costs nothing if it's a no-op, and DWM being genuinely
    caught up can't hurt even if it isn't sufficient by itself. A
    no-op on non-Windows platforms (no DWM there). Logs any failure
    calling it rather than swallowing it silently."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        result = ctypes.windll.dwmapi.DwmFlush()
        if result != 0:
            logging.getLogger("pnglyph").warning(
                "DwmFlush() returned non-zero HRESULT 0x%x -- composition may be disabled", result & 0xFFFFFFFF)
    except Exception:
        logging.getLogger("pnglyph").exception("DwmFlush() call itself failed")


def _exclude_from_capture(widget) -> None:
    """Makes widget genuinely excluded from a subsequent
    QScreen.grabWindow() call, via full transparency
    (setWindowOpacity(0.0)) -- confirmed via a standalone diagnostic,
    completely separate from this application, after two other
    techniques were each ruled out by live testing first: hiding the
    widget (even carefully synchronized with DWM via _dwm_flush, and
    even waited on via its own Hide event) still left it visible in the
    resulting capture, and minimizing it -- confirmed working in that
    same diagnostic -- required a much longer wait (a real time budget
    of repeated processEvents() calls, not just one or two) and a more
    disruptive window-state transition (taskbar animation) to get
    there. The underlying cause for hide's own failure: Windows'
    QScreen.grabWindow() appears to use the legacy GDI BitBlt capture
    path, a documented case of not reliably reflecting DWM's actual
    composited output regardless of how long a caller waits for DWM
    itself -- not a timing problem at all, which is why no amount of
    synchronization around hide() could ever have fixed it.

    Suggested reasoning for why THIS technique specifically works
    (found externally, not independently verified here): forcing full
    transparency supposedly forces DWM to composite whatever's behind
    the window immediately, a fundamentally different mechanism from
    hiding or minimizing. Confirmed empirically working regardless of
    whether that specific explanation is correct.

    The wait itself -- a single processEvents() plus a single
    _dwm_flush() call -- matches exactly what was tested and confirmed
    sufficient; not lengthened defensively without a reason to, given
    this technique's own advantage over minimizing is specifically
    that it needs less."""
    widget.setWindowOpacity(0.0)
    QApplication.processEvents()
    _dwm_flush()


def _restore_after_capture(widget) -> None:
    """Counterpart to _exclude_from_capture -- restores full opacity
    and brings widget back to the foreground."""
    widget.setWindowOpacity(1.0)
    widget.activateWindow()



def _screen_union() -> QRect:
    """The bounding rectangle covering every connected screen, in
    logical desktop pixel coordinates -- the exact region
    _grab_virtual_desktop() below captures."""
    union = QRect()
    for screen in QGuiApplication.screens():
        union = union.united(screen.geometry())
    return union


def _grab_virtual_desktop() -> QImage:
    """Grabs every screen and stitches them into one QImage covering the
    union of their geometries, each blitted at its own device pixel
    ratio -- so the result is 1:1 with logical desktop pixel coordinates
    regardless of per-monitor HiDPI scaling differences.

    Wayland is out of scope (see the spec) -- this relies on
    QScreen.grabWindow working directly, true on Windows/macOS/X11."""
    screens = QGuiApplication.screens()
    union = _screen_union()

    result = QImage(union.size(), QImage.Format_ARGB32)
    result.fill(0)
    from PySide6.QtGui import QPainter
    painter = QPainter(result)
    for screen in screens:
        shot = screen.grabWindow(0)
        geo = screen.geometry()
        painter.drawPixmap(geo.topLeft() - union.topLeft(), shot)
    painter.end()
    return result


def _pixmap_to_png_bytes(pixmap) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    pixmap.save(buf, "PNG")
    data: QByteArray = buf.data()
    return bytes(data)


class CaptureSessionController(QObject):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.store: CaptureSessionStore | None = None
        self.overlay: CaptureOverlay | None = None
        self.capture_window: CaptureWindow | None = None
        # Just an initial guess for a brand-new app run, before any
        # session has actually started -- _enter_interact_state always
        # sets both of these fresh at the start of every session
        # regardless (see its own comment), so this only matters in the
        # narrow window before that first happens.
        self._fixed_size: QSize | None = app_settings.get_last_capture_size()
        self._snap_enabled: bool = app_settings.get_last_capture_snap_enabled()

    # -- entry point --------------------------------------------------------

    def start_or_resume(self) -> None:
        project = self.main_window.project
        if project is None:
            # No "Set canvas size" checkbox here (defer_canvas_draw
            # covers the same "postpone showing anything" job the
            # checkbox used to) -- confirmed as a deliberate removal,
            # not a bug: asking the user to guess a capture size before
            # they've captured anything is rarely useful, and every
            # from-scratch finalization now goes through the same
            # review-and-edit canvas-size prompt anyway (see
            # CanvasSizePromptDialog, used by _on_commit and
            # _on_close_keep_pending), whether the queue has one
            # capture or several. Same treatment "Add a new glyph"
            # already got -- see MainWindow._add_blank_glyph's own
            # comment on this exact reasoning.
            if not self.main_window._create_new_project_via_dialog(size_label="Capture", show_size_fields=False,
                                                                     defer_canvas_draw=True):
                return
            project = self.main_window.project
            # Forced True regardless of the dialog -- with no checkbox
            # left to ever produce the old 0x0 deferred sentinel, this
            # would otherwise always come back False, and every from-
            # scratch capture would incorrectly seed its first grab
            # from the (nonexistent) project canvas size instead of
            # staying properly deferred. Same forcing "Add a new glyph"
            # already does for the identical reason.
            self.main_window._capture_canvas_pending = True
            # Tracked so a Discard of THIS session (before anything's
            # been committed) can revert the project entirely rather
            # than leaving a placeholder/finalized canvas behind for a
            # project that was only ever created to serve this attempt
            # -- see _on_discard. Cleared the moment anything real
            # happens to it (a successful commit, a save).
            self.main_window._project_created_for_capture_session = True
        if self.store is not None:
            # A session is already active in this run (not a crash-
            # recovery case) -- just bring its UI back to front rather
            # than starting a second, conflicting one.
            self._raise_active_ui()
            return

        store = CaptureSessionStore(project.source_dir)
        resumed = store.load_existing()
        self.store = store

        if resumed:
            self._open_capture_window(resumed=True)
        else:
            self._enter_interact_state()

    def resume_from_recovery(self, source_dir: str) -> None:
        """Used specifically by MainWindow._check_capture_recovery --
        source_dir may belong to a project that isn't even loaded yet
        (see that method for how it gets one loaded first)."""
        store = CaptureSessionStore(source_dir)
        if not store.load_existing():
            return   # nothing there after all -- caller already checked, but be defensive
        self.store = store
        self._open_capture_window(resumed=True)

    def _raise_active_ui(self) -> None:
        if self.capture_window is not None:
            self.capture_window.show()
            self.capture_window.raise_()

    def topmost_widget(self):
        """The capture window if it's actually visible right now, used
        as the parent for any message box shown mid-session. The main
        window itself is hidden for the whole session, and a dialog
        parented to a hidden window gets no elevation relative to the
        capture window/overlay's own WindowStaysOnTopHint -- it can end
        up trapped underneath them with no way to click it forward,
        which is exactly what happened before this existed. Falls back
        to the main window once nothing capture-specific is showing."""
        if self.capture_window is not None and self.capture_window.isVisible():
            return self.capture_window
        return self.main_window

    # -- Interact / Capture state ------------------------------------------

    def _enter_interact_state(self) -> None:
        self.main_window.hide()
        # Always the persisted, remembered capture size -- never the
        # project's own canvas size, regardless of whether this project
        # is brand new, already existed, or is being resumed.
        #
        # Confirmed as a real, reported bug in the earlier version of
        # this (which unconditionally borrowed the canvas size whenever
        # a real one was already in effect, then immediately persisted
        # THAT, overwriting whatever capture size had actually been in
        # use): starting a "Capture rectangles" session against an
        # existing project silently discarded and overwrote the size
        # just set moments earlier via "Add a new glyph" (or a previous
        # capture session entirely) -- and since the overwrite was
        # itself persisted, even switching back afterward inherited the
        # contamination, not just this one session. Simplified per the
        # user's own explicit request: never revert to the canvas area
        # at all while the app is open, just keep remembering whatever
        # the capture area was last changed to -- so this is now
        # identical to what a from-scratch, still-pending project
        # already did (get_last_capture_size(), whatever that currently
        # holds, possibly still None if truly nothing's ever been
        # captured this run or before it).
        self._fixed_size = app_settings.get_last_capture_size()
        self._snap_enabled = app_settings.get_last_capture_snap_enabled()
        self._open_capture_window(resumed=False)

    def _open_capture_window(self, resumed: bool) -> None:
        """Creates the capture window if needed and shows/raises it,
        refreshing its live status (count, capture size, canvas-size
        availability) against current state every time -- covers both
        a session's first entry into Interact and a resumed session
        (see start_or_resume/resume_from_recovery), which is why this
        (unlike the grab/canvas-size wiring, done once at construction)
        always re-applies the freshest values regardless of whether the
        window itself is new or already existed."""
        if self.capture_window is None:
            window = CaptureWindow(
                self.store, resumed=resumed,
                existing_codepoints_cb=lambda: set(self.main_window.project.glyphs.keys())
                if self.main_window.project else set())
            window.grab_requested.connect(self._on_grab_requested)
            window.use_canvas_size_requested.connect(self._on_use_canvas_size)
            window.commit_requested.connect(self._on_commit)
            window.discard_requested.connect(self._on_discard)
            window.close_keep_pending_requested.connect(self._on_close_keep_pending)
            self.capture_window = window
        self.capture_window.set_capture_count(len(self.store.items))
        self.capture_window.set_capture_size(self._fixed_size)
        self.capture_window.set_canvas_size_available(self._current_canvas_size())
        self.capture_window.set_interact_mode(True)
        self.capture_window.show()
        self.capture_window.raise_()

    def _current_canvas_size(self):
        """The current project's own, genuinely-established canvas
        size as a QSize, or None when there isn't one worth offering --
        no project at all, no metrics yet, OR a still-pending, from-
        scratch project created just for THIS capture session (see
        start_or_resume's own _capture_canvas_pending comment): that
        one's own canvas_size is only ever a provisional placeholder at
        this point, not yet finalized by anything real (finalization
        happens at commit time, from whatever the largest actual
        capture across the session turns out to be) -- offering to
        match a placeholder that's about to be overridden anyway
        wouldn't mean anything."""
        project = self.main_window.project
        if project is None or self.main_window._capture_canvas_pending:
            return None
        size = project.metrics.canvas_size
        if size is None:
            return None
        return QSize(size[0], size[1])

    def _on_use_canvas_size(self) -> None:
        size = self._current_canvas_size()
        if size is None:
            return   # shouldn't happen -- the button is only enabled once this is real, but don't proceed on a guess
        self._fixed_size = size
        # Persisted immediately, same as any other capture-size change
        # (see _on_captured's own identical call) -- shared with
        # capture_one_shot and survives across sessions/app restarts,
        # not just within this still-open one.
        app_settings.remember_last_capture_size(self._fixed_size)
        self.capture_window.set_capture_size(self._fixed_size)

    def _on_grab_requested(self) -> None:
        # Deliberately does NOT re-sync _fixed_size from the persisted
        # setting here -- an earlier version did, and it was a real,
        # reported bug: the main window stays hidden for the entire
        # Interact/Capture cycle, so nothing else (single-shot Capture,
        # a different session) can possibly run and change the
        # persisted value while this session is active -- there is no
        # legitimate "someone else changed it, pick up the fresh value"
        # case to protect against mid-session. Re-syncing anyway meant
        # a DEFERRED session (self._fixed_size correctly None, per
        # _enter_interact_state) would have that None silently
        # replaced by whatever the retained value happened to be the
        # moment the first grab ran -- exactly the "reverts to the last
        # stored value" bug that deferring was supposed to prevent.
        # self._fixed_size/_snap_enabled are already correct here,
        # either from _enter_interact_state (this session's first grab)
        # or from this SAME session's own previous _on_captured (later
        # grabs) -- nothing else could have touched them in between.
        try:
            # Excluded BEFORE the grab, not after -- confirmed as a
            # real, reported bug otherwise: _grab_virtual_desktop below
            # freezes a screenshot that becomes the overlay's own
            # background for the entire rest of the capture state, so
            # hiding this window any later than this bakes its own,
            # already-visible appearance permanently into that frozen
            # image regardless. It LOOKED like it never hid at all, even
            # on a run where the live widget genuinely had -- it just
            # hid too late to keep itself out of the one screenshot that
            # actually mattered. Same opacity-based technique already
            # proven necessary for this exact class of problem (see
            # _exclude_from_capture's own docstring). This is now the
            # ONLY window that ever needs this treatment -- see this
            # module's own former two-window history in capture_window.
            # py's docstring for what used to go wrong when a second,
            # independently-timed window could also be on screen.
            _exclude_from_capture(self.capture_window)
            image = _grab_virtual_desktop()
            # Genuinely hidden too, not just left transparent -- the
            # screenshot is already safely taken by this point, so this
            # can't un-exclude anything; this instead ensures this
            # window can't intercept mouse events over its own screen
            # region while the overlay is up, which a merely-
            # transparent window still could.
            self.capture_window.hide()
            from PySide6.QtGui import QPixmap
            pixmap = QPixmap.fromImage(image)

            self.overlay = CaptureOverlay(pixmap, self._fixed_size, self._snap_enabled)
            self.overlay.set_capture_count(len(self.store.items))
            self.overlay.captured.connect(self._on_captured)
            self.overlay.cancelled_to_interact.connect(self._on_overlay_cancelled)
            self.capture_window.set_interact_mode(False)
            _show_and_activate(self.overlay)
        except Exception as e:
            # A grab failure must not leave the capture window stuck mid-transition
            # (grab button already disabled, no overlay to re-enable
            # it) -- restore Interact and surface the error instead of
            # leaving state inconsistent. Local snap detection itself
            # can no longer reach this branch (it's patch-local, lazy,
            # and wrapped in its own _safe variant -- see
            # line_detection.py); only the grab/QImage conversion steps
            # still can.
            logging.getLogger("pnglyph").exception("Screen grab failed")
            self.overlay = None
            if self.capture_window is not None:
                # Restored BEFORE the error box below, regardless of
                # exactly where above the exception happened to occur
                # (even before _exclude_from_capture ever ran) --
                # show()/setWindowOpacity/set_interact_mode are all
                # idempotent to call here even if the window was never
                # actually hidden/excluded in the first place this time.
                self.capture_window.show()
                _restore_after_capture(self.capture_window)
                self.capture_window.set_interact_mode(True)
            # Parented to the capture window (visible again by this
            # point), not the hidden main window -- same reasoning as
            # topmost_widget() elsewhere in this class: a dialog
            # parented to a hidden window has no elevation relative to
            # the capture window/overlay's own WindowStaysOnTopHint.
            box = QMessageBox(QMessageBox.Warning, "Capture failed",
                               f"Couldn't grab the screen:\n{e}",
                               QMessageBox.Ok, self.topmost_widget())
            box.setWindowFlags(box.windowFlags() | Qt.WindowStaysOnTopHint)
            box.exec()

    def _on_overlay_cancelled(self) -> None:
        self._snap_enabled = self.overlay.snap_enabled   # carry forward even if nothing was captured this grab
        app_settings.remember_last_capture_snap_enabled(self._snap_enabled)
        self.overlay = None   # WA_DeleteOnClose already handled the widget itself
        if self.capture_window is not None:
            # show() before _restore_after_capture -- activateWindow()
            # (part of that call) needs the widget to actually be
            # visible again first to meaningfully bring it to the
            # foreground; doing it in the other order left it trying to
            # activate a still-hidden window.
            self.capture_window.show()
            _restore_after_capture(self.capture_window)
            self.capture_window.set_interact_mode(True)

    def _on_captured(self, rect: QRect) -> None:
        crop = self.overlay.pixmap.copy(rect)
        self._fixed_size = self.overlay.fixed_size   # carry forward to the NEXT grab's overlay
        self._snap_enabled = self.overlay.snap_enabled
        # Persisted immediately, not just held in this controller
        # instance -- see __init__'s own comment on why this is now
        # shared with capture_one_shot and survives across sessions/app
        # restarts, not just within one still-open session.
        if self._fixed_size is not None:
            app_settings.remember_last_capture_size(self._fixed_size)
        app_settings.remember_last_capture_snap_enabled(self._snap_enabled)
        item = self.store.add_capture(_pixmap_to_png_bytes(crop))

        # Canvas-size finalization for a "set by first capture" project
        # (see MainWindow._create_new_project_via_dialog) is now decided
        # at COMMIT time, from the LARGEST capture across the whole
        # session -- not here, on whichever one happens to be first.
        # Locking in from the first capture risked a too-small canvas
        # forcing later, bigger captures through the resize-to-fit
        # pipeline unnecessarily -- see _on_commit's own handling.

        self.overlay.set_capture_count(len(self.store.items))
        if self.capture_window is not None:
            self.capture_window.set_capture_count(len(self.store.items))
            self.capture_window.set_capture_size(self._fixed_size)
            self.capture_window.add_capture(item)

    def _on_close_keep_pending(self) -> None:
        """The "Close (keep pending)" button specifically -- see
        close_keep_pending_requested's own docstring. Useful when the
        user wants to step away mid-capture to do something else first
        (open a different project, import a folder) while these
        captures stay queued for later -- which means the project
        needs a REAL canvas size right now if it doesn't have one yet,
        rather than staying pending indefinitely while nothing is
        actively happening to ever finalize it. Runs the same review-
        and-edit prompt every other from-scratch finalization now uses
        (see CanvasSizePromptDialog), pre-filled with the largest
        capture in the queue so far, or the hardcoded/retained New
        Project default if nothing's been captured yet at all -- only
        when _capture_canvas_pending (starting from scratch); an
        already-established canvas from an existing project is never
        touched by this."""
        if getattr(self.main_window, "_capture_canvas_pending", False):
            sizes = self.main_window._read_capture_item_dimensions(self.store)
            if sizes:
                areas = [(w * h, w, h) for _seq, w, h in sizes]
                _max_area, default_w, default_h = max(areas)
                message = ("No canvas exists yet for this project. The canvas will be set "
                           "to the largest capture so far \u2014 edit it if you'd like, or "
                           "accept it as is.")
            else:
                default_size = self.main_window._last_new_project_field_size
                default_w, default_h = default_size.width(), default_size.height()
                message = ("No canvas exists yet for this project, and nothing has been "
                           "captured yet either. This is the current default \u2014 edit it "
                           "if you'd like, or accept it as is.")
            chosen = self.main_window._prompt_canvas_size(message, default_w, default_h)
            if chosen is None:
                return   # stays open, exactly as it was -- nothing to undo
            self.main_window._finalize_capture_canvas_size(chosen.width(), chosen.height())
        if self.capture_window is not None:
            self.capture_window.mark_resolved()
        self._close_all_and_reset()

    # -- commit / discard -----------------------------------------------------

    def _on_commit(self) -> None:
        assigned = [i for i in self.store.items if i.codepoint is not None]
        if not assigned:
            return

        if getattr(self.main_window, "_capture_canvas_pending", False):
            # Canvas-size finalization for a "set by first capture"
            # project, deferred all the way to commit time so it can be
            # based on the LARGEST capture across the whole session
            # (assigned or not -- a still-unassigned capture could
            # still end up the largest once it IS assigned, so the
            # whole store is considered here, not just what's being
            # committed this specific time), rather than whichever one
            # happened to be captured first. A too-small canvas would
            # otherwise force bigger, later captures through the
            # resize-to-fit pipeline unnecessarily.
            #
            # No size-variance confirmation gate here (an earlier
            # version had one) -- resize-to-fit already anchors from a
            # fixed top-left origin regardless of a capture's native
            # size, so every glyph ends up consistently positioned
            # without needing the user to approve size differences
            # first; it's not protecting against anything that would
            # otherwise go wrong.
            sizes = self.main_window._read_capture_item_dimensions(self.store)
            if sizes:
                areas = [(w * h, w, h) for _seq, w, h in sizes]
                _max_area, max_w, max_h = max(areas)
                # Runs the same review-and-edit prompt every other
                # from-scratch finalization now uses -- entirely
                # replaces the earlier too-small-only warning (see
                # CanvasSizePromptDialog and
                # MainWindow._confirm_tiny_capture_size, the latter no
                # longer used by any caller): far better to always let
                # the user see and adjust the size about to be locked
                # in, not just get a warning on the way TOO small.
                # Cancel leaves everything exactly as it was -- still
                # fully intact, nothing queued or assigned is lost --
                # so a bigger capture can be added and Commit tried
                # again, the same outcome "restart" used to mean.
                chosen = self.main_window._prompt_canvas_size(
                    "No canvas exists yet for this project. The canvas will be set to "
                    "the largest capture being committed \u2014 edit it if you'd like, "
                    "or accept it as is.",
                    max_w, max_h)
                if chosen is None:
                    return
                self.main_window._finalize_capture_canvas_size(chosen.width(), chosen.height())

        # (seq, codepoint, source_png_path) -- main_window hands back
        # the seqs it actually committed, since a user "No" on an
        # individual overwrite-conflict prompt means that one specific
        # item stays queued rather than being silently dropped.
        committed_seqs = self.main_window._commit_capture_session(
            [(i.seq, i.codepoint, self.store.item_path(i)) for i in assigned]
        )
        if committed_seqs:
            # The project now has real committed glyphs -- no longer
            # eligible for a Discard-time revert even if items remain
            # queued (see _on_discard).
            self.main_window._project_created_for_capture_session = False
        for seq in committed_seqs:
            self.store.remove_item(seq)
            if self.capture_window is not None:
                self.capture_window.remove_row_silently(seq)

        if not self.store.items:
            self.store.clear_after_commit()
            self._close_all_and_reset()
        # else: unassigned items remain queued -- session stays open,
        # exactly as the assignment dialog's own commit-confirmation
        # warned would happen.

    def _on_discard(self) -> None:
        self.store.discard()
        if getattr(self.main_window, "_project_created_for_capture_session", False):
            # This project only exists because THIS session created it
            # (capture-from-scratch) and nothing was ever actually
            # committed into it -- discarding the session should leave
            # things exactly as if it had never been started, not with
            # a placeholder/finalized canvas and an otherwise-empty
            # project hanging around.
            self.main_window._revert_uncommitted_capture_project()
        self._close_all_and_reset()

    def _close_all_and_reset(self) -> None:
        if self.overlay is not None:
            self.overlay.close()
        if self.capture_window is not None:
            self.capture_window.close()
            self.capture_window.deleteLater()
        self.main_window.show()
        self.main_window.raise_()
        self._reset_session_objects()

    def _reset_session_objects(self) -> None:
        self.store = None
        self.overlay = None
        self.capture_window = None
        # Re-seeded from the persisted value, not reset to None/False --
        # a session ending (commit or discard) shouldn't also throw away
        # a perfectly good, still-relevant capture size/snap state that
        # the NEXT session (or a single-shot capture) would otherwise
        # have to rediscover via SHIFT-two-click all over again. See
        # __init__'s own comment.
        self._fixed_size = app_settings.get_last_capture_size()
        self._snap_enabled = app_settings.get_last_capture_snap_enabled()


# ---------------------------------------------------------------------------
# Single-capture helper for AddGlyphDialog's "Capture..." button -- reuses
# the same grab/detect/overlay machinery for exactly one crop, with no
# capture window, no session store, no multi-grab loop.
# ---------------------------------------------------------------------------

def capture_one_shot(parent=None) -> str | None:
    """Runs one grab -> overlay -> single-rectangle capture, saves the
    crop to a temp PNG, and returns its path (or None if the user
    cancelled, OR if the grab itself failed -- both cases return
    cleanly rather than letting an exception escape into the caller's
    Qt slot, which is what let a grab failure destabilize the whole app
    rather than just this one action; see _on_grab_requested's own
    try/except for the same reasoning). Blocks the calling dialog via a
    local QEventLoop rather than returning immediately, since
    AddGlyphDialog's own flow expects a synchronous-feeling result the
    same way its "File..." button's QFileDialog call already behaves.
    Reads/writes the SAME persisted "last capture size/snap" the full
    multi-capture session uses (see app_settings.get_last_capture_size)
    -- confirmed as a real, reported inconsistency in an earlier version
    that forced this specific flow through SHIFT-click two corners
    EVERY time regardless of anything captured before, in either flow.
    Now behaves identically to the session's own overlay: a plain click
    reuses whatever size was last used (by either flow), SHIFT starts a
    fresh two-click rectangle to redefine it."""
    import tempfile
    import os

    # Confirmed as a real, reported bug otherwise: the screenshot below
    # is taken BEFORE the overlay ever shows, so if the caller (the
    # main window, and/or AddGlyphDialog if it hasn't fully closed yet)
    # is still visible at that exact moment, it gets baked directly
    # into the captured reference image itself -- not just briefly
    # visible on screen, but part of what the user then has to draw a
    # capture rectangle over.
    #
    # See _exclude_from_capture's own docstring for the full account of
    # why this specific technique (window opacity) is used, and what
    # else was tried and ruled out first.
    if parent is not None:
        _exclude_from_capture(parent)

    try:
        image = _grab_virtual_desktop()
        from PySide6.QtGui import QPixmap
        pixmap = QPixmap.fromImage(image)
    except Exception as e:
        logging.getLogger("pnglyph").exception("Screen grab failed")
        QMessageBox.warning(parent, "Capture failed", f"Couldn't grab the screen:\n{e}")
        if parent is not None:
            _restore_after_capture(parent)
        return None

    overlay = CaptureOverlay(pixmap, app_settings.get_last_capture_size(),
                              app_settings.get_last_capture_snap_enabled())
    result_path: list[str | None] = [None]
    loop = QEventLoop()

    def on_captured(rect: QRect):
        crop = overlay.pixmap.copy(rect)
        fd, path = tempfile.mkstemp(suffix=".png", prefix="pnglyph_capture_")
        os.close(fd)
        crop.save(path, "PNG")
        result_path[0] = path
        overlay.close()

    def on_cancelled():
        overlay.close()

    def on_destroyed(*_args):
        # Written back here, once, right as the overlay is about to
        # disappear -- covers BOTH a completed capture and a plain
        # cancel (which can still have flipped the snap toggle, or
        # redefined the size via SHIFT without ever clicking a final
        # second corner... actually the size only ever changes on an
        # actual capture, but snap can change on its own either way),
        # the same two cases _on_overlay_cancelled/_on_captured cover
        # separately for the full session.
        if overlay.fixed_size is not None:
            app_settings.remember_last_capture_size(overlay.fixed_size)
        app_settings.remember_last_capture_snap_enabled(overlay.snap_enabled)
        loop.quit()

    overlay.captured.connect(on_captured)
    overlay.cancelled_to_interact.connect(on_cancelled)
    overlay.destroyed.connect(on_destroyed)
    _show_and_activate(overlay)
    loop.exec()
    # Restored to full opacity now that the overlay's gone, and
    # explicitly re-activated -- confirmed as a real, reported cause of
    # the parent LOOKING correctly restored but not actually responding
    # to clicks afterward: nothing was explicitly re-establishing its
    # own activation, and that's exactly as unreliable in this
    # direction as it was showing the overlay in the first place (see
    # _show_and_activate's own docstring) -- deferred the same way, for
    # the same reason.
    if parent is not None:
        def _reactivate_parent():
            _restore_after_capture(parent)
            parent.setFocus()
        QTimer.singleShot(0, _reactivate_parent)
    return result_path[0]
