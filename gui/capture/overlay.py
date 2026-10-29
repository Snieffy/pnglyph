"""
The overlay shown during the "Capture" state (see session_controller.py
for the Interact/Capture/Suspended state machine). One instance covers
exactly one "grab" -- a single frozen snapshot of the current on-screen
section, which the user may crop from any number of times before
returning to Interact to zoom/pan the reference app and grab a fresh
section.

Gesture model (replaced an earlier click-vs-click-and-hold timing
heuristic -- eyeballing a start point was difficult with an existing
fixed-size rectangle covering it the whole time):

  - Plain left-click: commits the current FIXED-SIZE rectangle at the
    cursor's (possibly CTRL-snapped) position. This is the normal,
    fast path once a size is established.
  - SHIFT held: hides the fixed-size preview entirely (nothing to
    eyeball around/through) and switches to two discrete clicks --
    click one corner, move, click the opposite corner -- rather than a
    press-drag-release. SHIFT does NOT need to stay held for the
    second click; only the FIRST click's modifier state matters, since
    requiring it through the whole gesture would reintroduce the same
    "hold two things at once" friction this replaced. Escape while a
    first corner is pending cancels just that pending rectangle (back
    to normal fixed-size mode), not the whole overlay -- a second
    Escape (nothing pending) does that.
  - Right-click: always cancels the whole overlay back to Interact,
    regardless of any pending state -- a mouse-only equivalent of
    Escape, useful since the mouse is already in hand.
  - CTRL held: local corner/line/point snapping (see line_detection.py)
    applies to whichever point is currently being positioned, in both
    of the above modes independently.

Coordinates: everything on this widget -- fixed_size, emitted rects --
is in the SAME local pixel space as the stored desktop_pixmap (i.e. this
widget's own geometry maps 1:1 onto that pixmap, already corrected for
each source screen's device pixel ratio at grab time -- see
session_controller._grab_virtual_desktop).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRect, QPoint, QSize, Signal
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import QWidget

from .. import style
from . import line_detection

_LOCAL_SNAP_PATCH_RADIUS = 30   # px around the cursor searched when CTRL is held

_SNAP_COLORS = {
    "corner": "#2ecc71",   # green -- strongest signal, both axes locked
    "line":   "#5b8def",   # matches style.GUIDE_COLOR -- one axis locked
    "point":  "#e0a52f",   # weakest signal, per-axis only
}


class CaptureOverlay(QWidget):
    # Emitted with the FINAL (possibly snapped) capture rectangle, in
    # this widget's local pixel coordinates -- session_controller crops
    # from its own stored copy of the same pixmap, not from a re-grab.
    captured = Signal(QRect)
    # Esc with nothing pending: cancel back to Interact WITHOUT ending
    # the whole session -- distinct from Close (keep pending)/Discard
    # in the capture window. Also reachable via right-click (see
    # mouseReleaseEvent).
    cancelled_to_interact = Signal()

    def __init__(self, desktop_pixmap, fixed_size: QSize | None, snap_enabled: bool = False, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)
        # Explicit, not left at QWidget's own default (Qt.NoFocus) --
        # confirmed as a real, reported cause of ESC/CTRL appearing to
        # do nothing until some other interaction "woke up" the
        # overlay first: without an explicit policy that actually
        # allows this widget to hold keyboard focus, a later
        # setFocus() call has nothing to grant, regardless of how the
        # window itself gets activated.
        self.setFocusPolicy(Qt.StrongFocus)
        self._pixmap = desktop_pixmap
        # fixed_size now typically arrives already known -- both the
        # single-shot Capture button and the full session read the
        # SAME persisted "last capture size" (app_settings.
        # get_last_capture_size) before ever constructing this, so a
        # genuinely brand-new capture rectangle is now the EXCEPTION,
        # not the normal first-ever case it used to be. An earlier
        # version had a separate always_two_click flag specifically for
        # the single-shot flow (which never used to have ANY notion of
        # a reusable size at all) -- removed now that both flows share
        # the same persisted memory and the same SHIFT-to-redefine
        # gesture; there's no longer a flow-specific distinction left to
        # make here.
        self._fixed_size = fixed_size
        # Computed once per grab, sliced into small patches on demand
        # rather than searched all at once -- see line_detection's own
        # docstring for why a full-image search was dropped.
        self._gray = line_detection.qimage_to_gray(desktop_pixmap.toImage())

        self._cursor_pos = QPoint(0, 0)  # last known mouse pos
        # A persistent TOGGLE, not a held-modifier state -- a single
        # CTRL press flips it and it stays that way (across mouse
        # movement, across SHIFT being held or not, across a fresh
        # grab -- see session_controller, which carries this value
        # forward between overlay instances the same way it already
        # does for _fixed_size). Deliberately not tied to holding the
        # key down: needing to hold CTRL at the same time as SHIFT (for
        # the two-click gesture) was exactly the two-things-at-once
        # friction this was meant to remove.
        self._snap_enabled = snap_enabled
        self._shift_held = False
        # Set the moment a SHIFT-click establishes a first corner;
        # cleared on commit, on Escape (cancels just this), or if the
        # overlay closes entirely.
        self._pending_corner: QPoint | None = None
        self._pending_corner_kind: str = "none"
        self._capture_count: int | None = None   # None = don't show (see set_capture_count)

        self.setGeometry(0, 0, desktop_pixmap.width(), desktop_pixmap.height())
        self.setMouseTracking(True)

    @property
    def snap_enabled(self) -> bool:
        """Read by session_controller after this overlay closes/cancels
        so the NEXT grab's overlay is constructed with the toggle in
        whatever state it was left in -- see the constructor's own
        note on why this persists rather than resetting per grab."""
        return self._snap_enabled

    def set_capture_count(self, n: int | None) -> None:
        """Attached to the capture rectangle's own dimension label (see
        paintEvent) rather than shown as a separate, persistent corner
        readout -- confirmed as a real, reported problem with the
        latter: it relied on the HUD being fully covered by this
        overlay to justify existing at all, but the HUD stays visible
        on top of it in practice, showing a stale, unchanging count for
        the entire time this overlay is up (whatever it happened to
        read at Interact-state entry, never live-updated visibly
        regardless of whether the underlying value was). Attaching the
        count to the rectangle itself instead means it's always right
        where the user's own attention already is while positioning a
        capture -- no separate glance at a corner of the screen (or
        reliance on the HUD repainting promptly while this overlay has
        active mouse/keyboard focus) required at all."""
        self._capture_count = n
        self.update()

    @property
    def fixed_size(self) -> QSize | None:
        """Reflects the latest size established by a capture on THIS
        overlay instance -- session_controller reads this after each
        `captured` emission to carry the size forward into the next
        grab's overlay (a fresh instance each time)."""
        return self._fixed_size

    @property
    def pixmap(self):
        return self._pixmap

    # -- local snap detection -------------------------------------------------

    def _local_snap(self, pt: QPoint) -> tuple[QPoint, str]:
        """Returns (possibly-adjusted point, snap kind). Best-effort,
        never blocking: with the snap toggle off, or nothing found
        nearby, returns (pt, "none") completely unchanged -- a missed
        or skipped snap costs a manual touch-up later, never the
        capture itself."""
        if not self._snap_enabled:
            return pt, "none"
        r = _LOCAL_SNAP_PATCH_RADIUS
        x0 = max(0, pt.x() - r)
        y0 = max(0, pt.y() - r)
        x1 = min(self._gray.shape[1], pt.x() + r)
        y1 = min(self._gray.shape[0], pt.y() + r)
        if x1 - x0 < 6 or y1 - y0 < 6:
            return pt, "none"
        patch = self._gray[y0:y1, x0:x1]
        result = line_detection.detect_local_snap_safe(patch)
        if not result.found:
            return pt, "none"
        new_x = pt.x() if result.x is None else x0 + result.x
        new_y = pt.y() if result.y is None else y0 + result.y
        return QPoint(int(round(new_x)), int(round(new_y))), result.kind

    def _current_hover_rect(self) -> tuple[QRect | None, str]:
        """Fixed-size cursor preview -- only relevant when neither SHIFT
        mode nor a pending two-click corner is active."""
        if self._fixed_size is None:
            return None, "none"
        anchor, kind = self._local_snap(self._cursor_pos)
        return QRect(anchor, self._fixed_size), kind

    def _current_pending_rect(self) -> tuple[QRect, QPoint, str]:
        """The live two-click rectangle: fixed first corner (already
        snapped and locked in at click time) to the CURRENTLY live-
        snapped cursor position. Returns the raw second point `b`
        alongside the normalized rect -- QRect.normalized() reassigns
        which corner counts as topLeft/bottomRight based on relative
        x/y, so rect.bottomRight() is NOT reliably "the second point"
        (it can silently mix one axis from each corner whenever the
        cursor is in a quadrant that flips one of them, which is
        exactly what produced a marker that looked disconnected from
        the pointer, stuck aligned to the first corner on one axis)."""
        b, kind_b = self._local_snap(self._cursor_pos)
        return QRect(self._pending_corner, b).normalized(), b, kind_b

    # -- mouse handling -------------------------------------------------------

    def mousePressEvent(self, event):
        self._shift_held = bool(event.modifiers() & Qt.ShiftModifier)

    def mouseMoveEvent(self, event):
        self._shift_held = bool(event.modifiers() & Qt.ShiftModifier)
        self._cursor_pos = event.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, event):
        self._shift_held = bool(event.modifiers() & Qt.ShiftModifier)
        self._cursor_pos = event.position().toPoint()

        if event.button() == Qt.RightButton:
            # Always cancels the whole overlay, regardless of any
            # pending first-corner state -- a plain, unconditional
            # mouse-only equivalent of Escape.
            self.cancelled_to_interact.emit()
            self.close()
            return

        if event.button() != Qt.LeftButton:
            return

        if self._pending_corner is not None:
            # Second click of a SHIFT-started two-click rectangle --
            # SHIFT does not need to still be held (see module docstring).
            rect, _b, _kind_b = self._current_pending_rect()
            self._pending_corner = None
            self._pending_corner_kind = "none"
            if rect.width() > 0 and rect.height() > 0:
                self._fixed_size = rect.size()
                self.captured.emit(rect)
            self.update()
            return

        if self._shift_held:
            # First click of a new two-click rectangle -- record the
            # (already snapped) corner and wait for the second click.
            # Nothing is emitted yet.
            anchor, kind = self._local_snap(self._cursor_pos)
            self._pending_corner = anchor
            self._pending_corner_kind = kind
            self.update()
            return

        # Plain click: commit the fixed-size rectangle at the current
        # position, if one exists yet.
        rect, _kind = self._current_hover_rect()
        if rect is not None:
            self.captured.emit(rect)
        # else: no fixed size established yet (first capture of the
        # session) -- a plain click has no sensible size to use, so
        # it's silently ignored. The Grab section button's own tooltip
        # explains SHIFT-clicking two corners to set one.
        self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self._pending_corner is not None:
                # Cancel just the pending rectangle -- back to normal
                # fixed-size mode, overlay stays open. A second Escape
                # (nothing pending) is what closes the overlay.
                self._pending_corner = None
                self._pending_corner_kind = "none"
                self.update()
                return
            self.cancelled_to_interact.emit()
            self.close()
            return
        if event.key() == Qt.Key_Control:
            # A TOGGLE, not a hold -- guard against key auto-repeat
            # (holding a key down generates repeated press events on
            # most platforms), which would otherwise flip this back and
            # forth many times a second for as long as it's held rather
            # than once per physical press.
            if not event.isAutoRepeat():
                self._snap_enabled = not self._snap_enabled
                self.update()
            return
        if event.key() == Qt.Key_Shift:
            self._shift_held = True
            self.update()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key_Shift:
            self._shift_held = False
            self.update()
            return
        super().keyReleaseEvent(event)

    # -- painting -------------------------------------------------------------

    def _draw_snap_marker(self, p: QPainter, pt: QPoint, kind: str) -> None:
        if kind == "none":
            return
        color = QColor(_SNAP_COLORS.get(kind, "#ffffff"))
        pen = QPen(color)
        pen.setWidth(2)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(pt, 6, 6)
        if kind == "corner":
            p.drawLine(pt.x() - 10, pt.y(), pt.x() + 10, pt.y())
            p.drawLine(pt.x(), pt.y() - 10, pt.x(), pt.y() + 10)

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._pixmap)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))   # dim the frozen desktop

        rect = None
        snap_markers: list[tuple[QPoint | None, str]] = []

        if self._pending_corner is not None:
            rect, b, kind_b = self._current_pending_rect()
            snap_markers = [
                (self._pending_corner, self._pending_corner_kind),
                # The raw second point `b` -- NOT rect.bottomRight()
                # (see _current_pending_rect's own docstring for why
                # that was the actual bug).
                (b if kind_b != "none" else None, kind_b),
            ]
        elif self._shift_held:
            # SHIFT held, no first corner yet -- deliberately draw NO
            # rectangle at all (that's the whole point: nothing to
            # eyeball a start point around/through). Just a crosshair-
            # style marker at the cursor, plus its snap marker if CTRL
            # is also held.
            anchor, kind = self._local_snap(self._cursor_pos)
            pen = QPen(QColor(style.POPUP_PREVIEW_COLOR))
            pen.setWidth(1)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.drawLine(anchor.x() - 12, anchor.y(), anchor.x() + 12, anchor.y())
            p.drawLine(anchor.x(), anchor.y() - 12, anchor.x(), anchor.y() + 12)
            snap_markers = [(anchor if kind != "none" else None, kind)]
        else:
            rect, kind = self._current_hover_rect()
            snap_markers = [(rect.topLeft() if rect is not None and kind != "none" else None, kind)]

        if rect is not None:
            cursor_pen = QPen(QColor(style.POPUP_PREVIEW_COLOR))
            cursor_pen.setWidth(2)
            cursor_pen.setCosmetic(True)
            p.setPen(cursor_pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect)

            # Live pixel-dimension readout -- doubles as a zoom-
            # consistency check: a size that suddenly differs from
            # the last committed capture is the visible sign of a zoom
            # change mid-session. The running capture count is attached
            # here too now, rather than shown as a separate, persistent
            # corner readout -- confirmed as a real, reported problem
            # with the latter: it relied on the capture window being
            # fully covered by this overlay to justify existing at all,
            # but the capture window doesn't actually get hidden while
            # capturing on every setup, leaving a stale, unchanging
            # count on screen for as long as this overlay stayed up
            # regardless of how many captures actually happened. Right
            # here instead, it's exactly where the user's own attention
            # already is while positioning a capture -- no separate
            # glance elsewhere, or reliance on some OTHER widget
            # repainting promptly while this overlay holds active
            # mouse/keyboard focus, required at all. Capture size and
            # the snap toggle's own on/off state no longer get an
            # on-screen readout of their own at all -- both are already
            # explained in the Grab section button's own tooltip,
            # simple enough to not need repeating here, and the capture
            # window itself is visible again the moment this overlay isn't.
            label = f"{rect.width()} x {rect.height()}"
            if self._capture_count is not None:
                label += f"   \u2022   {self._capture_count} captured"
            label_pos = QPoint(rect.left(), max(0, rect.top() - 6))
            p.setPen(QColor("#ffffff"))
            p.drawText(label_pos, label)

        for pt, kind in snap_markers:
            if pt is not None:
                self._draw_snap_marker(p, pt, kind)

        p.end()

