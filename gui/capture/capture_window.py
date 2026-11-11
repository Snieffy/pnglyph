"""
The single window for an entire capture session -- grab controls, capture
count, and per-item codepoint assignment all live here together. Non-modal
by design (shown via .show(), never .exec()) so it can stay open while you
work with whatever reference app you're capturing from.

Replaces what used to be two separate top-level windows (a small draggable
HUD plus this review window, shown either one at a time or both at once).
Merged because keeping two independently excludable/closable windows in
sync during a screen grab was the direct cause of several real bugs: a
grab from the HUD not excluding this window when it happened to also be
open, and this window's own opacity-restore firing before the overlay it
was excluded FOR had actually finished, popping it back into view mid-
capture. With one window there is exactly one exclude/restore pairing to
get right, tied directly to the overlay's own show/dismiss lifecycle (see
session_controller.CaptureSessionController._on_grab_requested and
_on_captured/_on_overlay_cancelled) -- not a second, independently-timed
call site that can drift out of sync with the first.

Nothing here writes into the real Project -- see session_controller.py's
commit handling. This window only edits the in-memory/on-disk session
queue via the CaptureSessionStore it's given.

The character table lives INSIDE this window, as a side panel on the
right (toggle: "Character table"), not as a separate pop-up: the pop-up
used to open on top of the capture rows, hiding the very thumbnail being
identified. Keeping it in this same window also keeps the single
exclude/restore pairing above intact -- a grab hides this window, and
the panel with it, with no second window to keep in sync. Clicking a
character assigns it to the ACTIVE row (highlighted; click a row or its
field to make it active) and moves on to the next capture without a
code.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QFrame, QGroupBox, QMessageBox, QCheckBox,
    QFileDialog,
)

from .codepoint_parse import parse_codepoint
from .manifest import CaptureSessionStore, CaptureItem
from .thumbnails import readable_thumbnail
from .text_assign import TextFileAssignDialog, CaptureRowInfo
from ..codepoint_entry import CodepointLineEdit, UnicodePickerWidget
from .. import app_settings

_THUMB_SIZE = 64
_LEFT_WIDTH = 440      # the capture-rows column, as the whole window used to be
_PANEL_WIDTH = 420     # the character-table side panel, added to the right
_ACTIVE_ROW_STYLE = "#captureRow { border: 2px solid #4a90d9; border-radius: 4px; }"

# Verbatim text from the old HUD's always-visible note -- now the Grab
# section button's own tooltip instead of permanent on-window space, since
# this window also has to make room for the capture rows.
_GRAB_TOOLTIP = (
    "Freeze the current on-screen section for capture.\n"
    "Zoom/pan the reference app first if needed, then click this.\n\n"
    "Keep zoom level constant for this session -- changing it forces\n"
    "resizing captures to match, which can hurt detection.\n"
    "Press CTRL to toggle snapping to a nearby corner, line, or point\n"
    "on/off (stays as set, no need to hold it). Hold SHIFT to draw a\n"
    "new capture rectangle (click one corner, then the other) instead\n"
    "of using the current fixed size."
)


class _CaptureRow(QFrame):
    codepoint_changed = Signal(int, object)   # seq, codepoint-or-None -- fires on every keystroke (live store update + inline error text only, no popups)
    delete_requested = Signal(int)            # seq
    # Fires once the user is actually DONE with this field -- Enter,
    # focus loss (Tab, clicking elsewhere), or the fast single-key/
    # Ctrl+U-hex-complete path (which moves focus itself, and that
    # focus change is what triggers this via editingFinished). This is
    # the ONE place overwrite/duplicate confirmation happens, not on
    # every keystroke -- see CaptureWindow._on_row_commit.
    commit_requested = Signal(int)            # seq
    # This row became the target for character-table clicks -- its
    # field got focus, or the row itself was clicked.
    activated = Signal(int)                   # seq

    def __init__(self, item: CaptureItem, thumb_path: str, parent=None):
        super().__init__(parent)
        self.seq = item.seq
        self.setObjectName("captureRow")   # for _ACTIVE_ROW_STYLE's selector
        self.setFrameShape(QFrame.StyledPanel)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        thumb = QLabel()
        # Built from the ink mask with max-pooling, not a smooth scale of
        # the raw capture -- see thumbnails.py for why thin 1px strokes
        # (and lone dots) used to fade to near-invisible here.
        thumb.setPixmap(readable_thumbnail(thumb_path, _THUMB_SIZE))
        thumb.setFixedSize(_THUMB_SIZE, _THUMB_SIZE)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet("background: #1e1e1e; border-radius: 4px;")
        layout.addWidget(thumb)

        form = QVBoxLayout()
        form.addWidget(QLabel(item.filename))
        flatness_warning = getattr(item, "flatness_warning", None)
        if flatness_warning:
            # Short, fixed text with the full detail as a tooltip --
            # the actual message includes a computed percentage and can
            # run long; keeping the visible row compact matters more
            # here than showing the whole thing inline every time.
            warn_label = QLabel("\u26a0 Low contrast -- may trace noisily")
            warn_label.setStyleSheet("color: #b8860b; font-size: 10px;")
            warn_label.setToolTip(flatness_warning)
            form.addWidget(warn_label)
        self._edit = CodepointLineEdit()
        if item.codepoint is not None:
            self._edit.setText(f"U+{item.codepoint:04X}")
        self._edit.textChanged.connect(self._on_text_changed)
        # commitReady (the fast paths) only moves focus on -- the
        # ACTUAL confirmation/advance logic runs from editingFinished
        # below, which firing on that focus change already covers it.
        # Connecting both to the same handler would run it twice for
        # the same keystroke.
        self._edit.commitReady.connect(self._edit.focusNextChild)
        self._edit.editingFinished.connect(self._on_editing_finished)
        self._edit.installEventFilter(self)
        form.addWidget(self._edit)
        self._status = QLabel("")
        self._status.setStyleSheet("color: #b23; font-size: 10px;")
        form.addWidget(self._status)
        layout.addLayout(form, 1)

        del_btn = QPushButton("Delete")
        # Excluded from Tab order so focusNextChild() (used above to
        # auto-advance) lands on the NEXT ROW's own field directly,
        # rather than pausing on this row's Delete button first.
        del_btn.setFocusPolicy(Qt.ClickFocus)
        del_btn.clicked.connect(lambda: self.delete_requested.emit(self.seq))
        layout.addWidget(del_btn)

    def eventFilter(self, obj, event):
        if obj is self._edit and event.type() == QEvent.FocusIn:
            self.activated.emit(self.seq)
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        self.activated.emit(self.seq)
        super().mousePressEvent(event)

    def set_active(self, on: bool) -> None:
        self.setStyleSheet(_ACTIVE_ROW_STYLE if on else "")

    def assign_from_picker(self, cp: int) -> None:
        """Fills the field as if typed -- textChanged updates the store
        as usual. Confirmation (duplicate/overwrite) is run by the
        window right after, via the same _on_row_commit typing uses."""
        self._edit.setText(f"U+{cp:04X}")

    def set_codepoint_silently(self, cp: int | None) -> None:
        """Bulk assignment (text file): sets the field WITHOUT the
        per-row signals -- the caller has already validated the whole
        batch and updates the store in one go."""
        self._edit.blockSignals(True)
        self._edit.setText(f"U+{cp:04X}" if cp is not None else "")
        self._edit.blockSignals(False)
        self._status.setText("")

    def _on_text_changed(self, text: str):
        cp, err = parse_codepoint(text.strip())
        self._status.setText(err or "")
        self.codepoint_changed.emit(self.seq, cp)

    def _on_editing_finished(self):
        self.commit_requested.emit(self.seq)

    def current_codepoint(self) -> int | None:
        cp, _err = parse_codepoint(self._edit.text().strip())
        return cp

    def revert_codepoint(self) -> None:
        """Clears the field without re-triggering validation/commit
        signals -- used when CaptureWindow declines a conflict on the
        user's behalf (duplicate within the session, or a declined
        overwrite), so the revert itself doesn't loop back into the
        same confirmation logic."""
        self._edit.blockSignals(True)
        self._edit.clear()
        self._edit.blockSignals(False)
        self._status.setText("")


class PickerPanelMixin:
    """The character-table side panel, shared by the Capture window and
    the "Add glyph -> File" assignment dialog -- both list _CaptureRow
    rows, and both want the same "click a character, it goes to the
    highlighted row, move on" flow, so it lives here once.

    The host provides: self._rows (seq -> _CaptureRow, in display
    order), self._scroll (the rows' QScrollArea), self._on_row_commit
    (seq) (its own duplicate/overwrite confirmation),
    self._existing_codepoints_cb() (codes already in the project) and
    optionally _session_codepoints() (codes already used by its own
    rows; the default reads them off the rows). Each row's `activated`
    signal must be connected to self._set_active. The host builds its
    rows column into a QHBoxLayout and then calls _init_picker_panel
    with it; _make_panel_button gives the "Character table" toggle to
    place wherever suits it."""

    _ROW_NOUN = "capture"
    _EMPTY_HINT = "Grab a section first \u2013 captures appear on the left."

    def _make_panel_button(self) -> QPushButton:
        self._active_seq = getattr(self, "_active_seq", None)
        self._panel_btn = QPushButton("Character table \u25B8")
        self._panel_btn.setCheckable(True)
        self._panel_btn.setToolTip("Show or hide the character table on the right")
        self._panel_btn.toggled.connect(self._set_panel_visible)
        return self._panel_btn

    def _init_picker_panel(self, outer, left_width: int, height: int = 600) -> None:
        self._active_seq = getattr(self, "_active_seq", None)
        self._panel = QWidget()
        self._panel.setFixedWidth(_PANEL_WIDTH)
        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(6, 0, 0, 0)
        self._panel_hint = QLabel("")
        self._panel_hint.setWordWrap(True)
        self._panel_hint.setStyleSheet("color: #777777;")
        panel_layout.addWidget(self._panel_hint)
        self._picker = UnicodePickerWidget()
        self._picker.codepointClicked.connect(self._on_picker_clicked)
        panel_layout.addWidget(self._picker, 1)
        outer.addWidget(self._panel)

        if self._rows and self._active_seq is None:
            self._set_active(self._next_unassigned_after(None))
        self._refresh_marks()
        self._update_panel_hint()
        visible = app_settings.get_picker_panel_visible()
        self._panel.setVisible(visible)
        self._panel_btn.blockSignals(True)
        self._panel_btn.setChecked(visible)
        self._panel_btn.blockSignals(False)
        self._update_panel_btn_text()
        self.resize(left_width + (self._panel_share() if visible else 0), height)

    def _session_codepoints(self) -> set[int]:
        return {r.current_codepoint() for r in self._rows.values()
                if r.current_codepoint() is not None}


    def _set_active(self, seq: int | None) -> None:
        if seq == self._active_seq:
            return
        old = self._rows.get(self._active_seq) if self._active_seq is not None else None
        if old is not None:
            old.set_active(False)
        self._active_seq = seq if seq in self._rows else None
        row = self._rows.get(self._active_seq) if self._active_seq is not None else None
        if row is not None:
            row.set_active(True)
            self._scroll.ensureWidgetVisible(row)
        self._update_panel_hint()

    def _next_unassigned_after(self, seq: int | None) -> int | None:
        """First capture without a code AFTER seq (in capture order),
        wrapping round to the start; None when every capture has one."""
        order = list(self._rows.keys())
        if not order:
            return None
        start = order.index(seq) + 1 if seq in self._rows else 0
        for k in order[start:] + order[:start]:
            if k != seq and self._rows[k].current_codepoint() is None:
                return k
        return None

    def _on_picker_clicked(self, cp: int) -> None:
        seq = self._active_seq
        if seq is None:
            seq = self._next_unassigned_after(None)
        row = self._rows.get(seq) if seq is not None else None
        if row is None:
            self._panel_hint.setText(f"Nothing to assign to \u2013 click a {self._ROW_NOUN} first.")
            return
        self._set_active(seq)
        row.assign_from_picker(cp)
        self._on_row_commit(seq)
        if row.current_codepoint() == cp:
            # Accepted (not reverted by a duplicate/overwrite refusal):
            # move on to the next capture still waiting for a code.
            nxt = self._next_unassigned_after(seq)
            if nxt is not None:
                self._set_active(nxt)
        self._update_panel_hint()

    def _refresh_marks(self) -> None:
        if hasattr(self, "_picker"):
            self._picker.set_marked(self._existing_codepoints_cb() | self._session_codepoints())

    def _update_panel_hint(self) -> None:
        if not hasattr(self, "_panel_hint"):
            return
        if self._active_seq is not None and self._active_seq in self._rows:
            pos = list(self._rows.keys()).index(self._active_seq) + 1
            self._panel_hint.setText(
                f"Click a character to assign it to {self._ROW_NOUN} #{pos} (highlighted). "
                "Characters in amber are already in use.")
        elif self._rows:
            self._panel_hint.setText(f"Click a {self._ROW_NOUN}, then a character to assign it.")
        else:
            self._panel_hint.setText(self._EMPTY_HINT)

    def _set_panel_visible(self, on: bool) -> None:
        """Shows/hides the character table, growing or shrinking the
        window rightwards by exactly the panel's own share, so the
        capture column never moves or changes size.

        The target width is worked out from the window's width BEFORE
        the change, not after: showing a child widget makes Qt grow its
        top-level window on its own to fit the new minimum size, so
        adding the panel width to the already-grown window (as the
        first version did) counted the panel twice -- 868 -> 448 ->
        1288 px on repeated toggling. Resizing to an absolute target
        afterwards overrides whatever Qt's own auto-grow did."""
        was = self._panel.isVisible()
        share = self._panel_share()
        before = self.width()
        self._panel.setVisible(on)
        app_settings.remember_picker_panel_visible(on)
        self._update_panel_btn_text()
        if on != was and self.isVisible():
            # activate(): the layout recomputes its minimum lazily; a
            # shrink would otherwise be clamped to the old minimum that
            # still included the now-hidden panel.
            self.layout().activate()
            target = before + share if on else before - share
            self.resize(max(self.minimumSizeHint().width(), target), self.height())
        if on:
            self._refresh_marks()

    def _panel_share(self) -> int:
        """Width the panel adds to the window: its own fixed width plus
        the layout spacing between it and the capture column."""
        return _PANEL_WIDTH + max(0, self.layout().spacing())

    def _update_panel_btn_text(self) -> None:
        self._panel_btn.setText("Character table \u25C2" if self._panel_btn.isChecked()
                                else "Character table \u25B8")


class CaptureWindow(PickerPanelMixin, QDialog):
    # Emitted only via the Commit button -- session_controller does the
    # actual project.add_glyph work and then tells the store to clean up.
    commit_requested = Signal()
    discard_requested = Signal()
    # The "Close (keep pending)" button -- AND the titlebar X / Escape,
    # which now mean the same thing (see closeEvent): there's no separate
    # "collapse to a smaller HUD, session stays alive" state to return to
    # any more, so the least-surprising reading of dismissing this window
    # any way other than Commit/Discard is "suspend the session, keep
    # everything queued, resumable later from Capture Glyphs...". Finalizes
    # the canvas size first if one's still pending -- see
    # CaptureSessionController._on_close_keep_pending.
    close_keep_pending_requested = Signal()
    # Grab controls -- moved here from the old standalone HUD.
    grab_requested = Signal()
    use_canvas_size_requested = Signal()

    def __init__(self, store: CaptureSessionStore, resumed: bool,
                 existing_codepoints_cb=None, parent=None):
        # No parent by default -- this must stay independent of the main
        # window's own hide()/show() cycles during a grab.
        super().__init__(parent)
        self.setWindowTitle("Capture")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self._store = store
        self._active_seq: int | None = None
        self._resolved = False   # set True right before a Commit/Discard/Close-triggered close
        # Queried fresh each time (not snapshotted once) since a
        # partial commit mid-session grows the real project's glyph set
        # while this same window instance stays open -- see
        # session_controller._show_capture_window for what's passed.
        self._existing_codepoints_cb = existing_codepoints_cb or (lambda: set())

        # Two columns: the original capture column on the left, the
        # character-table panel on the right.
        outer = QHBoxLayout(self)
        left = QWidget()
        left.setMinimumWidth(_LEFT_WIDTH - 20)
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(left, 1)

        if resumed:
            banner = QLabel(f"Resumed \u2014 {len(store.items)} pending captures from an earlier session.")
            banner.setWordWrap(True)
            banner.setStyleSheet(
                "background: #3a3320; color: #e8c877; padding: 6px; border-radius: 4px;")
            layout.addWidget(banner)

        # -- grab controls (moved here from the old standalone HUD) --------
        size_row = QHBoxLayout()
        self._size_label = QLabel("")
        size_row.addWidget(self._size_label, 1)
        self._use_canvas_size_btn = QPushButton("Use canvas size")
        self._use_canvas_size_btn.setEnabled(False)
        self._use_canvas_size_btn.setToolTip(
            "No project canvas size available to match yet")
        self._use_canvas_size_btn.clicked.connect(self.use_canvas_size_requested.emit)
        size_row.addWidget(self._use_canvas_size_btn)
        layout.addLayout(size_row)

        self._grab_btn = QPushButton("Grab section")
        self._grab_btn.setToolTip(_GRAB_TOOLTIP)
        self._grab_btn.clicked.connect(self.grab_requested.emit)
        layout.addWidget(self._grab_btn)

        # -- assignment helpers ---------------------------------------------
        assign_row = QHBoxLayout()
        self._from_file_btn = QPushButton("Assign from text file\u2026")
        self._from_file_btn.setToolTip(
            "Read a text file holding the source's own characters in\n"
            "capture order, and assign each capture its code from it.\n"
            "You'll be asked which codepage the file uses, and shown a\n"
            "preview before anything is applied.")
        self._from_file_btn.clicked.connect(self._on_assign_from_file)
        assign_row.addWidget(self._from_file_btn)
        assign_row.addStretch(1)
        assign_row.addWidget(self._make_panel_button())
        layout.addLayout(assign_row)

        # -- captured glyphs -------------------------------------------------
        self._rows_group = QGroupBox("Captured glyphs (0)")
        rows_group_layout = QVBoxLayout(self._rows_group)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.addStretch(1)
        self._scroll.setWidget(self._rows_widget)
        rows_group_layout.addWidget(self._scroll)
        layout.addWidget(self._rows_group, 1)

        self._rows: dict[int, _CaptureRow] = {}
        for item in store.items:
            self._add_row(item)
        self._update_rows_group_title()

        # Unchecked every time a fresh window is constructed -- not a
        # remembered preference across sessions. "Skip all overwrite
        # confirmations" reads as something to consciously opt into per
        # batch, not something that should quietly stay on from
        # whenever it was last checked.
        self._overwrite_check = QCheckBox("Overwrite existing glyphs without asking")
        self._overwrite_check.setToolTip(
            "When checked, assigning a codepoint that already exists in\n"
            "this project replaces it immediately instead of asking each\n"
            "time.")
        layout.addWidget(self._overwrite_check)

        btn_row = QHBoxLayout()
        self._commit_btn = QPushButton("Commit All Assigned")
        self._commit_btn.clicked.connect(self._on_commit)
        btn_row.addWidget(self._commit_btn)

        self._discard_btn = QPushButton("Discard Session")
        self._discard_btn.clicked.connect(self._on_discard)
        btn_row.addWidget(self._discard_btn)

        self._close_btn = QPushButton("Close (keep pending)")
        self._close_btn.setToolTip(
            "Leaves everything captured so far untouched.\n"
            "Resume later from the Capture Glyphs button.")
        self._close_btn.clicked.connect(self.close_keep_pending_requested.emit)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

        self._init_picker_panel(outer, _LEFT_WIDTH)

    # -- grab/canvas-size status (moved here from the old standalone HUD) --

    def set_capture_size(self, size) -> None:
        """size: a QSize, or None if no capture size has been
        established yet this session (before the first rectangle has
        been drawn -- see overlay.py's SHIFT-held two-click gesture)."""
        if size is None:
            self._size_label.setText("Capture size: not set yet -- SHIFT-click two corners to set one")
        else:
            self._size_label.setText(f"Capture size: {size.width()} x {size.height()} px")

    def set_canvas_size_available(self, size) -> None:
        """size: a QSize matching the current project's own,
        genuinely-established canvas size, or None when there's no
        real one to offer yet -- either no project at all, or one
        that's only a still-pending, from-scratch placeholder for this
        very capture session (see CaptureSessionController.
        _on_use_canvas_size's own comment on why that specific case is
        deliberately excluded too, not just "no project"). Controls the
        Use canvas size button's own enabled state and tooltip;
        does NOT itself change the current capture size -- that only
        happens once the button is actually clicked."""
        self._use_canvas_size_btn.setEnabled(size is not None)
        self._use_canvas_size_btn.setToolTip(
            f"Set the capture size to the current project's own canvas size ({size.width()} x {size.height()} px)"
            if size is not None else "No project canvas size available to match yet")

    def set_interact_mode(self, interacting: bool) -> None:
        """Purely a visual cue -- just the grab button's own label/
        enabled state. Actually hiding this window while capturing is
        handled by the controller (see CaptureSessionController.
        _on_grab_requested), via the SAME opacity-based technique
        already proven necessary for excluding a window from a screen
        grab at all (see _exclude_from_capture's own docstring)."""
        self._grab_btn.setText("Grab section" if interacting else "Grabbing\u2026")
        self._grab_btn.setEnabled(interacting)

    # -- captured glyphs ------------------------------------------------------

    def _update_rows_group_title(self) -> None:
        self._rows_group.setTitle(f"Captured glyphs ({len(self._rows)})")

    def set_capture_count(self, n: int) -> None:
        """n is authoritative (from the store) even though len(self._rows)
        would normally agree -- kept as an explicit setter, same as the
        old HUD's, so the controller has one call that's always right
        immediately after a store mutation, without relying on row-list
        bookkeeping already being in sync at that exact instant."""
        self._rows_group.setTitle(f"Captured glyphs ({n})")

    def add_capture(self, item: CaptureItem) -> None:
        """Called by session_controller as new captures arrive."""
        self._add_row(item)
        self._update_rows_group_title()
        if self._active_seq is None:
            self._set_active(item.seq)
        self._update_panel_hint()

    def _add_row(self, item: CaptureItem) -> None:
        row = _CaptureRow(item, self._store.item_path(item))
        row.codepoint_changed.connect(self._on_codepoint_changed)
        row.delete_requested.connect(self._on_delete_requested)
        row.commit_requested.connect(self._on_row_commit)
        row.activated.connect(self._set_active)
        self._rows[item.seq] = row
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)

    def _on_codepoint_changed(self, seq: int, codepoint) -> None:
        self._store.update_codepoint(seq, codepoint)
        self._refresh_marks()

    def _session_codepoints(self) -> set[int]:
        return {i.codepoint for i in self._store.items if i.codepoint is not None}

    # -- assign from text file ---------------------------------------------

    def _on_assign_from_file(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "Nothing to assign", "No captures in this session yet.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Text file with the captured characters, in capture order",
            app_settings.get_last_browsed_dir(), "Text files (*.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            QMessageBox.warning(self, "Couldn't read file", str(e))
            return
        rows = [CaptureRowInfo(i.seq, self._store.item_path(i), i.codepoint)
                for i in self._store.items if i.seq in self._rows]
        dlg = TextFileAssignDialog(self, path, data, rows,
                                   existing_cps=self._existing_codepoints_cb(),
                                   include_existing_default=self._overwrite_check.isChecked())
        if dlg.exec() != QDialog.Accepted or not dlg.result_assignments:
            return
        for seq, cp in dlg.result_assignments.items():
            self._rows[seq].set_codepoint_silently(cp)
        self._store.update_codepoints(dlg.result_assignments)
        self._refresh_marks()
        self._set_active(self._next_unassigned_after(None))
        assigned = sum(1 for cp in dlg.result_assignments.values() if cp is not None)
        self._panel_hint.setText(f"Assigned {assigned} capture(s) from {os.path.basename(path)}.")

    def _on_row_commit(self, seq: int) -> None:
        """Runs once per field, when the user is actually done with it
        -- NOT per keystroke (see _CaptureRow's own docstring on this
        distinction). This is where overwrite/duplicate confirmation
        happens now, immediately as each assignment is made, rather
        than as a burst of separate popups at commit time for however
        many conflicts happened to be typed in."""
        row = self._rows.get(seq)
        if row is None:
            return
        cp = row.current_codepoint()
        if cp is None:
            return   # empty or invalid entry -- nothing to confirm

        # Duplicate WITHIN this session -- checked first since it's
        # unconditional (there's never a legitimate reason for two
        # queued captures to target the same codepoint at once, unlike
        # overwriting an existing project glyph, which is a real,
        # intentional action below).
        for other_seq, other_row in self._rows.items():
            if other_seq != seq and other_row.current_codepoint() == cp:
                QMessageBox.warning(
                    self, "Already assigned this session",
                    f"U+{cp:04X} is already assigned to another capture in this session.")
                row.revert_codepoint()
                self._store.update_codepoint(seq, None)
                self._refresh_marks()
                return

        # Collision with an EXISTING project glyph -- confirmed here,
        # once, at the moment it's typed in. Commit time no longer
        # re-asks this (see MainWindow._commit_capture_session) -- that
        # was the whole source of "a popup per conflict, all at once"
        # this replaces. The "Overwrite existing glyphs" checkbox skips
        # this confirmation entirely when checked -- unchecked by
        # default every time this window is freshly constructed (see
        # its own setup in __init__).
        if cp in self._existing_codepoints_cb() and not self._overwrite_check.isChecked():
            confirm = QMessageBox(
                QMessageBox.Question, "Already exists",
                f"U+{cp:04X} is already in this project. Overwrite it when committed?",
                QMessageBox.Yes | QMessageBox.No, self)
            confirm.setWindowFlags(confirm.windowFlags() | Qt.WindowStaysOnTopHint)
            confirm.setDefaultButton(QMessageBox.No)
            if confirm.exec() != QMessageBox.Yes:
                row.revert_codepoint()
                self._store.update_codepoint(seq, None)
                self._refresh_marks()
                return
        # Approved (or nothing needed approving) -- the value is
        # already stored via codepoint_changed, nothing further to do.

    def remove_row_silently(self, seq: int) -> None:
        """Removes a row from the UI only -- used by session_controller
        after it has ALREADY updated the store itself (e.g. right after
        a successful commit), so this must not re-touch the store."""
        self._drop_row(seq)

    def _on_delete_requested(self, seq: int) -> None:
        self._store.remove_item(seq)
        self._drop_row(seq)

    def _drop_row(self, seq: int) -> None:
        if seq == self._active_seq:
            nxt = self._next_unassigned_after(seq)
            self._set_active(nxt if nxt != seq else None)
        row = self._rows.pop(seq, None)
        if row is not None:
            row.setParent(None)
            row.deleteLater()
        if self._active_seq == seq:
            self._active_seq = None
        self._update_rows_group_title()
        self._refresh_marks()
        self._update_panel_hint()

    def _on_commit(self) -> None:
        items = self._store.items
        if not items:
            QMessageBox.information(self, "Nothing to commit", "No captures in this session yet.")
            return
        # Forces any field still being actively edited to fire its own
        # editingFinished (and therefore run the overwrite/duplicate
        # check above) before commit proceeds -- clicking Commit itself
        # already does this via ordinary Qt focus-out handling in
        # practice, but this makes it explicit rather than relying on
        # that incidentally being true.
        self.setFocus()
        unassigned = [i for i in items if i.codepoint is None]
        if unassigned:
            reply = QMessageBox.question(
                self, "Unassigned captures",
                f"{len(unassigned)} capture(s) have no codepoint assigned and will be "
                "left pending (not committed). Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
        self._resolved = True
        self.commit_requested.emit()

    def _on_discard(self) -> None:
        reply = QMessageBox.question(
            self, "Discard session",
            "Discard every capture in this session? This can't be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self._resolved = True
        self.discard_requested.emit()

    def mark_resolved(self) -> None:
        """Suppresses closeEvent's own close_keep_pending_requested --
        called by the controller right before it deliberately closes
        this window for real (a commit, a discard, or an already-
        handled close-keep-pending), so the same dismissal doesn't fire
        twice."""
        self._resolved = True

    def reject(self) -> None:
        """QDialog's own default Escape handling calls reject(), which
        by itself only hides the widget (QDialog.done() -> hide()) --
        confirmed by direct testing, not assumed -- WITHOUT ever
        invoking closeEvent. That skips the exact logic closeEvent
        depends on (ignore-unless-resolved, emit
        close_keep_pending_requested) for Escape to mean the same thing
        as clicking Close (keep pending) or the titlebar X, which DOES
        go through the normal window-close mechanism and therefore
        does reach closeEvent on its own. Routing Escape through
        close() here makes it take the identical path."""
        self.close()

    def closeEvent(self, event):
        """Ignores the close (keeps the window open) whenever it isn't
        already resolved -- X/Escape and the "Close (keep pending)"
        button both funnel through the SAME close_keep_pending_requested
        signal now, whose handler (CaptureSessionController.
        _on_close_keep_pending) can itself finalize a still-pending
        canvas size first and may be cancelled by the user partway
        through that prompt. If this accepted the close unconditionally
        (as a resolved close correctly does, below), the window would
        already be gone by the time a cancelled prompt tried to leave
        it open -- exactly the "stays open, nothing to undo" behavior
        the button-only path always relied on (see mark_resolved's own
        docstring: only the controller's own deliberate, POST-prompt
        close() call ever finds self._resolved already True here)."""
        if not self._resolved:
            event.ignore()
            self.close_keep_pending_requested.emit()
            return
        event.accept()
