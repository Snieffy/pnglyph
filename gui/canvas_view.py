from __future__ import annotations

import math
import copy

from PySide6.QtCore import Qt, Signal, QPointF, QRectF, QPoint, QSize, QEvent
from PySide6.QtGui import QPen, QBrush, QColor, QPainterPath, QPainter, QPainterPathStroker, QFont, QFontMetrics, QCursor
from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsLineItem,
    QGraphicsRectItem, QGraphicsPathItem, QGraphicsTextItem, QGraphicsItem,
    QApplication, QStyle, QWidget,
)

from core import geometry
from . import style

BASE_POINT_R = 5   # see DraggablePoint.__init__ -- the reference size _visual_scale multiplies from
INK_COLOR = QColor(style.INK_COLOR)
GUIDE_COLOR = QColor(style.GUIDE_COLOR)


def _point_to_segment_distance(p, a, b) -> float:
    """Shortest distance from point p to the line segment a-b (all
    QPointF, all in the same coordinate space) -- clamps the
    projection onto the segment rather than the infinite line, so a
    click past either end measures to that endpoint, not to a
    point beyond it. Used by _segment_or_point_at to resolve which of
    several overlapping segments' hit regions a click actually meant."""
    ax, ay = a.x(), a.y()
    bx, by = b.x(), b.y()
    px, py = p.x(), p.y()
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    proj_x, proj_y = ax + t * dx, ay + t * dy
    return ((px - proj_x) ** 2 + (py - proj_y) ** 2) ** 0.5


def _closest_point_on_segment(p, a, b) -> tuple[float, float]:
    """Companion to _point_to_segment_distance, sharing the exact same
    clamped-projection math, but returning the actual closest (x, y)
    on the segment rather than just its distance from p -- needed for
    snap-to-line, which has to know WHERE to move the dragged point
    to, not merely whether it's close enough."""
    ax, ay = a.x(), a.y()
    bx, by = b.x(), b.y()
    px, py = p.x(), p.y()
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return (ax, ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return (ax + t * dx, ay + t * dy)


def paste_anchor_point(clip_paths: list) -> tuple[float, float]:
    """The single, actual endpoint (never a synthetic bounding-box
    corner that may not correspond to any real vertex) that
    paste-follows-cursor placement anchors to -- confirmed as the
    wanted behavior, valid across a single segment, a polyline, or a
    group: using a REAL point here means it lands exactly on a snap
    target when placed near one, giving at least that one point of the
    pasted content precise, snapped alignment with the rest of the
    glyph. A synthetic bounding-box corner wouldn't actually align any
    real vertex to wherever it snapped to -- the snap would be spent on
    a point that doesn't exist in the geometry at all.

    "Top-left" here means smallest y first (this app's canvas uses
    scene/screen-style y-down coordinates, so smaller y is higher up),
    smallest x as the tie-break -- the same reading-order convention
    (top row first, then left-to-right within it) a person would use
    picking the "top-left" point out of a scattered set by eye.

    Shared between GlyphCanvas.start_paste_mode (the live ghost
    preview) and MainWindow._on_paste_placed (the actual insertion),
    which each need this same point independently -- the latter
    recomputes it from its own clipboard data rather than reading back
    the canvas's own private state (see _on_paste_placed's own
    docstring), so keeping the actual selection logic in exactly one
    place avoids the two ever drifting apart."""
    return min((pt for path in clip_paths for pt in path), key=lambda pt: (pt[1], pt[0]))


def _paths_to_qpath(paths: list, half_width: float, cap_style: str) -> QPainterPath | None:
    """Shared conversion from skeleton paths to a filled outline path --
    used both for the main ink rendering and the onion-skin overlay, so
    the two never drift into subtly different thickening logic."""
    geom = geometry.thicken(paths, half_width, cap_style=cap_style)
    if geom is None or geom.is_empty:
        return None
    qpath = QPainterPath()
    polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    for poly in polys:
        if poly.is_empty:
            continue
        ext = list(poly.exterior.coords)
        if len(ext) < 3:
            continue
        qpath.moveTo(*ext[0])
        for x, y in ext[1:]:
            qpath.lineTo(x, y)
        qpath.closeSubpath()
        for interior in poly.interiors:
            pts = list(interior.coords)
            qpath.moveTo(*pts[0])
            for x, y in pts[1:]:
                qpath.lineTo(x, y)
            qpath.closeSubpath()
    return qpath
BASELINE_COLOR = QColor(style.BASELINE_COLOR)
BOX_COLOR = QColor(style.BOX_COLOR)
POINT_COLOR = QColor(style.POINT_COLOR)
POINT_ACTIVE_COLOR = QColor(style.POINT_SELECTED_COLOR)
SKELETON_COLOR = QColor(style.SKELETON_COLOR)
SEGMENT_SELECTED_COLOR = QColor(style.HIGHLIGHT_SEGMENT_COLOR)
SEGMENT_FAMILY_COLOR = QColor(style.GUIDE_COLOR)   # same path, not itself selected
POPUP_PREVIEW_COLOR = QColor(style.POPUP_PREVIEW_COLOR)
GRID_COLOR = QColor(style.GRID_COLOR)
GROUP_DRAG_THRESHOLD = 3.0   # scene units of movement before a press-on-a-selected-segment
                              # counts as the start of a group-offset drag rather than a click


class DraggablePoint(QGraphicsEllipseItem):
    """A vertex marker. Purely visual and inert until its owning segment
    is selected ("activated") -- inactive points don't accept mouse
    events at all, so clicks pass straight through to whichever
    SegmentItem is underneath instead of landing on an ambiguous stack
    of same-position points. Once active (because a segment touching it
    is selected), it becomes draggable exactly like before."""

    def __init__(self, canvas: "GlyphCanvas", path_index: int, point_index: int, x: float, y: float):
        # Radius scales with the glyph's own cap height (see
        # GlyphCanvas._visual_scale's own comment for the full
        # reasoning) rather than sitting at a fixed pixel size -- BASE_POINT_R
        # is what that fixed size used to be outright, kept as the
        # reference value the scale factor multiplies from, not as a
        # constant in its own right anymore.
        r = BASE_POINT_R * (canvas._visual_scale if canvas is not None else 1.0)
        super().__init__(-r, -r, r * 2, r * 2)
        self.canvas = canvas
        self.path_index = path_index
        self.point_index = point_index
        self.setPos(x, y)
        self.setBrush(QBrush(POINT_COLOR))
        self.setPen(QPen(Qt.NoPen))
        self.setZValue(10)
        self.setAcceptedMouseButtons(Qt.NoButton)   # inert until activated
        self._active = False

    def set_active(self, on: bool):
        self._active = on
        self.setBrush(QBrush(POINT_ACTIVE_COLOR if on else POINT_COLOR))
        self.setFlag(QGraphicsEllipseItem.ItemIsMovable, on)
        self.setFlag(QGraphicsEllipseItem.ItemSendsGeometryChanges, on)
        self.setAcceptedMouseButtons(Qt.LeftButton if on else Qt.NoButton)

    def set_highlighted(self, on: bool):
        """A third, distinct visual state from set_active: highlighted
        (same strength as a selected segment) but explicitly NOT
        draggable -- used for points belonging to a SHIFT-click whole-
        unit selection, where the whole polyline/group is being acted
        on as one thing and an individual endpoint can't be moved on
        its own (group-offset-drag with nearest-point snap is still
        how the whole selection moves together). Never combined with
        set_active(True) -- a point is either individually draggable,
        highlighted-but-locked, or plain idle, never two at once."""
        self._active = False
        self.setBrush(QBrush(SEGMENT_SELECTED_COLOR if on else POINT_COLOR))
        self.setFlag(QGraphicsEllipseItem.ItemIsMovable, False)
        self.setFlag(QGraphicsEllipseItem.ItemSendsGeometryChanges, False)
        self.setAcceptedMouseButtons(Qt.NoButton)

    def itemChange(self, change, value):
        if change == QGraphicsEllipseItem.ItemPositionChange and self.canvas is not None and self._active:
            new_pos: QPointF = value
            # Shared exclusion-computation, used by BOTH angle-snap and
            # ordinary snap below -- angle-snap's own point/line
            # consideration (see apply_angle_snap) needs exactly the
            # same exclusion ordinary dragging already does, not just
            # the non-angle path, now that it's no longer a completely
            # separate code path.
            #
            # Excludes every point sitting at this drag's own STARTING
            # position, not just this one's own identity -- see
            # _points_coincident_with's own docstring for why excluding
            # only "this exact point" wasn't enough. Uses the canvas's
            # own _point_drag_start (the self-contained drag tracker's
            # own record of where the drag began), not
            # self.point_index's CURRENT path coordinate, which would
            # already reflect wherever this drag has moved to by the
            # current tick, not where it started.
            #
            # The exclusion itself is a one-way latch, not a live "am I
            # near start RIGHT NOW" check -- confirmed as a real,
            # reported bug otherwise: whatever this point started
            # coincident with would stay unreachable as a snap target
            # for the rest of the drag EVEN AFTER moving well away from
            # it, since checking live would exclude it again the
            # instant the cursor happened to pass back near that spot,
            # however briefly or unintentionally. Once actually moved
            # past snap_point_tolerance from the start even once,
            # _point_drag_escaped_start latches True for the rest of
            # this drag (reset on the next press) -- every point,
            # including whatever this one started on, becomes a
            # completely normal, available snap target again, including
            # a later, deliberate move back onto it.
            start = self.canvas._point_drag_start
            if start is not None and not self.canvas._point_drag_escaped_start:
                dx = new_pos.x() - start.x()
                dy = new_pos.y() - start.y()
                if (dx * dx + dy * dy) ** 0.5 > self.canvas.snap_point_tolerance:
                    self.canvas._point_drag_escaped_start = True

            if start is not None and not self.canvas._point_drag_escaped_start:
                exclude = self.canvas._points_coincident_with(start.x(), start.y())
                exclude.add((self.path_index, self.point_index))
                exclude_lines = self.canvas._lines_coincident_with(start.x(), start.y())
            else:
                exclude = {(self.path_index, self.point_index)}
                exclude_lines = set()
            # This point's OWN structurally-adjacent segment(s) are
            # excluded from the line-snap half unconditionally, not
            # just while within the escape-latch's own window -- unlike
            # the starting-coincidence case above, this isn't about
            # where the drag began: one of this own segment's two
            # endpoints IS the point being dragged, at every tick, for
            # the entire drag, so its line's distance to wherever the
            # cursor is heading next stays near zero throughout --
            # without this, the point would effectively stick to its
            # own line's trajectory and never be able to move freely
            # towards anything else at all.
            exclude_lines.add((self.path_index, self.point_index))
            exclude_lines.add((self.path_index, self.point_index - 1))

            if self.canvas.angle_snap_enabled:
                # Angle-snap now ADDS a condition on top of grid/point
                # snap rather than replacing them entirely -- see
                # apply_angle_snap's own docstring for the full
                # reasoning (points/lines/grid/boundary all expressed
                # as a distance along the already-chosen ray, instead
                # of competing for x/y directly). clamp_to_source_bounds
                # is deliberately NOT called again afterward here --
                # apply_angle_snap already enforces the boundary along
                # the ray itself; re-clamping x/y independently
                # afterward would silently reintroduce the exact
                # off-ray drift this was built to fix.
                snapped = self.canvas.apply_angle_snap(
                    new_pos.x(), new_pos.y(), self.canvas._angle_reference(self),
                    exclude=exclude, exclude_lines=exclude_lines)
            else:
                snapped = self.canvas.apply_snap(new_pos.x(), new_pos.y(),
                                                  exclude=exclude, exclude_lines=exclude_lines)
                snapped = self.canvas.clamp_to_source_bounds(*snapped)
            value = QPointF(*snapped)
            self.canvas.on_point_moved(self.path_index, self.point_index, snapped[0], snapped[1])
        return super().itemChange(change, value)

    # No mousePressEvent/mouseReleaseEvent here -- confirmed as dead
    # code and removed (see the integrity check that found this):
    # GlyphCanvas's own mousePressEvent (the view-level override) never
    # calls super().mousePressEvent() in any branch, which is what
    # would normally let Qt's QGraphicsScene dispatch a press to
    # whichever item is under the cursor -- every click is fully
    # intercepted and handled explicitly at the view level instead (see
    # _point_drag_item and friends). Since a native release can only
    # ever be delivered to an item that already received a matching
    # native press, this item's own per-item handlers could never
    # actually fire. Exactly the same situation SegmentItem's own
    # docstring already documents for itself (leftover from a version
    # that predates the canvas's current "self-contained, explicit"
    # drag system) -- this class just hadn't had the same note added
    # yet.


class SegmentItem(QGraphicsLineItem):
    """One skeleton segment between two consecutive path points. This,
    not the points themselves, is the primary click target: clicking a
    segment selects it and activates (makes draggable) its two
    endpoints. Segments rarely overlap the way points at a shared
    junction do, so clicking is unambiguous -- pick the line you mean,
    rather than guessing which of several stacked points you hit.

    Selection and group-offset-drag handling both actually live on
    GlyphCanvas (mousePressEvent etc.), NOT here -- the canvas does its
    own hit-testing (_segment_or_point_at) and handles a SegmentItem hit
    directly, returning without ever calling super().mousePressEvent(),
    so this item's own mouse handlers would never actually be invoked by
    a real click. An earlier version of this class had mouse handlers
    for exactly this purpose that looked correct in isolation but were
    silently dead code in the running app."""

    def __init__(self, canvas: "GlyphCanvas", path_index: int, seg_index: int,
                 point_a: DraggablePoint, point_b: DraggablePoint):
        super().__init__(point_a.pos().x(), point_a.pos().y(), point_b.pos().x(), point_b.pos().y())
        self.canvas = canvas
        self.path_index = path_index
        self.seg_index = seg_index          # index of point_a within its path
        self.point_a = point_a
        self.point_b = point_b
        # Width scales with cap height (see GlyphCanvas._visual_scale),
        # same reasoning as the endpoint dots -- a fixed pixel width
        # looked proportionate only at the one canvas size it was
        # originally tuned against. The hit region for clicking/
        # selecting this segment is NOT this width -- see shape()
        # below, which deliberately uses the fixed, weight-independent
        # click_line_tolerance instead, unaffected by this at all.
        scale = canvas._visual_scale if canvas is not None else 1.0
        self.setPen(QPen(SKELETON_COLOR, 1 * scale))
        self.setZValue(2)
        self.setFlag(QGraphicsLineItem.ItemIsSelectable, True)

    def paint(self, painter, option, widget=None):
        # Skip Qt's default dashed selection-bounding-box decoration --
        # the pen-color highlight (set via on_segment_selection_changed)
        # already shows selection clearly, and the box adds visual noise
        # without adding information.
        option.state = option.state & ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)

    def shape(self):
        # Hit region is a FIXED width around the centerline -- NOT the
        # segment's own rendered stroke width at the glyph's current
        # weight, which is what this used to be (max(half_width * 2,
        # a small fixed floor for thin strokes)). Confirmed as a real,
        # reported problem at anything beyond a light weight: since the
        # rendered width scales linearly with weight with no ceiling,
        # the clickable region around a thick stroke could balloon far
        # past its own visible ink, making a genuinely different,
        # nearby segment increasingly hard or impossible to select on
        # its own -- exactly the mechanism making both ordinary
        # selection AND the disambiguation popup (see
        # _find_ambiguous_candidates) fire or misfire more often as
        # weight increased, for a reason that had nothing to do with
        # how close together the strokes actually were. Shares
        # click_line_tolerance with ambiguity-detection rather than a
        # separately-tuned width, for the same reason those two need to
        # agree at all -- one shared, weight-independent notion of
        # "close enough to this line" for every feature that asks the
        # question, not several quietly-different ones. The width
        # passed to the stroker is double the tolerance since it's
        # applied symmetrically on both sides of the centerline.
        half_click = self.canvas.click_line_tolerance if self.canvas is not None else 5.0
        stroker = QPainterPathStroker()
        stroker.setWidth(half_click * 2)
        # Matches the glyph's OWN current cap style, not a hardcoded
        # round cap regardless of it -- confirmed as a real, reported
        # bug otherwise: a round cap's stroker bulges the hit region
        # in a semicircle past each endpoint, which a flat-capped
        # segment's actual rendered ink never does, so switching to
        # Flat still left the (larger, round-shaped) hit region behind
        # even though the drawn stroke itself changed correctly.
        stroker.setCapStyle(Qt.FlatCap if (self.canvas is not None and self.canvas.cap_style == "flat")
                             else Qt.RoundCap)
        path = QPainterPath()
        path.moveTo(self.line().p1())
        path.lineTo(self.line().p2())
        return stroker.createStroke(path)

    def itemChange(self, change, value):
        if change == QGraphicsLineItem.ItemSelectedHasChanged and self.canvas is not None:
            self.canvas.on_segment_selection_changed(self, bool(value))
        return super().itemChange(change, value)

    def set_endpoints(self, a: tuple[float, float], b: tuple[float, float]):
        self.setLine(a[0], a[1], b[0], b[1])


class ClickCandidate:
    """One plausible meaning of an ambiguous click, ALWAYS a specific
    segment -- picking a candidate always selects that one segment
    (see GlyphCanvas._select_plain_click), so a point-hit and a line-
    hit on the exact same segment would be the identical outcome
    either way. Deliberately NOT split into separate "endpoint" vs
    "line" candidates for that reason -- confirmed as a real, reported
    problem otherwise: a lone, standalone segment's own endpoint sits
    ON that same segment's own line by definition, so treating them as
    two competing candidates made even an unambiguous click near a
    single segment's endpoint show a 2-item popup asking the user to
    choose between "this segment's point" and "this same segment's
    line" -- a distinction with no actual difference in outcome, and
    ambiguity that was never real. `dist` is scene-space distance from
    the click to this candidate (however it qualified -- see
    GlyphCanvas._find_ambiguous_candidates), used only for stable list
    ordering (closest first)."""

    def __init__(self, segment: "SegmentItem", dist: float):
        self.segment = segment
        self.dist = dist


class DisambiguationPopup(QWidget):
    """Frameless, cursor-positioned candidate list for a genuinely
    ambiguous click -- shown only when nothing is currently selected
    and _find_ambiguous_candidates finds two or more plausible things
    at the click position (see GlyphCanvas.mousePressEvent). Hovering
    a row previews exactly what picking it would select (see
    GlyphCanvas.set_preview_tint); picking commits that selection the
    same way a real plain click on that item would -- ready for the
    established two-click flow (click the segment, then click its now-
    active endpoint) to take over from there, not an attempt to also
    arm a drag in the same gesture. Dismissing without picking --
    Escape, or a press anywhere outside this popup's own geometry (see
    eventFilter) -- leaves the selection completely untouched.

    Plain "Line n", numbered by distance (closest first), with
    "(polyline)" or "(group)" appended only when the candidate belongs
    to one -- deliberately without a member/segment count: the hover
    tint already shows exactly which pieces are involved, so a count
    would only repeat information already visible for the cost of a
    noisier list. See ClickCandidate's own docstring for why there's
    no separate "Endpoint" wording at all: picking is always "select
    this segment" regardless of whether the click was nearer its point
    or its line, so showing both as if they were different choices was
    never real ambiguity, only a needless second row (and, for a lone
    standalone segment, a popup firing on an ordinary, perfectly
    unambiguous click).

    Rows are painted directly (paintEvent below), not built from
    QListWidget/QListWidgetItem -- confirmed as a real, reported
    problem otherwise, even after tightening its QSS padding to
    almost nothing: a platform's native Qt style can still apply its
    OWN delegate-driven minimum row height and hover-highlight extent
    underneath the stylesheet, invisible from code and inconsistent
    across platforms, which is exactly the kind of thing that showed
    up as "still has large gaps / large highlight boxes" on a real
    desktop despite measuring tight in isolation. Painting the rows
    directly means row height is a single, explicit constant this
    class controls completely, with no style or delegate layer able to
    override it.

    Width is measured with QFontMetrics bound to THIS widget as its
    paint device (see __init__), not a bare, device-less QFontMetrics
    -- confirmed as the actual cause of a real, reported width/text
    mismatch: font metrics computed off-widget can come out slightly
    different from the SAME font's metrics at actual render time
    (device DPI/hinting differences), which is a small enough gap to
    be invisible in some cases and visibly wrong (too narrow, clipping
    the widest label, or too wide, an obvious empty gutter) in others.

    WA_TranslucentBackground alongside setWindowOpacity, not
    setWindowOpacity alone -- confirmed as the actual second half of
    an earlier "still doesn't allow to clearly see a segment behind
    the popup" problem: without that attribute, a frameless,
    undecorated widget's default (opaque) background can be what a
    platform's compositor actually blends against, making
    setWindowOpacity's effect much weaker than intended or a no-op
    outright, depending on the platform. With it, this widget's own
    background truly is translucent, and the semi-transparent fill
    painted in paintEvent below is what actually shows through to the
    glyph underneath -- background, hover-highlight, and text are all
    independently semi-transparent, not just the background alone, so
    the glyph stays visible through every part of the popup, not only
    the gaps between rows.

    An explicit, application-wide event filter drives outside-click
    dismissal, rather than relying solely on Qt.Popup's own built-in
    mouse-grab behavior -- confirmed as a real, reported bug otherwise:
    combined with WA_TranslucentBackground and this class's own custom
    mouse handling, the popup could end up not releasing its input
    grab on some platforms, silently swallowing clicks anywhere
    roughly aligned with its own column instead of passing them
    through, and never dismissing on a genuine click elsewhere at all.
    Watching every application-level mouse press directly and closing
    on any press outside this popup's own geometry doesn't depend on
    that platform-variable grab behavior working correctly at all."""

    ROW_HEIGHT = 15
    ROW_PAD_X = 6
    BG_ALPHA = 0.28
    HOVER_ALPHA = 0.38
    TEXT_ALPHA = 0.7

    def __init__(self, canvas: "GlyphCanvas", candidates: list[ClickCandidate], global_pos: QPoint):
        super().__init__(canvas, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.canvas = canvas
        self.picked = False
        self.hovered_row = -1
        self.setMouseTracking(True)

        self.candidates = sorted(candidates, key=lambda c: c.dist)
        self.labels = [f"Line {i}{canvas._unit_label_suffix(c.segment.path_index)}"
                       for i, c in enumerate(self.candidates, 1)]

        small_font = self.font()
        small_font.setPointSize(max(7, small_font.pointSize() - 2))
        self.setFont(small_font)
        self._anchor_pos = global_pos
        self._resize_to_content()

    def _resize_to_content(self):
        # A few extra pixels beyond the strictly-measured width, not
        # just the bare measurement -- confirmed as a real, reported
        # gap even with device-bound QFontMetrics (see this class's
        # own docstring): that fix addresses metrics computed with no
        # device context at all, but a small measurement-vs-actual-
        # paint gap can still show up from other sources (hinting,
        # subpixel rounding) depending on the platform, and a few
        # pixels of slack costs nothing visually while guaranteeing the
        # widest label is never clipped.
        #
        # Called again from showEvent, not just here -- a widget's
        # font metrics right at construction, before it has ever
        # actually been shown on a real screen, aren't guaranteed to
        # exactly match its metrics once it has one (device DPI isn't
        # necessarily fully resolved yet) -- re-measuring once shown
        # catches the rare case where those two differ.
        fm = QFontMetrics(self.font(), self)
        text_width = max((fm.horizontalAdvance(label) for label in self.labels), default=0)
        width = text_width + self.ROW_PAD_X * 2 + 6
        height = self.ROW_HEIGHT * len(self.labels) + 2
        if self.size() != QSize(width, height):
            self.setFixedSize(width, height)
        self.move(self._anchor_pos)

    def showEvent(self, event):
        super().showEvent(event)
        self._resize_to_content()
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        # Watches MouseMove application-wide too, not just
        # MouseButtonPress -- closing on a click outside alone left the
        # "moves the mouse away, expects it to just go away" case
        # (this widget's own leaveEvent, below) as the ONLY mechanism
        # for that, and leaveEvent depends on Qt's own per-widget
        # mouse-tracking, which the same platform-specific quirk
        # already confirmed to interfere with this widget's input grab
        # (see this class's own docstring) could plausibly affect too.
        # Checking the GLOBAL cursor position against this popup's own
        # geometry doesn't depend on that per-widget tracking working
        # correctly at all, so it closes reliably even if leaveEvent
        # itself doesn't fire.
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseMove):
            global_pos = event.globalPosition().toPoint()
            if not self.geometry().contains(global_pos):
                self.close()
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # Own background painted explicitly, semi-transparent, rather
        # than relying on any default widget fill -- WA_TranslucentBackground
        # (see this class's own docstring) means there's nothing else
        # behind this to show through otherwise.
        bg = QColor("#ffffff")
        bg.setAlphaF(self.BG_ALPHA)
        border = QColor("#dde1e8")
        border.setAlphaF(self.BG_ALPHA + 0.15)
        painter.setPen(QPen(border, 1))
        painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 6, 6)

        hover_fill = QColor(POPUP_PREVIEW_COLOR)
        hover_fill.setAlphaF(self.HOVER_ALPHA)
        text_color = QColor("#161616")
        text_color.setAlphaF(self.TEXT_ALPHA)
        for i, label in enumerate(self.labels):
            row_rect = QRectF(1, i * self.ROW_HEIGHT + 1, self.width() - 2, self.ROW_HEIGHT)
            if i == self.hovered_row:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(hover_fill))
                painter.drawRect(row_rect)
                painter.setPen(text_color)
            else:
                painter.setPen(text_color)
            text_rect = row_rect.adjusted(self.ROW_PAD_X, 0, -self.ROW_PAD_X, 0)
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, label)

    def _row_at(self, pos) -> int:
        row = int(pos.y() // self.ROW_HEIGHT)
        return row if 0 <= row < len(self.labels) else -1

    def leaveEvent(self, event):
        # Closes the instant the cursor actually leaves this widget's
        # own bounds -- confirmed as a real, reported gap: the
        # application-wide eventFilter above only ever closes the
        # popup on an outside CLICK, so simply moving the mouse away
        # left it sitting there indefinitely, still holding its input
        # grab -- which is also why nothing elsewhere was clickable
        # until a click landed inside the popup's own column: every
        # click anywhere was being consumed by the still-open popup's
        # grab before the eventFilter's own outside-geometry check
        # ever got to close it and let that same click reach anything
        # underneath.
        self.close()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        row = self._row_at(event.position())
        if row != self.hovered_row:
            self.hovered_row = row
            self.canvas.set_preview_tint(self.candidates[row] if row >= 0 else None)
            self.update()

    def mousePressEvent(self, event):
        row = self._row_at(event.position())
        if row >= 0:
            self.picked = True
            self.canvas._select_plain_click(self.candidates[row].segment)
            self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event):
        # Covers every dismiss path that isn't a pick -- Escape, a
        # click outside (see eventFilter), or losing focus some other
        # way -- so the preview tint never lingers after the popup is
        # gone.
        QApplication.instance().removeEventFilter(self)
        if not self.picked:
            self.canvas.set_preview_tint(None)
        super().hideEvent(event)


class GridItem(QGraphicsItem):
    """The grid, drawn as a proper scene item rather than via
    drawBackground(). drawBackground() paints at the viewport level,
    always BEFORE every scene item regardless of z-value -- so with the
    margin's opaque fill sitting in the scene at z=0, the grid was being
    completely covered everywhere the margin covers, i.e. exactly the
    box + margin area, the one place the grid is actually needed. As a
    real item, it can be given a z-value between the margin and
    everything else so it shows through correctly."""

    def __init__(self, canvas: "GlyphCanvas"):
        super().__init__()
        self.canvas = canvas
        self.setZValue(0.5)

    def boundingRect(self):
        if self.canvas.box_rect is not None:
            x0, y0, x1, y1 = self.canvas.box_rect
            return QRectF(x0, y0, x1 - x0, y1 - y0)
        # NOT self.canvas.sceneRect() -- confirmed as a real,
        # reproducible infinite recursion otherwise, severe enough to
        # bring down the whole application: QGraphicsView.sceneRect(),
        # when no explicit rect was ever set via setSceneRect() (which
        # only ever happens elsewhere here when there IS a box -- see
        # load_glyph's own "if box:" gating), falls back to Qt's own
        # dynamic "union of every item's own boundingRect()" -- which
        # includes asking THIS item for its boundingRect() again,
        # calling straight back into this same line. Reproduced
        # directly: a brand new canvas that's never shown a real glyph
        # at all (so no prior load_glyph call ever set an explicit
        # scene rect to fall back on) going blank -- exactly what
        # discarding an uncommitted, from-scratch capture session does
        # -- hits this immediately. An empty rect here is both safe and
        # correct regardless: there's nothing meaningful for the grid
        # to be bounded BY when there's no box to draw it within (see
        # paint()'s own "limited to the box itself" comment), and an
        # empty boundingRect means Qt won't even call paint() for this
        # item at all.
        return QRectF()

    def paint(self, painter, option, widget=None):
        if not self.canvas.grid_spacing:
            return
        g = self.canvas.grid_spacing
        # limited to the box itself, not the full margin -- the margin
        # exists to give overshoot ink room to breathe, not as an area
        # a user is meant to be lining points up against
        rect = self.boundingRect()
        ox, oy = self.canvas.grid_origin
        painter.setPen(QPen(QColor(GRID_COLOR), 0))
        left = ox + math.floor((rect.left() - ox) / g) * g
        top = oy + math.floor((rect.top() - oy) / g) * g
        x = left
        while x < rect.right():
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += g
        y = top
        while y < rect.bottom():
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += g


GUIDE_ORDER = ["cap", "x-height", "baseline", "descender"]
MIN_GUIDE_GAP = 2.0   # smallest allowed distance between adjacent horizontal guides


class GuideLine(QGraphicsLineItem):
    """A draggable reference line. Horizontal guides (cap/x-height/
    baseline/descender) drag vertically only; the "advance" guide is
    vertical and drags horizontally only -- same shared behavior
    (reports its new position back to the canvas, snaps, gets labeled),
    just locked to the other axis. Its name label is a separate
    GuideLabelStrip/AdvanceLabelStrip in main_window.py handle the name
    label now -- not a fixed child of this line --
    a name fixed at a specific spot scrolls off-screen the moment you
    pan/zoom away from it, even while the line itself is still crossing
    the visible canvas.

    The four horizontal guides are also constrained to stay in their
    proper relative order (cap above x-height above baseline above
    descender), each clamped against whichever neighbor is closer, with
    a small minimum gap rather than allowing them to touch exactly --
    without this, dragging cap past baseline (for instance) silently
    produces a negative half_width and a malformed box, not caught
    until export. Both axes are also clamped to the source canvas's own
    pixel bounds, same as point-dragging already is -- otherwise a
    guide dragged far enough off-canvas has no line and no pill left
    anywhere in view to grab it back with."""

    def __init__(self, canvas: "GlyphCanvas", name: str, position: float,
                 span_lo: float, span_hi: float,
                 dashed: bool = True, color: QColor = GUIDE_COLOR, vertical: bool = False):
        if vertical:
            super().__init__(position, span_lo, position, span_hi)
        else:
            super().__init__(span_lo, position, span_hi, position)
        self.canvas = canvas
        self.name = name
        self.vertical = vertical
        # Scales with cap height (see GlyphCanvas._visual_scale) same
        # as the skeleton and endpoint dots -- confirmed as the same
        # class of problem: a fixed pixel width looked right only at
        # the one canvas size it was tuned against, and was reported
        # as looking far too heavy on a small canvas, nearly invisible
        # on a large one.
        scale = canvas._visual_scale if canvas is not None else 1.0
        pen = QPen(color, 1 * scale)
        if dashed:
            pen.setStyle(Qt.DashLine)
        self.setPen(pen)
        self.setFlag(QGraphicsLineItem.ItemSendsGeometryChanges, True)
        self.setZValue(5)
        # No longer ItemIsMovable -- direct click-drag on the thin line
        # itself is what made guides hard to grab precisely and easy to
        # grab by accident while working on nearby segments. Guides now
        # move only via the label strips (GuideLabelStrip/AdvanceLabelStrip
        # in main_window.py), which call setPos() on this item directly --
        # that still fires itemChange below and updates the project's
        # metrics normally, just without the line itself being an
        # interactive click target.

    def current_scene_position(self) -> float:
        """The one coordinate that actually varies: x for a vertical
        guide (advance), y for a horizontal one (everything else)."""
        if self.vertical:
            return self.pos().x() + self.line().x1()
        return self.pos().y() + self.line().y1()

    def _clamp(self, proposed: float) -> float:
        if self.canvas is None:
            return proposed
        # ordering constraint -- horizontal guides only
        if not self.vertical and self.name in GUIDE_ORDER:
            idx = GUIDE_ORDER.index(self.name)
            if idx > 0:
                prev_guide = self.canvas._guides.get(GUIDE_ORDER[idx - 1])
                if prev_guide is not None:
                    proposed = max(proposed, prev_guide.current_scene_position() + MIN_GUIDE_GAP)
            if idx < len(GUIDE_ORDER) - 1:
                next_guide = self.canvas._guides.get(GUIDE_ORDER[idx + 1])
                if next_guide is not None:
                    proposed = min(proposed, next_guide.current_scene_position() - MIN_GUIDE_GAP)
        # usable-area bound -- same source-canvas bounds point-dragging
        # already respects, so a guide can't be dragged somewhere with
        # no way back
        if self.canvas.source_bounds is not None:
            w, h = self.canvas.source_bounds
            limit = w if self.vertical else h
            proposed = min(max(proposed, 0.0), limit)
        # NOT also bounded to the current scene rect -- that WAS needed
        # (see the earlier fix for the exact reasoning) back when the
        # scene rect stayed fixed at whatever it was on load. Now that
        # it's elastic (see GlyphCanvas.resync_scene_to_box, called on
        # every guide-drag release), bounding to the CURRENT scene rect
        # here would be self-defeating: shrink the box once, and the
        # clamp would then treat that shrunk scene as the new ceiling
        # forever, making it impossible to ever grow the guide back out
        # again -- confirmed as a real bug this way (a guide that
        # should reach the true canvas edge topped out at whatever the
        # scene had last shrunk to instead). source_bounds is the only
        # bound that should never move.
        return proposed

    def itemChange(self, change, value):
        if change == QGraphicsLineItem.ItemPositionChange:
            new_pos: QPointF = value
            line = self.line()
            if self.vertical:
                proposed = line.x1() + new_pos.x()
                new_value = self._clamp(proposed)
                value = QPointF(new_value - line.x1(), 0)
            else:
                proposed = line.y1() + new_pos.y()
                new_value = self._clamp(proposed)
                value = QPointF(0, new_value - line.y1())
            if self.canvas is not None:
                self.canvas.on_guide_moved(self.name, new_value)
        return super().itemChange(change, value)


class GlyphCanvas(QGraphicsView):
    guideChanged = Signal(str, float)
    pointsChanged = Signal()
    pointCommitted = Signal(int, int, float, float)   # path_index, point_index, x, y
    selectionChanged = Signal()   # segment selection changed; MainWindow re-checks Break/Join enabled state
    cursorMoved = Signal(float, float)   # design-space x, y -- for the status bar coordinate readout
    viewChanged = Signal()   # zoom or pan changed the visible area -- rulers need to re-derive their range
    pathDrawn = Signal(list)   # a freehand path was finished -- list of (x, y) scene points
    pastePlaced = Signal(float, float)   # paste-follows-cursor was placed with a click -- snapped (x, y) scene position
    deleteRequested = Signal()   # Delete/Backspace pressed with a selection active
    segmentContextMenuRequested = Signal(QPoint)   # a plain (no-drag) right-click on the canvas -- global pos
    offsetPreview = Signal(float, float)   # live dx, dy during a group-offset drag -- status bar readout
    offsetFinished = Signal(list, float, float)   # indices, final dx, dy -- MainWindow commits this as an EditStep
    snapToggleRequested = Signal(str)   # "grid" | "points" | "xheight_baseline" -- G/S/H tap-toggle; MainWindow flips the matching checkbox

    DRAW_DOT_RADIUS = 3.0   # scene units; the drawing-preview point markers' radius,
                             # also used as the "close enough to be the same point"
                             # threshold for double-click's tremor-duplicate check below

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        self.setRenderHint(QPainter.Antialiasing)
        self.setBackgroundBrush(QBrush(QColor(style.CANVAS_BG)))
        self.setDragMode(QGraphicsView.NoDrag)
        self.setMouseTracking(True)
        # QGraphicsView defaults to NoFocus, meaning keyPressEvent below
        # (Enter/Escape while drawing, Delete for the selected segments)
        # would never fire at all without this.
        self.setFocusPolicy(Qt.StrongFocus)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        # Confirmed as a real, reported problem otherwise: a wheel-zoom
        # tick's own scale() call correctly centers under the cursor on
        # its own, but every zoom also emits viewChanged, which
        # triggers CenterPanel.reflow() -- and reflow() resizes the
        # canvas WIDGET itself to match the new zoom level (deliberate;
        # see its own docstring on why the widget grows proportionally
        # with zoom rather than jumping to full size). A widget resize
        # uses Qt's own default ResizeAnchor (NoAnchor), which does NOT
        # preserve the scene point that was just centered under the
        # cursor -- it keeps the viewport's own top-left scene
        # coordinate fixed instead, which immediately undoes the
        # correct centering scale() just established, one tick later.
        # Matching ResizeAnchor to the same AnchorUnderMouse setting
        # closes that gap: the resize this same zoom tick triggers now
        # preserves the cursor's own scene point too, not just the
        # scale() call that precedes it.
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        # Qt draws a default ~1px QFrame border on QAbstractScrollArea-
        # derived widgets (QGraphicsView included) even with no QSS
        # styling applied -- on top of the QSS border already removed,
        # this was still insetting the viewport by a couple px relative
        # to the ruler widgets.
        self.setFrameShape(QGraphicsView.NoFrame)
        # Scrollbars reserve real viewport space once zoomed content
        # exceeds the visible area (Qt shrinks the viewport to make
        # room for them), and that reserved space isn't visible to the
        # ruler widgets at all -- their own size never changes, so once
        # scrollbars appeared the canvas's actual rendering area and
        # the rulers' coordinate math silently started disagreeing,
        # growing right along with how much extra room the scrollbars
        # took up. Right-drag panning already covers navigation once
        # zoomed in, so the scrollbars aren't needed functionally either.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Hiding the scrollbar WIDGETS above doesn't disable scrolling
        # itself -- QGraphicsView still responds to keyboard scrolling
        # (arrow keys, Page Up/Down, Home/End) through the same
        # underlying scroll-position mechanism, natively, without going
        # through any of the mouse-event overrides below. Rather than
        # trying to catch every possible way the view can change one
        # method at a time, connecting directly to the scrollbars' own
        # valueChanged is the single robust source of truth: it fires
        # for ANY change to scroll position, whatever triggered it.
        self.horizontalScrollBar().valueChanged.connect(lambda _v: self.viewChanged.emit())
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.viewChanged.emit())
        # guide label repositioning/visibility now lives in main_window's
        # GuideLabelStrip/AdvanceLabelStrip, refreshed from there via
        # _on_view_changed (already connected to this same signal) --
        # nothing to connect here anymore

        # zoom is expressed relative to whatever fitInView() establishes
        # as the baseline (1.0 = fit-to-window). The upper end used to
        # be a fixed 10x regardless of canvas size -- fine for this
        # project's typical ~300x500 canvas, but a much larger one (up
        # to 4096x4096) shrinks far more at fit-to-window, so the SAME
        # multiplier reaches nowhere near as much actual on-screen
        # detail. See _compute_max_zoom for the canvas-size-aware
        # replacement.
        self.MIN_ZOOM = 1.0
        self._zoom_level = 1.0
        self._user_zoomed = False

        self.paths: list[list[tuple[float, float]]] = []
        self.half_width: float = 10.0
        self.cap_style: str = "round"

        # snapping
        self.grid_spacing: float | None = None
        self.snap_to_grid: bool = False
        self.snap_to_points: bool = False
        self.snap_to_xheight_baseline: bool = False
        self.snap_point_tolerance: float = 10.0
        # Skeleton-based, weight-independent line-proximity tolerance --
        # shared by ambiguity-detection (see _find_ambiguous_candidates)
        # and, later, snap-to-existing-segments. Deliberately narrower
        # than snap_point_tolerance: a line is a continuous target, so
        # using the same generous radius as a point would make it
        # easier to hit than the point itself, the opposite of the
        # intent ("points are already large enough; skeletons are
        # narrow and need a LITTLE more forgiveness than their own
        # rendered width, not as much as a point gets").
        self.click_line_tolerance: float = 5.0
        # Endpoint dot radius, skeleton line width, and guide line width
        # all derive from this single, per-glyph scale factor -- NOT
        # snap_point_tolerance/click_line_tolerance above, which stay
        # deliberately fixed regardless of canvas size (see their own
        # comments, and docs/snapping.md's "why the snap distance is
        # fixed" section: a glyph's structure doesn't change just
        # because its canvas uses bigger numbers, so click/snap
        # precision shouldn't either). Visual size is a different
        # question -- confirmed as a real, reported problem otherwise:
        # a FIXED pixel dot/line size looks proportionate only at the
        # one canvas size it happened to be tuned against (291x509,
        # this app's original Ultiboard reference set), and looks
        # increasingly oversized on a small canvas or increasingly
        # thin on a large one, the same way a fixed-pixel UI element
        # would. Recomputed once per load_glyph call from that glyph's
        # own cap height, exactly the way stroke weight already scales
        # with cap height rather than sitting at a fixed pixel value.
        # Clamped, not left unbounded either direction: a floor keeps
        # dots/lines genuinely visible on the smallest, DOS-era-scale
        # canvases this app is actually built around (see
        # docs/getting-started.md's "Canvas size" section) rather than
        # shrinking toward invisibility, and a ceiling keeps them from
        # growing enormous at the largest canvas size this app still
        # allows (past the soft-limit confirmation, up to the hard
        # limit). 390px is this app's own original reference cap
        # height (the Ultiboard set), where a ratio of 1.0 exactly
        # reproduces every value that was already correct there.
        self._visual_scale: float = 1.0
        self.angle_snap_enabled: bool = False
        # Paste-follows-cursor state -- a ghost preview attaches to the
        # cursor after Paste is clicked, placed with a single click
        # (mirroring draw_mode's own "mode" pattern -- see
        # start_paste_mode/mousePressEvent/keyPressEvent). Initialized
        # here (not left to only ever be set inside load_glyph/
        # start_paste_mode, the way draw_mode itself is) so every read
        # site is safe even before a glyph has ever been loaded.
        self._paste_mode: bool = False
        self._paste_clip_paths: list | None = None
        self._paste_anchor: tuple[float, float] | None = None
        self._paste_ghost_item: QGraphicsPathItem | None = None
        # Excluded-from-export glyphs are viewable but not editable --
        # set explicitly by MainWindow whenever the currently-displayed
        # glyph changes (see _load_glyph_into_canvas's own comment),
        # never derived here. Blocks mutating mouse/keyboard entry
        # points (see mousePressEvent/keyPressEvent/set_draw_mode/
        # start_paste_mode's own checks) while deliberately leaving
        # panning, zooming, and the crosshair itself untouched -- those
        # are viewing, not editing.
        self.read_only: bool = False
        # E is the one remaining held-key override -- G/S/H now tap-
        # toggle their matching persistent checkbox directly (see
        # keyPressEvent), so there's no separate temporary-override
        # state left for them to track here at all.
        #
        # E is different in kind from what G/S/H used to be -- not an
        # exclusive "force this mode on instead" override, but a
        # modifier ON TOP of segment-snap specifically, whenever that's
        # currently on via its checkbox. Confirmed as a real, reported
        # problem otherwise: point-snap's own unconditional priority
        # (see _snap_to_segments_xy) means an unrelated endpoint sitting
        # near a line you're actually trying to snap TO can "steal" the
        # snap, even though it has nothing to do with that line. Holding
        # E suspends just the point half for as long as it's held,
        # falling through directly to line-snap, without needing a
        # second checkbox/shortcut pair to split "snap to points" and
        # "snap to lines" into two independent settings -- considered
        # and rejected in favor of this, since the two were never really
        # independent choices in practice (a line's own endpoints are
        # already points, so "points off, lines on" would still see
        # most of what a line search would anyway). It stays a HELD key
        # rather than a toggle because it has no checkbox of its own to
        # visually represent an on/off state -- it only ever means
        # anything relative to S's own current state. E for suspending
        # Endpoint-priority, specifically, not L: L was considered first
        # and dropped as too easily read as "line", which segment-snap
        # already IS once point-priority steps aside -- confusing given
        # the two ideas would otherwise sound like the same thing.
        self._temp_suspend_endpoint_snap: bool = False
        # aligned to the box's top-left corner each time a glyph loads
        # (see load_glyph), rather than blindly to scene-coordinate
        # (0, 0) -- which for the Y axis in particular isn't a
        # meaningful reference point (it sits somewhere in the empty
        # margin above the glyph, not on any real boundary)
        self.grid_origin: tuple[float, float] = (0.0, 0.0)
        # The source PNG's own pixel dimensions -- the coordinate space
        # the traced skeleton actually lives in. Beyond these, there was
        # never any real traced data to begin with, so a dragged point
        # going past them is inventing geometry rather than editing
        # what the source described.
        self.source_bounds: tuple[float, float] | None = None
        self.box_rect: tuple[float, float, float, float] | None = None
        self.margin_width: float = 0.0

        # right-drag panning
        self._panning: bool = False
        self._mouse_over_canvas: bool = False
        self._pan_last_pos = None
        # Distinct from _pan_last_pos, which updates on every move tick
        # while panning -- this stays fixed at the ORIGINAL press
        # position for the whole gesture, so mouseReleaseEvent can tell
        # a genuine pan (moved) apart from a plain right-click (didn't),
        # the same movement-threshold pattern GROUP_DRAG_THRESHOLD
        # already uses for left-click group drags.
        self._right_click_press_pos = None

        # freehand drawing
        self.draw_mode: bool = False
        self._drawing_points: list[tuple[float, float]] = []
        self._drawing_preview_items: list = []
        self._rubber_band_line: QGraphicsLineItem | None = None
        self._onion_item: QGraphicsPathItem | None = None

        # group-offset drag -- dragging an already-selected segment
        # moves every path touched by the current selection together as
        # one rigid unit. Click-vs-drag disambiguation state (which
        # segment was pressed, where, and whether it's moved enough to
        # count as a drag) lives here on the canvas, NOT on SegmentItem
        # -- mousePressEvent below does its own hit-testing and handles
        # a SegmentItem hit directly, so item-level mouse handlers would
        # never actually be invoked by a real click.
        self._group_drag_item: SegmentItem | None = None
        # True only when _group_drag_item was armed by a plain click
        # on an item that was ALREADY selected (see mousePressEvent) --
        # the one case where a same-spot release with no movement
        # should collapse the selection down to just this item.
        # SHIFT-click and CTRL-click also arm _group_drag_item (so a
        # continued drag can move the result), but must NOT trigger
        # that collapse on a plain release -- confirmed as a real,
        # reported bug otherwise: releasing right after a CTRL-click
        # or SHIFT-click immediately collapsed the selection back down
        # to the one just-clicked item, undoing the add/toggle that
        # had just happened (visible as Join's button flickering
        # enabled then disabled again).
        self._group_drag_defer_collapse: bool = False
        # Explicit, self-contained point-drag tracking -- deliberately
        # NOT relying on Qt's native ItemIsMovable+super().mousePressEvent
        # delegation for this. That delegation asks QGraphicsView's own
        # internal hit-testing to independently rediscover which item
        # the event is for, using Qt's own Z-order/shape()-based logic
        # -- NOT the distance-based resolution _segment_or_point_at
        # already did to correctly determine the point was the
        # intended target. There is no guarantee the two agree,
        # especially with a segment's own wide hit region and an
        # active point often sitting almost exactly on top of each
        # other. Confirmed as the likely real cause of a
        # persistent, reported bug that survived several rounds of
        # fixing _segment_or_point_at itself: the resolution logic was
        # correct, but delegating the actual drag to Qt's own,
        # separate rediscovery could still silently land on the
        # segment underneath instead. Driving the whole gesture
        # explicitly here removes that uncertainty entirely -- once
        # _segment_or_point_at has decided which point is meant, that
        # exact item is what moves, with no second, independent
        # hit-test in between to disagree.
        self._point_drag_item: DraggablePoint | None = None
        self._point_drag_start: QPointF | None = None
        self._group_drag_press_pos: QPointF | None = None
        self._group_drag_moved = False
        self._offset_drag_indices: list[int] | None = None
        self._offset_drag_snapshot: list | None = None
        self._offset_drag_delta: tuple[float, float] = (0.0, 0.0)
        self._offset_drag_anchor: tuple[int, int, float, float] | None = None   # (path_idx, point_idx, orig_x, orig_y)

        # rubber-band select -- catches segments/points, not a native
        # QGraphicsView drag mode, since that would fight the extensive
        # custom press/move/release handling already in place here
        self._rubber_band_start: QPointF | None = None
        self._rubber_band_additive = False
        self._rubber_band_shift = False
        self._rubber_band_moved = False
        self._rubber_band_rect_item: QGraphicsRectItem | None = None

        self._ink_item: QGraphicsPathItem | None = None
        self.skeleton_only: bool = False
        self._points: list[DraggablePoint] = []
        self._segments: list[SegmentItem] = []
        self._box_item: QGraphicsRectItem | None = None
        self._margin_item: QGraphicsRectItem | None = None
        self._grid_item: GridItem | None = None
        self._guides: dict[str, GuideLine] = {}
        self._active_points: set[DraggablePoint] = set()
        # Two accumulation layers, mirroring how Windows Explorer's
        # click/CTRL-click/SHIFT-click interact -- see the extended
        # design discussion this implements. Effective selection is
        # always their union (see the selected_segments property).
        # Elements are (path_index, seg_index) pairs, NOT bare path
        # indices -- a plain click on a SPECIFIC segment of a multi-
        # segment polyline must select only that one segment, not
        # every segment sharing its path_index (confirmed as a real
        # gap: bare path-index tracking made a plain click on one
        # segment of an unjoined polyline indistinguishable from
        # selecting the whole thing).
        #   _individual_selected: from click, CTRL-click, or a drag-box
        #     -- always piece-level; acting on a pair here
        #     (Delete/Break/Join) only ever concerns that one segment,
        #     never its whole polyline/group automatically.
        #   _shift_selected: from SHIFT-click, with each click's whole
        #     unit (polyline or group, see _unit_seg_pairs) flattened
        #     into individual (path, seg) pairs at the moment it's
        #     added, not kept as a unit reference -- this is what lets
        #     CTRL-click later carve a single piece back out
        #     (Explorer's own toggle behavior), which a stored "whole
        #     unit" object couldn't represent.
        self._individual_selected: set[tuple[int, int]] = set()
        self._shift_selected: set[tuple[int, int]] = set()
        # Whether the immediately-preceding selection-building action
        # was itself a SHIFT-click -- the ONE piece of memory SHIFT-
        # click needs to decide whether the NEXT SHIFT-click should
        # clear _individual_selected (starting fresh) or extend
        # _shift_selected on top of what's already there (continuing
        # a SHIFT-click sequence). CTRL-click deliberately never
        # touches or checks this -- that's what keeps "SHIFT then
        # CTRL" and "CTRL then SHIFT" asymmetric, matching Explorer.
        self._last_action_was_shift: bool = False
        # Box-select's own SHIFT behavior needs the ACTUAL physical
        # key-hold state, not just "was the last action shift-based"
        # (which is what _last_action_was_shift tracks, and stays
        # correct for discrete clicks -- confirmed as intentional:
        # SHIFT-click stays additive even across releasing and
        # retaking the key, mirroring Explorer). A box-select is a
        # drag, not a discrete click, and can be repeated several
        # times in one continuous SHIFT-hold -- each new box while
        # still held adds to the accumulated whole-unit selection, but
        # releasing the key and pressing it again to draw another box
        # starts fresh. _shift_hold_generation increments on every
        # fresh (non-autorepeat) SHIFT press in keyPressEvent;
        # _shift_box_select_generation remembers which generation the
        # last SHIFT box-select belonged to, so comparing the two at
        # the next one tells whether the key was ever released in
        # between.
        self._shift_hold_generation: int = 0
        self._shift_box_select_generation: int | None = None
        # Whether the current selection's origin was a bare plain
        # click (necessarily a single item in _individual_selected,
        # nothing in _shift_selected) -- the ONLY case allowing
        # endpoint-dragging. A single item CTRL-clicked into an empty
        # selection looks IDENTICAL in the two sets alone, but must
        # never allow endpoint-dragging (disrupts CTRL-click's own
        # purpose) -- this flag is what tells the two apart.
        self._last_action_was_plain_click: bool = False
        # Persistent groups (see GlyphRecipe.groups), set alongside
        # paths on every load_glyph call. A "unit" is either a group
        # (all its member paths) or an ordinary path on its own --
        # _unit_path_indices/_unit_seg_pairs are the single place that
        # resolves which one applies, used everywhere selection,
        # tinting, Delete, Break, and SHIFT-click need to treat a
        # polyline and a group the same way.
        self._groups: list[list[int]] = []

        # crosshair overlay -- recreated fresh in load_glyph() each time
        # rather than kept alive across scene_.clear() calls (see below)
        self._crosshair_h: QGraphicsLineItem | None = None
        self._crosshair_v: QGraphicsLineItem | None = None
        self._add_crosshair()

    def _add_crosshair(self):
        self._crosshair_h = QGraphicsLineItem()
        self._crosshair_v = QGraphicsLineItem()
        for c in (self._crosshair_h, self._crosshair_v):
            c.setPen(QPen(QColor(120, 120, 130, 150), 0, Qt.DashLine))
            c.setZValue(20)
            c.setVisible(False)
            self.scene_.addItem(c)

    # -- loading ------------------------------------------------------------

    def load_glyph(self, paths, half_width, cap_style="round",
                    box: tuple[float, float, float, float] | None = None,
                    guides: dict[str, float] | None = None,
                    max_half_width: float | None = None,
                    source_bounds: tuple[float, float] | None = None,
                    preserve_view: bool = False,
                    groups: list[list[int]] | None = None):
        # Reloading the SAME glyph after an edit (break/join/delete/a
        # finished drawn path) rebuilds the whole scene the same way a
        # genuinely different glyph selection does -- but unlike an
        # actual glyph switch, there's no reason a same-glyph edit
        # should silently reset the user's current zoom/pan back to
        # fit-to-window. preserve_view captures the current view before
        # the rebuild and restores it after, instead of re-fitting.
        saved_transform = saved_scroll = saved_zoom_level = saved_user_zoomed = None
        if preserve_view:
            saved_transform = self.transform()
            saved_scroll = (self.horizontalScrollBar().value(), self.verticalScrollBar().value())
            saved_zoom_level = self._zoom_level
            saved_user_zoomed = self._user_zoomed
        self._groups = groups if groups is not None else []

        # scene_.clear() -- Qt's own bulk teardown -- rather than a
        # hand-rolled loop of individual removeItem() calls that tried
        # to skip over the crosshair items to keep them alive. clear()
        # properly DELETES every item (not just detaches it), which is
        # more robust around edge cases like an item mid-mouse-grab or
        # with pending queued events than doing it one Python object at
        # a time; the crosshair is just recreated fresh afterward, since
        # that's cheap (two plain QGraphicsLineItems).
        #
        # Deselecting first still matters here too: clear() itself can
        # synchronously fire itemChange(SelectedHasChanged) for whatever
        # was selected, and that cascades into MainWindow reading
        # self.paths/self.selected_segments -- doing that against state
        # that's mid-teardown is the kind of window a hard-to-reproduce
        # native crash comes from.
        self.clear_segment_selection()
        self.scene_.clear()
        self._crosshair_h = None
        self._crosshair_v = None
        self._ink_item = None
        self._box_item = None
        self._margin_item = None
        self._onion_item = None
        self._rubber_band_line = None
        # If a glyph switch happens mid-draw (list selection while
        # points are already placed but not yet finished), those
        # preview items were just deleted by scene_.clear() above too --
        # same stale-reference risk as the others, and the in-progress
        # points themselves aren't meaningful for a DIFFERENT glyph anyway.
        self._drawing_points = []
        self._drawing_preview_items = []
        # Same reasoning as _drawing_points above, for an in-progress
        # paste-follows-cursor placement -- the ghost item itself was
        # just deleted by scene_.clear() too, and a clipboard captured
        # from one glyph pasted into a different one (mid-placement,
        # via a glyph-list switch) isn't a scenario this needs to
        # support gracefully; cancelling outright is simpler and safer
        # than trying to carry it over.
        self._paste_mode = False
        self._paste_clip_paths = None
        self._paste_anchor = None
        self._paste_ghost_item = None
        self.source_bounds = source_bounds
        self.paths = [list(p) for p in paths]
        self.half_width = half_width
        self.cap_style = cap_style
        self._points = []
        self._segments = []
        self._guides = {}
        self._active_points = set()
        # Every piece of drag-tracking state reset here too, not just
        # _active_points -- confirmed as a real, reported bug
        # otherwise: this method runs on every Join/Break/Delete (not
        # just an actual glyph switch), destroying every SegmentItem
        # via scene_.clear() above. If a click or drag had armed any
        # of this right before the button was pressed, it would
        # survive the reload as a dangling reference to a now-
        # destroyed item (_group_drag_item) or a stale pre-reload
        # snapshot (_offset_drag_snapshot/_offset_drag_indices) --
        # and since mouseMoveEvent's own checks don't require the
        # mouse button to still be held, that stale state could
        # silently keep acting on the next mouse movement, moving the
        # wrong thing by an offset computed against coordinates that
        # no longer correspond to anything real. Symptoms reported
        # matched this closely: erratic, hard to reproduce, and
        # specifically tied to Join/Break (both of which rebuild the
        # scene) rather than any single click gesture.
        self._group_drag_item = None
        self._group_drag_press_pos = None
        self._group_drag_moved = False
        self._group_drag_defer_collapse = False
        self._point_drag_item = None
        self._point_drag_start = None
        # Latches True the first time the dragged point moves further
        # than snap_point_tolerance from _point_drag_start, and stays
        # True for the rest of this one drag (press-to-release) even
        # if the point is later moved back close to where it started --
        # see DraggablePoint.itemChange for why this needs to be a
        # one-way latch rather than a live, every-tick "is it near
        # start right now" check.
        self._point_drag_escaped_start = False
        self._offset_drag_indices = None
        self._offset_drag_snapshot = None
        # Same one-way escape latch as _point_drag_escaped_start, for
        # a group/whole-segment drag's own anchor point instead of a
        # single dragged point -- see begin_group_offset_drag /
        # update_group_offset_drag.
        self._group_drag_escaped_start = False
        self._offset_drag_delta = (0.0, 0.0)
        self._offset_drag_anchor = None
        # Every point belonging to the group currently being dragged --
        # see begin_group_offset_drag for why this is computed once per
        # drag and permanently excluded from point-snap regardless of
        # the escape latch above.
        self._group_own_points: set[tuple[int, int]] = set()
        self._rubber_band_start = None
        self._rubber_band_moved = False
        self._rubber_band_additive = False
        self._rubber_band_shift = False
        if self._rubber_band_rect_item is not None:
            # Already destroyed by scene_.clear() above -- just drop
            # the now-dangling Python reference, don't try to remove
            # it from the scene again.
            self._rubber_band_rect_item = None

        # See _visual_scale's own comment (in __init__) for the full
        # reasoning. Computed here, before ANY visual item below this
        # point is constructed (the margin/box rect immediately below,
        # then guide lines, then every point and segment) -- cap
        # height is a property of the calibration/project rather than
        # of any individual glyph, but load_glyph is the one place
        # both that value (via guides) and everything that depends on
        # it come together.
        REFERENCE_CAP_HEIGHT = 390.0
        if guides and "cap" in guides and "baseline" in guides:
            cap_height_px = guides["baseline"] - guides["cap"]
        else:
            cap_height_px = REFERENCE_CAP_HEIGHT   # no metrics available -- behave exactly as before
        self._visual_scale = max(0.5, min(4.0, cap_height_px / REFERENCE_CAP_HEIGHT))

        # The margin is sized to the LARGEST half-width reachable on the
        # weight slider (not the current weight) -- round-cap overshoot
        # scales with weight, and a fixed padding sized for a moderate
        # weight clips a glyph's ink against the canvas edge the moment
        # a heavier weight is chosen, worse for glyphs whose skeleton
        # already sits close to the box boundary. Sizing it once from
        # the slider's max, rather than from whatever weight happens to
        # be selected right now, keeps the canvas at a CONSISTENT size
        # across every glyph and every weight, instead of visibly
        # jumping/rescaling each time either one changes.
        margin = max_half_width if max_half_width is not None else max(half_width, 40.0)

        if box:
            x0, y0, x1, y1 = box
            self._margin_item = QGraphicsRectItem(x0 - margin, y0 - margin,
                                                    (x1 - x0) + 2 * margin, (y1 - y0) + 2 * margin)
            self._margin_item.setBrush(QBrush(QColor(style.MARGIN_FILL)))
            self._margin_item.setPen(QPen(Qt.NoPen))
            self._margin_item.setZValue(0)
            self.scene_.addItem(self._margin_item)

            self._box_item = QGraphicsRectItem(x0, y0, x1 - x0, y1 - y0)
            pen = QPen(BOX_COLOR, 1 * self._visual_scale, Qt.DashLine)
            self._box_item.setPen(pen)
            self._box_item.setZValue(1)
            self.scene_.addItem(self._box_item)
            # guide labels sit past the margin zone, not just past the
            # box edge, so they don't overlap the shaded overshoot area
            x_lo, x_hi = x0, x1 + margin

            # grid aligns to the box's own top-left corner, not blindly
            # to scene (0, 0) -- x0 is always 0 anyway by construction,
            # but y0 (cap top) is the meaningful reference on the Y axis
            self.grid_origin = (x0, y0)
            self.box_rect = box
            self.margin_width = margin
        else:
            x_lo, x_hi = 0, 300
            self.grid_origin = (0.0, 0.0)
            self.box_rect = None
            self.margin_width = 0.0

        self._grid_item = GridItem(self)
        self.scene_.addItem(self._grid_item)

        if guides:
            # advance's line extends toward the BOTTOM margin only,
            # matching x_hi above -- the horizontal guides extend into
            # the margin on the side facing their strip (right), so
            # advance (whose strip is at the bottom) should do the same
            # toward the bottom, rather than stopping flush at the
            # box's own edge and leaving a margin-width gap before its
            # label strip even begins.
            y_lo, y_hi = (y0, y1 + margin) if box else (0, 300)
            for name, pos in guides.items():
                if name == "advance":
                    # vertical guide: the box's own right edge, draggable
                    # horizontally -- everything else here is horizontal
                    line = GuideLine(self, name, pos, y_lo, y_hi,
                                      dashed=True, color=GUIDE_COLOR, vertical=True)
                else:
                    is_baseline = name == "baseline"
                    line = GuideLine(self, name, pos, x_lo, x_hi,
                                      dashed=not is_baseline,
                                      color=BASELINE_COLOR if is_baseline else GUIDE_COLOR)
                self.scene_.addItem(line)
                self._guides[name] = line

        self._rebuild_ink()

        for pi, path in enumerate(self.paths):
            pts = [DraggablePoint(self, pi, qi, x, y) for qi, (x, y) in enumerate(path)]
            for pt in pts:
                self.scene_.addItem(pt)
                self._points.append(pt)
            for qi in range(len(path) - 1):
                seg = SegmentItem(self, pi, qi, pts[qi], pts[qi + 1])
                self.scene_.addItem(seg)
                self._segments.append(seg)

        if box:
            x0, y0, x1, y1 = box
            # Scene rect is set ONCE here to a FIXED, generous extent
            # covering the true canvas maximum (not just the current
            # box) -- not resized again as guides move. This is the CAD/
            # mapping-software pattern: a fixed "world", with all
            # zooming and panning being purely VIEW-side operations
            # (fitInView on a specific sub-rect, ensureVisible) rather
            # than repeatedly resizing the underlying scene itself.
            # Confirmed as the actual fix for a real, reproducible bug:
            # calling setSceneRect() mid-interaction triggers Qt's own
            # internal scrollbar-range recalculation in ways that turned
            # out genuinely unpredictable (the range could even SHRINK
            # when the scene grew) -- fighting that after the fact
            # (restoring scrollbar values, centerOn, direct transform
            # translation) never fully worked, while simply never
            # calling setSceneRect() again during interaction sidesteps
            # the problem entirely.
            cw, ch = source_bounds if source_bounds is not None else (x1, y1)
            scene_left = min(x0, 0.0) - margin - 10
            scene_top = min(y0, 0.0) - margin - 10
            scene_right = max(x1, cw) + margin + 10
            scene_bottom = max(y1, ch) + margin + 10
            self.setSceneRect(scene_left, scene_top,
                               scene_right - scene_left, scene_bottom - scene_top)
            if preserve_view and saved_transform is not None:
                self.setTransform(saved_transform)
                self.horizontalScrollBar().setValue(saved_scroll[0])
                self.verticalScrollBar().setValue(saved_scroll[1])
                self._zoom_level = saved_zoom_level
                self._user_zoomed = saved_user_zoomed
            else:
                # Fit to the BOX's own sub-rect specifically, not
                # self.sceneRect() (which is now the much larger, fixed
                # extent) -- fitInView takes any rect, not just the
                # scene's own stored bounds, which is what makes "box
                # fills the viewport" achievable without the scene
                # itself ever needing to match the box.
                self.fitInView(QRectF(x0 - margin, y0 - margin,
                                       (x1 - x0) + 2 * margin, (y1 - y0) + 2 * margin),
                               Qt.KeepAspectRatio)
                # a new glyph always starts at a fresh fit, not whatever
                # zoom level was left over from the previous one
                self._zoom_level = 1.0
                self._user_zoomed = False

        self._add_crosshair()

    # -- live re-render on edit -----------------------------------------

    def _rebuild_ink(self):
        if self._ink_item is not None:
            self.scene_.removeItem(self._ink_item)
            self._ink_item = None
        if self.skeleton_only:
            # Skips the thickening entirely (not just hides the result)
            # -- that's real, non-trivial work (shapely boolean union
            # per segment, scaling with point count), and the point of
            # this mode is a faster redraw when a glyph has a lot of
            # points, not just a different look. The skeleton itself is
            # already visible via the existing SegmentItem lines, so
            # there's nothing extra to build here at all.
            return
        qpath = _paths_to_qpath(self.paths, self.half_width, self.cap_style)
        if qpath is None:
            return
        item = QGraphicsPathItem(qpath)
        item.setBrush(QBrush(INK_COLOR))
        item.setPen(QPen(Qt.NoPen))
        item.setZValue(1)
        self.scene_.addItem(item)
        self._ink_item = item

    def set_skeleton_only(self, enabled: bool):
        self.skeleton_only = enabled
        self._rebuild_ink()

    def _segments_for_path(self, path_index: int) -> list[SegmentItem]:
        return [s for s in self._segments if s.path_index == path_index]

    def _unit_path_indices(self, path_index: int) -> set[int]:
        """The full set of path indices making up the "unit" a given
        path belongs to: its own group's members if it's grouped, or
        just itself if it's an ordinary path (a polyline's own several
        segments are already one unit via their shared path_index;
        this only adds the group case on top, so a polyline and a
        group are resolved through the same call everywhere this
        matters -- selection, tinting, Delete, Break)."""
        for group in self._groups:
            if path_index in group:
                return set(group)
        return {path_index}

    def _unit_label_suffix(self, path_index: int) -> str:
        """" (group)" / " (polyline)" / "" for the disambiguation
        popup's list labels -- distinguishing an explicit, multi-path
        group (see self._groups) from an ordinary polyline (a single
        path with more than one segment of its own) from a genuinely
        standalone, single-segment path, which gets no suffix at
        all."""
        for group in self._groups:
            if path_index in group:
                return " (group)"
        if 0 <= path_index < len(self.paths) and len(self.paths[path_index]) > 2:
            return " (polyline)"
        return ""

    def _unit_seg_pairs(self, segment: SegmentItem) -> set[tuple[int, int]]:
        """Every (path_index, seg_index) pair belonging to the unit a
        given segment is part of. For an ordinary polyline (not
        grouped), that's every segment sharing its OWN path_index --
        the whole polyline is the unit, same as tinting/Break have
        always treated it. For a grouped path, it's every member
        path's own segment(s) (each member is normally a single-
        segment path by the time it's grouped, but this doesn't
        assume that)."""
        unit_paths = self._unit_path_indices(segment.path_index)
        return {(s.path_index, s.seg_index) for s in self._segments if s.path_index in unit_paths}

    @property
    def selected_segments(self) -> list[SegmentItem]:
        """The effective selection -- every segment whose (path,
        segment) pair is in EITHER accumulation layer. Computed fresh
        rather than kept as a maintained list, since the two layers
        (not this) are the actual source of truth; Delete/Break/Join
        all read this without needing to know which layer any given
        piece came from."""
        all_pairs = self._individual_selected | self._shift_selected
        return [s for s in self._segments if (s.path_index, s.seg_index) in all_pairs]

    def _select_plain_click(self, segment: SegmentItem):
        """A bare click: always starts completely fresh (see the
        Explorer-mirroring design this implements), selecting just
        the one clicked SEGMENT -- not its whole path, even if that
        path is a multi-segment polyline. The only action that sets
        _last_action_was_plain_click -- the sole case where endpoint-
        dragging is available."""
        self._individual_selected.clear()
        self._shift_selected.clear()
        self._individual_selected.add((segment.path_index, segment.seg_index))
        self._last_action_was_plain_click = True
        self._last_action_was_shift = False
        self._refresh_selection_visuals()

    def _select_ctrl_click(self, segment: SegmentItem):
        """CTRL-click: toggles ONE segment in or out of the effective
        selection, regardless of which layer it's currently in --
        confirmed as genuine Explorer behavior: an item CTRL-clicked
        back out is removed from wherever it actually lives, whether
        it got there via a previous CTRL-click or via a SHIFT-click's
        whole-unit expansion. Never touches _shift_selected's OTHER
        members -- that asymmetry (SHIFT then CTRL stays additive;
        CTRL then SHIFT gets wiped) is what _select_shift_click
        implements on its own side. Always piece-level, never
        upgrades to a whole unit on its own."""
        key = (segment.path_index, segment.seg_index)
        if key in self._individual_selected:
            self._individual_selected.discard(key)
        elif key in self._shift_selected:
            self._shift_selected.discard(key)
        else:
            self._individual_selected.add(key)
        self._last_action_was_plain_click = False
        self._last_action_was_shift = False
        self._refresh_selection_visuals()

    def _select_shift_click(self, segment: SegmentItem):
        """SHIFT-click: selects the clicked segment's WHOLE unit
        (every segment of its polyline, or every member of its group,
        see _unit_seg_pairs), flattened into individual (path, seg)
        pairs at the moment it's added -- not kept as a unit
        reference, which is what lets a later CTRL-click carve a
        single piece back out.

        Clears _individual_selected first, but ONLY if the immediately
        preceding action wasn't itself a SHIFT-click -- this is the
        Explorer asymmetry: SHIFT-click then CTRL-click stays additive
        (CTRL never touches _shift_selected), but CTRL-click then
        SHIFT-click forgets every individually-picked item and starts
        fresh from the SHIFT-click. Repeated SHIFT-clicks keep
        accumulating whole units in _shift_selected on top of each
        other, regardless of whether the modifier key was held
        continuously or pressed fresh each time."""
        if not self._last_action_was_shift:
            self._individual_selected.clear()
        self._shift_selected |= self._unit_seg_pairs(segment)
        self._last_action_was_plain_click = False
        self._last_action_was_shift = True
        self._refresh_selection_visuals()

    def _select_drag_box(self, rect: QRectF, additive: bool):
        """Selects every segment with at least one endpoint inside
        `rect` -- using the raw skeleton endpoints (the actual point
        positions), not the wide, weight-based region used for normal
        single-click hit-testing (see SegmentItem.shape()). At a heavy
        stroke weight that wide region can extend well beyond the box
        a user actually dragged; the skeleton itself doesn't change
        with weight, so it's the only geometry that means the same
        thing regardless of how thick the glyph currently renders.

        Always piece-level, like click/CTRL-click -- a fully-caught
        polyline or group ending up entirely selected is an incidental
        consequence of every one of its pieces individually
        qualifying, never a deliberate whole-unit upgrade; SHIFT-click
        is the only thing that ever does that. No combined SHIFT+drag-
        box -- a fresh (non-additive) box starts over exactly like a
        plain click does, clearing BOTH layers, never just adding to
        _shift_selected the way SHIFT-click does.

        additive (CTRL held) toggles each caught segment the same way
        _select_ctrl_click does, one at a time -- including carving an
        already-SHIFT-selected piece back out, and including a
        drag-box that happens to catch just one segment behaving
        identically to CTRL-clicking that same segment directly."""
        caught = {(s.path_index, s.seg_index) for s in self._segments
                  if rect.contains(s.line().p1()) or rect.contains(s.line().p2())}
        if additive:
            for key in caught:
                if key in self._individual_selected:
                    self._individual_selected.discard(key)
                elif key in self._shift_selected:
                    self._shift_selected.discard(key)
                else:
                    self._individual_selected.add(key)
        else:
            self._individual_selected.clear()
            self._shift_selected.clear()
            self._individual_selected |= caught
        self._last_action_was_plain_click = False
        self._last_action_was_shift = False
        self._refresh_selection_visuals()

    def _select_shift_drag_box(self, rect: QRectF):
        """SHIFT-held box-select: every segment caught by the box is
        expanded to its WHOLE unit (polyline or group, see
        _unit_seg_pairs) and added to _shift_selected -- the same
        whole-unit semantics as SHIFT-click, just applied to
        everything the box touches at once, rather than one click at
        a time.

        Additive across MULTIPLE box-drags for as long as SHIFT stays
        continuously held (see _shift_hold_generation) -- draw one
        box, release the mouse but keep SHIFT down, draw another box
        elsewhere, and the second adds to the first rather than
        replacing it. Releasing SHIFT and pressing it again before the
        next box starts a fresh selection instead, clearing both
        layers first -- mirroring _select_shift_click's own "forgets
        individually-picked items" rule, extended here to also forget
        the previous SHIFT-accumulated whole units once the key has
        genuinely been let go, not just because a different gesture
        happened to run in between."""
        fresh = self._shift_box_select_generation != self._shift_hold_generation
        if fresh:
            self._individual_selected.clear()
            self._shift_selected.clear()
        caught_paths = {s.path_index for s in self._segments
                        if rect.contains(s.line().p1()) or rect.contains(s.line().p2())}
        for pi in caught_paths:
            unit_paths = self._unit_path_indices(pi)
            self._shift_selected |= {(s.path_index, s.seg_index) for s in self._segments
                                      if s.path_index in unit_paths}
        self._shift_box_select_generation = self._shift_hold_generation
        self._last_action_was_plain_click = False
        self._last_action_was_shift = True
        self._refresh_selection_visuals()

    def clear_all_selection(self):
        """Used for contexts outside the 5 modeled gestures (e.g.
        switching glyphs, Escape) -- NOT part of the click/CTRL/SHIFT
        model itself, just a full reset."""
        self._individual_selected.clear()
        self._shift_selected.clear()
        self._last_action_was_plain_click = False
        self._last_action_was_shift = False
        self._refresh_selection_visuals()

    def _refresh_selection_visuals(self):
        """The single place that turns the two accumulation layers
        into what's actually drawn: segment pens, point states
        (idle / individually-active / SHIFT-highlighted-and-locked),
        family tinting, and z-order -- called explicitly at the end of
        every one of the 5 selection-building actions above, rather
        than reacting to Qt's own per-item itemChange signal. Doing it
        this way (one full recompute from the two sets, after they've
        both finished changing) avoids the mid-loop "can't yet know
        the final state" problem earlier per-item-triggered versions
        of this had."""
        all_pairs = self._individual_selected | self._shift_selected

        # Qt's own selection flag kept in sync too, for whatever else
        # (QGraphicsItem internals, isSelected() checks elsewhere)
        # might still read it -- not used as this method's own source
        # of truth, which is strictly the two sets above.
        for seg in self._segments:
            selected = (seg.path_index, seg.seg_index) in all_pairs
            if seg.isSelected() != selected:
                seg.setSelected(selected)

        # Tinting: for every INDIVIDUALLY-selected piece, tint the rest
        # of ITS OWN unit (every other segment of the same polyline, or
        # every other member of the same group, see _unit_seg_pairs) --
        # a union across however many different units are touched,
        # since click/CTRL-click never merge multiple touched units
        # into one shared consideration.
        tint_pairs: set[tuple[int, int]] = set()
        for seg in self._segments:
            if (seg.path_index, seg.seg_index) in self._individual_selected:
                tint_pairs |= self._unit_seg_pairs(seg)
        tint_pairs -= all_pairs   # never tint something that's actually selected
        tint_paths = {pi for pi, si in tint_pairs}

        for seg in self._segments:
            key = (seg.path_index, seg.seg_index)
            if key in all_pairs:
                seg.setPen(QPen(SEGMENT_SELECTED_COLOR, 2 * self._visual_scale))
                seg.setZValue(3)
            elif seg.path_index in tint_paths:
                seg.setPen(QPen(SEGMENT_FAMILY_COLOR, 1 * self._visual_scale))
                seg.setZValue(2)
            else:
                seg.setPen(QPen(SKELETON_COLOR, 1 * self._visual_scale))
                seg.setZValue(2)

        # Points: individually-active (draggable) only for a genuine
        # plain-click single selection; SHIFT-highlighted-and-locked
        # for anything in _shift_selected; idle otherwise. Endpoints
        # of an individually-selected but non-plain-click piece (e.g.
        # CTRL-click) get raised z (so they're not visually buried
        # under an overlapping idle point at a busy junction) without
        # being draggable -- reuses the same highlighted-but-locked
        # visual as SHIFT, since neither allows single-point dragging.
        needed_active = set()
        if self._last_action_was_plain_click:
            for seg in self.selected_segments:
                needed_active.add(seg.point_a)
                needed_active.add(seg.point_b)
        needed_highlighted = set()
        for seg in self._segments:
            if (seg.path_index, seg.seg_index) in all_pairs:
                if seg.point_a not in needed_active:
                    needed_highlighted.add(seg.point_a)
                if seg.point_b not in needed_active:
                    needed_highlighted.add(seg.point_b)

        for pt in list(self._active_points):
            if pt not in needed_active:
                self._active_points.discard(pt)
                pt.set_active(False)
        for pt in needed_active:
            self._active_points.add(pt)
            pt.set_active(True)
            pt.setZValue(11)
        for pt in needed_highlighted:
            pt.set_highlighted(True)
            pt.setZValue(11)
        # everything else idle
        all_points_touched = needed_active | needed_highlighted
        for pt in self._points:
            if pt not in all_points_touched:
                pt.set_highlighted(False)
                if pt.zValue() != 10:
                    pt.setZValue(10)

    def set_preview_tint(self, candidate: "ClickCandidate | None"):
        """Hover-preview for the disambiguation popup -- shows exactly
        which segment PICKING this candidate would select, in a
        distinct, deliberately vibrant color (POPUP_PREVIEW_COLOR) of
        its own -- not SEGMENT_SELECTED_COLOR, the color an actual,
        committed plain-click selection uses. Confirmed as the right
        call, not just a style choice: this state isn't a selection at
        all (nothing is touched in _individual_selected/_shift_selected
        -- see below), so borrowing the same color a real selection
        uses would make a merely-hovered candidate look indistinguishable
        from something already committed.

        The candidate's own two endpoints are recolored the SAME way,
        not just its line -- confirmed as a real, wanted improvement:
        the line alone didn't make clear exactly which two points this
        specific candidate would activate, particularly useful at a
        busy multi-way junction where several candidates' lines
        converge but their FAR endpoints are what actually distinguish
        one from another.

        The rest of the candidate's own unit (polyline/group) stays at
        the weaker SEGMENT_FAMILY_COLOR, mirroring _refresh_selection_
        visuals' own plain-click treatment -- an accurate preview of
        what a real pick produces, just recolored to read as "not yet
        committed."

        Touches NOTHING in _individual_selected/_shift_selected at all
        -- dismissing the popup without picking anything leaves the
        real selection state completely untouched. None restores the
        normal, selection-driven visuals (always "everything idle"
        here, since the popup only ever appears when nothing is
        selected), which also reverts every point's color back to
        normal via the same path _refresh_selection_visuals always
        uses."""
        if candidate is None:
            self._refresh_selection_visuals()
            return
        unit_paths = self._unit_path_indices(candidate.segment.path_index)
        for seg in self._segments:
            if seg is candidate.segment:
                seg.setPen(QPen(POPUP_PREVIEW_COLOR, 2 * self._visual_scale))
                seg.setZValue(3)
                seg.point_a.setBrush(QBrush(POPUP_PREVIEW_COLOR))
                seg.point_b.setBrush(QBrush(POPUP_PREVIEW_COLOR))
            elif seg.path_index in unit_paths:
                seg.setPen(QPen(SEGMENT_FAMILY_COLOR, 1 * self._visual_scale))
                seg.setZValue(2)
            else:
                seg.setPen(QPen(SKELETON_COLOR, 1 * self._visual_scale))
                seg.setZValue(2)

    def _update_segments_touching(self, path_index: int, point_index: int):
        """Keep the skeleton overlay in sync when a point moves."""
        path = self.paths[path_index]
        for seg in self._segments_for_path(path_index):
            if seg.seg_index == point_index - 1:
                seg.set_endpoints(path[point_index - 1], path[point_index])
            elif seg.seg_index == point_index:
                seg.set_endpoints(path[point_index], path[point_index + 1])

    def apply_paths_preview(self, indices: list[int], new_paths: list):
        """Directly updates point/segment/ink state for the given path
        indices IN PLACE, without a full scene teardown+rebuild --
        load_glyph() is too slow to call on every slider tick (or every
        auto-repeat click while a +/- button is held): each call was
        outpacing how fast Qt could actually process them, so clicks
        queued up faster than they drained and the whole window
        appeared to lock up until the backlog cleared. This updates
        only what actually changed, so it's cheap enough to run on
        every tick. Selection state is left completely untouched.

        new_paths is a full paths list (same shape as self.paths) --
        only the entries at `indices` are actually read or applied."""
        for pi in indices:
            new_path = new_paths[pi]
            self.paths[pi] = list(new_path)
            for pt in self._points:
                if pt.path_index == pi:
                    x, y = new_path[pt.point_index]
                    # Bypass itemChange's snap/clamp logic (gated on
                    # _active) -- a flatten preview needs the EXACT
                    # computed coordinate, not one pulled onto the grid
                    # or clamped as if this were a manual drag.
                    was_active = pt._active
                    pt._active = False
                    pt.setPos(x, y)
                    pt._active = was_active
            for seg in self._segments_for_path(pi):
                seg.set_endpoints(new_path[seg.seg_index], new_path[seg.seg_index + 1])
        self._rebuild_ink()

    def begin_group_offset_drag(self):
        """Snapshots the ORIGINAL shape of every path touched by the
        current selection, before any offset is applied -- update_
        recomputes from this on every mouse-move tick, the same
        snapshot-based approach Flatten uses and for the same reason:
        it keeps the drag's meaning fixed to "how far from where this
        started", regardless of how many intermediate ticks occur.

        For a drag that originated from a PLAIN click (see
        _last_action_was_plain_click), every selected path expands to
        its full unit (see _unit_path_indices) before the drag starts
        -- this is what makes a group move as one rigid whole when
        you've only plain-clicked ONE of its members, matching how a
        polyline already behaves in the exact same situation. That
        polyline behavior was never a deliberate special case: a
        polyline's segments all share one path_index, and offset_part
        moves an entire path regardless of which one of its own
        segments triggered the drag -- a group's members are genuinely
        separate paths, so without this expansion, only the one
        plain-clicked member would move, silently "pulling it out" of
        the group instead of carrying the whole thing along.
        Confirmed as a real, reported gap between the two.

        Deliberately NOT applied when the drag came from CTRL-click
        (or a CTRL-held drag-box): CTRL-click's whole purpose is
        picking a specific, hand-chosen subset of pieces to move
        together, which must stay exactly that subset -- expanding it
        to whole units here would defeat it the same way allowing
        endpoint-dragging on a CTRL-click selection would (see
        _select_ctrl_click's own reasoning).

        Also finds the group's own point nearest to where the drag
        started, as the "handle" the cursor effectively grabs. Clicking
        exactly ON a point starts a single-point drag instead of a
        group drag, so there'd otherwise be no way to make snap-to-grid
        or snap-to-existing-points apply to a group at all -- this
        point's position is what actually gets snapped each tick (see
        update_group_offset_drag), with the whole group riding along
        rigidly behind it."""
        touched_paths = {s.path_index for s in self.selected_segments}
        if self._last_action_was_plain_click:
            expanded: set[int] = set()
            for pi in touched_paths:
                expanded |= self._unit_path_indices(pi)
            touched_paths = expanded
        indices = sorted(touched_paths)
        self._offset_drag_indices = indices
        self._offset_drag_snapshot = copy.deepcopy(self.paths)
        self._offset_drag_delta = (0.0, 0.0)

        press_x, press_y = self._group_drag_press_pos.x(), self._group_drag_press_pos.y()
        best = None
        for pi in indices:
            for qi, (x, y) in enumerate(self._offset_drag_snapshot[pi]):
                d = math.hypot(x - press_x, y - press_y)
                if best is None or d < best[0]:
                    best = (d, pi, qi, x, y)
        self._offset_drag_anchor = (best[1], best[2], best[3], best[4]) if best else None
        # Every point belonging to ANY path in this group, computed
        # ONCE here (group membership doesn't change mid-drag) --
        # permanently excluded from point-snap for the whole drag, the
        # same way every one of the group's own segments is already
        # permanently excluded from line-snap (see
        # update_group_offset_drag's own comment on that). Without
        # this, a rigidly-moving group's own internal point-to-point
        # distances -- fixed for the entire drag by definition -- could
        # make the anchor keep "discovering" its own group-mates as
        # legitimate, very-close snap targets regardless of where the
        # group actually is on the canvas, producing jitter with no
        # relation to any other, external geometry at all. This is
        # unconditional, not tied to the escape latch below, unlike the
        # anchor's own starting-coincidence exclusion -- the group's
        # internal geometry never stops being internal, so there's
        # never a point in the drag where snapping onto it would be
        # anything but the same self-referential problem.
        self._group_own_points: set[tuple[int, int]] = {
            (pi, qi)
            for pi in indices
            for qi in range(len(self._offset_drag_snapshot[pi]))
        }
        # Same one-way escape latch as a single point's own drag (see
        # DraggablePoint.itemChange's own comment for the full
        # reasoning) -- reset fresh for every new group/whole-segment
        # drag.
        self._group_drag_escaped_start = False

    def update_group_offset_drag(self, cursor_x: float, cursor_y: float):
        """cursor_x/y is the raw, unsnapped scene position under the
        mouse. Rather than snapping the raw cursor position itself,
        this snaps where the group's ANCHOR point (see
        begin_group_offset_drag) would land if it followed the cursor
        exactly -- so the anchor visibly "attaches" to the cursor at
        drag start (a deliberate jump, not a bug: it's what makes it
        possible to see exactly what will land where), and from then on
        snapping is computed relative to that specific point, not an
        arbitrary raw coordinate under the mouse."""
        if self._offset_drag_indices is None or self._offset_drag_snapshot is None:
            return
        if self._offset_drag_anchor is not None:
            api, aqi, ax, ay = self._offset_drag_anchor
            # Same one-way escape latch as a single point's own drag
            # (see DraggablePoint.itemChange's own comment for the
            # full reasoning): while still within snap_point_tolerance
            # of where this drag began, whatever the anchor started
            # coincident with -- points AND lines alike -- is excluded,
            # so the drag isn't magnetically stuck to its own starting
            # spot; once it's moved past that once, every point and
            # line, including whatever it started on, becomes a normal
            # snap target again for the rest of this drag.
            if not self._group_drag_escaped_start:
                dx0 = cursor_x - ax
                dy0 = cursor_y - ay
                if (dx0 * dx0 + dy0 * dy0) ** 0.5 > self.snap_point_tolerance:
                    self._group_drag_escaped_start = True

            if not self._group_drag_escaped_start:
                exclude = self._points_coincident_with(ax, ay)
                exclude.add((api, aqi))
                exclude_lines = self._lines_coincident_with(ax, ay)
            else:
                exclude = {(api, aqi)}
                exclude_lines = set()
            # Permanently union in every one of the group's OWN points
            # (computed once in begin_group_offset_drag) -- unlike the
            # exclusions above, this is NOT gated by the escape latch:
            # the group's internal point-to-point distances are fixed
            # for the whole drag by construction, so there's no phase
            # of the drag where snapping onto one of them would ever be
            # anything but the same self-referential problem this
            # exists to prevent. Mirrors the unconditional per-tick
            # line exclusion immediately below, which already does the
            # equivalent thing for the group's own segments.
            exclude |= self._group_own_points
            # Every segment belonging to ANY path in this drag is
            # permanently excluded from the line-snap half, not just
            # the anchor's own immediate neighbors the way a single
            # point's own drag excludes -- the whole group/segment
            # moves rigidly together here (see begin_group_offset_drag),
            # so every one of ITS OWN member segments' lines would
            # otherwise be chasing this same drag at every tick, the
            # same degenerate "stuck to itself" problem a single point
            # excluding only its own two neighbors is enough to avoid,
            # but a multi-path rigid group is not.
            for pi in (self._offset_drag_indices or []):
                path = self._offset_drag_snapshot[pi] if self._offset_drag_snapshot else []
                for si in range(max(len(path) - 1, 0)):
                    exclude_lines.add((pi, si))
            snapped_x, snapped_y = self.apply_snap(cursor_x, cursor_y,
                                                     exclude=exclude, exclude_lines=exclude_lines)
            dx = snapped_x - ax
            dy = snapped_y - ay
        else:
            dx = dy = 0.0
        dx, dy = self._clamp_group_offset(dx, dy)
        self._offset_drag_delta = (dx, dy)
        new_paths = geometry.offset_part(self._offset_drag_snapshot, self._offset_drag_indices, dx, dy)
        self.apply_paths_preview(self._offset_drag_indices, new_paths)
        self.offsetPreview.emit(dx, dy)

    def _clamp_offset_to_box(self, dx: float, dy: float, points: list[tuple[float, float]]) -> tuple[float, float]:
        """Limits the offset itself, not each point independently --
        clamping points one at a time would let some hit the box edge
        before others and distort the shape, breaking the "moves as one
        rigid unit" guarantee this is meant to provide. Computed from
        points' own collective bounding box so the WHOLE thing stops
        together, still perfectly rigid, right at the box edge -- not
        just whichever single reference point happens to be tracked.

        Shared between _clamp_group_offset (dragging an existing
        selection) and paste-follows-cursor's own ghost placement (see
        _update_crosshair_at/mousePressEvent) -- confirmed as a real,
        reported bug otherwise for the latter: clamping only the
        paste's own anchor point kept THAT one point on-canvas while
        the rest of the pasted content, extending away from it, could
        still be placed well outside the canvas bounds. Same
        underlying requirement either way (the WHOLE selection/paste
        stays entirely on-canvas, not just one reference point within
        it), same fix."""
        if self.box_rect is None or not points:
            return dx, dy
        box_x0, box_y0, box_x1, box_y1 = self.box_rect
        min_x = min(p[0] for p in points)
        max_x = max(p[0] for p in points)
        min_y = min(p[1] for p in points)
        max_y = max(p[1] for p in points)

        def clamp_axis(value, lo, hi):
            # If the content is itself wider/taller than the box, lo >
            # hi here -- there's no dx that fits it inside, so don't
            # fight that; just don't let it move at all along this
            # axis rather than snapping to an arbitrary edge.
            if lo > hi:
                return 0.0
            return min(max(value, lo), hi)

        dx = clamp_axis(dx, box_x0 - min_x, box_x1 - max_x)
        dy = clamp_axis(dy, box_y0 - min_y, box_y1 - max_y)
        return dx, dy

    def _clamp_group_offset(self, dx: float, dy: float) -> tuple[float, float]:
        """Applied AFTER snapping (see update_group_offset_drag), not
        instead of it -- the snapped target is what gets clamped, so a
        single, already-constrained motion is what's ever visible, not
        an unconstrained jump immediately followed by a separate,
        visible correction. See _clamp_offset_to_box's own docstring
        for the actual clamping logic, shared with paste-follows-
        cursor's own identical requirement."""
        if self._offset_drag_indices is None or self._offset_drag_snapshot is None:
            return dx, dy
        points = [pt for i in self._offset_drag_indices for pt in self._offset_drag_snapshot[i]]
        return self._clamp_offset_to_box(dx, dy, points)

    def end_group_offset_drag(self):
        if self._offset_drag_indices and self._offset_drag_delta != (0.0, 0.0):
            dx, dy = self._offset_drag_delta
            self.offsetFinished.emit(self._offset_drag_indices, dx, dy)
        self._offset_drag_indices = None
        self._offset_drag_snapshot = None
        self._offset_drag_delta = (0.0, 0.0)
        self._offset_drag_anchor = None
        self._group_own_points = set()

    def finish_any_pending_drag(self) -> str | None:
        """Defensively finishes any drag the canvas still thinks is
        active, exactly as mouseReleaseEvent would -- deliberately NOT
        a refactor of mouseReleaseEvent itself (which also has a
        selection side effect, the deferred plain-click collapse, that
        has no business running from a safety-net call site like this
        one) -- some duplication is the safer trade here.

        This exists specifically for callers like _on_break_clicked
        that treat self.paths as an authoritative, already-settled
        snapshot: reading it while a live drag preview is still
        uncommitted (see apply_paths_preview/on_point_moved, which
        write into self.paths immediately, well before any commit)
        would silently bake in a displacement that was never recorded
        as an EditStep -- which is exactly why Undo would have nothing
        to revert if this ever actually happens. Break is toolbar-
        button-only (no keyboard shortcut), so by the time it runs, a
        normal mouse release should ALREADY have cleared all of this;
        finding anything still active here is itself suspicious and
        worth surfacing loudly, not just silently patching over.

        Returns a short description of what was found (for logging),
        or None if nothing was pending -- the expected, normal case."""
        found: list[str] = []
        if self._point_drag_item is not None:
            pt = self._point_drag_item
            found.append(f"point drag (path {pt.path_index}, point {pt.point_index})")
            if self._point_drag_start is not None and self._point_drag_start != pt.pos():
                self.on_point_committed(pt.path_index, pt.point_index, pt.pos().x(), pt.pos().y())
            self._point_drag_item = None
            self._point_drag_start = None
            self._point_drag_escaped_start = False
        if self._group_drag_item is not None:
            found.append(f"group drag (paths {self._offset_drag_indices})")
            if self._group_drag_moved:
                self.end_group_offset_drag()
            self._group_drag_item = None
            self._group_drag_press_pos = None
            self._group_drag_moved = False
            self._group_drag_defer_collapse = False
        return "; ".join(found) if found else None

    def on_point_moved(self, path_index: int, point_index: int, x: float, y: float):
        self.paths[path_index][point_index] = (x, y)
        self._rebuild_ink()
        self._update_segments_touching(path_index, point_index)
        self.pointsChanged.emit()

    def on_point_committed(self, path_index: int, point_index: int, x: float, y: float):
        self.pointCommitted.emit(path_index, point_index, x, y)

    # -- segment selection --------------------------------------------------

    def on_segment_selection_changed(self, segment: SegmentItem, selected: bool):
        """Fired by Qt's own itemChange hook whenever setSelected() is
        called on a segment -- including the sync calls
        _refresh_selection_visuals makes to keep Qt's internal flag
        consistent with the two accumulation layers. Deliberately
        inert beyond emitting the signal: all actual visual work
        (pens, z-order, point states, tinting) is driven explicitly by
        _refresh_selection_visuals as ONE full recompute after each
        selection-building action finishes, not reactively per-item
        here -- an earlier version tried doing it per-item through
        this hook and ran into "can't yet know the final state
        mid-loop" bugs."""
        self.selectionChanged.emit()

    def clear_segment_selection(self):
        self.clear_all_selection()

    def _clamp_pan_to_box(self):
        """Keeps the box at least partly reachable while panning --
        the scene rect is fixed at the full canvas maximum (see
        load_glyph), so nothing about the scrollbars' own range stops
        a manual pan from sliding the box (and its guides) completely
        out of view into the mostly-empty margin beyond it. Confirmed
        as a real, reported problem: pulling descender itself off
        screen with a plain pan-down, with no way to see it without
        panning back manually. Clamped to keep the visible range's
        CENTER within the box+margin extent, rather than a stricter
        "box must stay fully visible" rule, which would defeat the
        point of being able to pan around a large box at all when
        zoomed in on part of it."""
        if self.box_rect is None:
            return
        x0, y0, x1, y1 = self.box_rect
        m = self.margin_width or 0.0
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        cx, cy = visible.center().x(), visible.center().y()
        clamped_cx = min(max(cx, x0 - m), x1 + m)
        clamped_cy = min(max(cy, y0 - m), y1 + m)
        if clamped_cx != cx or clamped_cy != cy:
            self.centerOn(clamped_cx, clamped_cy)

    def _update_box_visuals(self):
        """The cheap half of what resync_scene_to_box used to do on
        every drag tick -- just the box/margin outline's own geometry,
        no fitInView. Safe to call on every tick of a guide drag: this
        alone is what keeps the dashed boundary visually tracking
        cap/descender/advance as they move, without the view-rescaling
        that turned out to be the actual source of the jitter (see
        resync_scene_to_box's own docstring, and the guide-drag
        handlers below for the fuller reasoning on why view rescaling
        was dropped from the drag path entirely, not just throttled)."""
        if self.box_rect is None:
            return
        x0, y0, x1, y1 = self.box_rect
        margin = self.margin_width or 0.0
        if self._margin_item is not None:
            self._margin_item.setRect(x0 - margin, y0 - margin,
                                       (x1 - x0) + 2 * margin, (y1 - y0) + 2 * margin)
        if self._box_item is not None:
            self._box_item.setRect(x0, y0, x1 - x0, y1 - y0)

    def resync_scene_to_box(self):
        """Updates the box/margin visual items to match box_rect's
        CURRENT extent, then re-fits the view to that extent directly
        -- the scene rect itself is untouched (it was set once,
        generously, at load_glyph time, and stays fixed for the whole
        editing session; see that method's own comment for why).

        NOT called anywhere in the guide-drag path anymore (see
        _update_box_visuals, which handles that cheaper, fitInView-free
        case instead) -- re-fitting the view against box_rect on every
        drag tick, while box_rect itself was live-shrinking as the same
        drag moved cap/descender/advance, turned out to be the actual
        cause of a real, reported jitter bug: as two guides converged,
        the box's own aspect ratio went increasingly degenerate,
        driving fitInView's zoom factor into instability tick to tick.
        Removing live rescaling from guide-dragging entirely (not just
        throttling it) was the fix, and doing so raised the further
        question of whether auto-panning/scaling was ever the right
        model for guide-dragging in the first place -- it wasn't: with
        the view no longer moving out from under a drag, the user
        always has a fixed, visible frame of reference for exactly how
        far a guide is from the edge or from other guides, without
        needing to track a shifting boundary or fall back on legends.
        Guides are now clamped to whatever's currently visible instead
        -- reachable range depends on the current zoom, and reaching
        further is a deliberate zoom-out, not an automatic pan. Kept
        here, unused by guide-dragging, as a general utility in case
        some other, non-drag caller wants a one-shot re-fit against the
        current box (typing a value directly into a metrics field, for
        instance) -- unlike a live drag, a single, discrete commit has
        no equivalent tick-to-tick instability to worry about."""
        if self.box_rect is None:
            return
        x0, y0, x1, y1 = self.box_rect
        margin = self.margin_width or 0.0
        self._update_box_visuals()
        # Only force a fresh fit when the view was ALREADY in auto-fit
        # mode (matches load_glyph's own reasoning for a genuinely new
        # glyph) -- if the user had deliberately zoomed in to work on
        # fine detail, this shouldn't silently discard that zoom on
        # every single guide release. Confirmed as a real, reported
        # annoyance: moving a guide that never even left the current
        # view still reset a manual zoom back to fit-to-window every
        # time.
        if not self._user_zoomed:
            self.fitInView(QRectF(x0 - margin, y0 - margin,
                                   (x1 - x0) + 2 * margin, (y1 - y0) + 2 * margin),
                            Qt.KeepAspectRatio)
            self._zoom_level = 1.0
        self.viewChanged.emit()

    # begin_guide_drag() was removed entirely -- it used to pre-expand
    # the scene rect at the start of every guide drag so there'd be
    # room to pan into. Now that the scene rect is set ONCE, generously,
    # at load_glyph time (see there) and never touched again during
    # interaction, there's nothing left for this to do: the room
    # already exists from the start. This is also what actually fixed
    # the "jump" and "runaway drag" bugs that motivated building this
    # method in the first place -- see load_glyph's own comment for the
    # full reasoning.

    # update_guide_drag_pan() was removed -- panning at a fixed zoom
    # was superseded by rescaling the view live during the drag
    # instead (see GuideLabelStrip/AdvanceLabelStrip's own
    # mouseMoveEvent), which grows or shrinks the visible area
    # proportionally in the direction of the move rather than just
    # keeping a fixed-size window scrolled to follow it.

    def on_guide_moved(self, name: str, y: float):
        # Keeps box_rect in sync with the LIVE guide position, not just
        # whatever it was at the last full glyph load -- without this,
        # clamp_to_source_bounds (and _within_active_area) kept using
        # stale, pre-move bounds until the next reload. Confirmed as a
        # real, reproducible bug: shrink cap/descender/advance, then
        # draw a new segment -- it could still be placed using the OLD,
        # larger box, since that's what was still being clamped
        # against. "y" is generically named for the common (horizontal
        # guide) case but is actually an x-coordinate for "advance".
        if self.box_rect is not None and name in ("cap", "descender", "advance"):
            x0, y0, x1, y1 = self.box_rect
            if name == "cap":
                y0 = y
            elif name == "descender":
                y1 = y
            elif name == "advance":
                x1 = y
            self.box_rect = (x0, y0, x1, y1)
        self.guideChanged.emit(name, y)

    def set_weight(self, half_width: float):
        self.half_width = half_width
        self._rebuild_ink()

    def set_cap_style(self, cap_style: str):
        self.cap_style = cap_style
        self._rebuild_ink()

    # -- snapping ---------------------------------------------------------

    def _snap_to_points_xy(self, x: float, y: float, exclude: set[tuple[int, int]] | None
                            ) -> tuple[float, float] | None:
        best = None
        best_d = self.snap_point_tolerance
        for pi, path in enumerate(self.paths):
            for qi, (px, py) in enumerate(path):
                if exclude is not None and (pi, qi) in exclude:
                    continue
                d = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
                if d < best_d:
                    best_d = d
                    best = (px, py)
        # Also snap to the path currently being drawn, not just
        # already-committed ones -- otherwise there's no reliable way
        # to click back onto the path's own first point to close it
        # into a clean polygon.
        if self.draw_mode:
            for px, py in self._drawing_points:
                d = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
                if d < best_d:
                    best_d = d
                    best = (px, py)
        return best

    def _snap_to_grid_xy(self, x: float, y: float) -> tuple[float, float] | None:
        if not self.grid_spacing:
            return None
        g = self.grid_spacing
        ox, oy = self.grid_origin
        # snap relative to the SAME origin the grid is actually drawn
        # from (the box's top-left corner), not scene (0, 0) -- those
        # don't coincide on the Y axis (grid_origin's y is cap_top_px,
        # not 0), so snapping to raw round(y/g)*g put points a fixed
        # distance off of where the grid lines themselves are, which
        # is what looked like a persistent offset above every
        # horizontal line.
        return (ox + round((x - ox) / g) * g, oy + round((y - oy) / g) * g)

    def _snap_to_xheight_baseline_y(self, y: float) -> float | None:
        """Y-only: baseline and x-height are horizontal references, so
        only the Y coordinate is ever meant to lock to them -- X stays
        whatever the drag/draw was already going to place it at,
        independent of this. Returns None (not the unsnapped y) when
        neither guide is close enough, so callers can tell "no match"
        apart from "matched exactly at the input y"."""
        candidates = []
        for name in ("baseline", "x-height"):
            guide = self._guides.get(name)
            if guide is not None:
                candidates.append(guide.current_scene_position())
        if not candidates:
            return None
        best = min(candidates, key=lambda gy: abs(gy - y))
        return best if abs(best - y) < self.snap_point_tolerance else None

    def snap_guide_to_points_y(self, y: float) -> float | None:
        """The reverse of _snap_to_xheight_baseline_y: used when
        dragging any of the four horizontal guides (cap, x-height,
        baseline, descender), so it can lock onto an existing glyph
        point's height for precise alignment (e.g. setting baseline to
        exactly match where a shape was already drawn, or cap to its
        own highest point) -- only meaningful when "Snap to existing
        segments" is on, since that's the same checkbox this borrows
        its intent from, just applied to a guide instead of a drawn
        point. Only Y matters here; a horizontal guide line has no X of
        its own to snap. See snap_guide_to_points_x for the advance
        guide's own, X-only equivalent."""
        if not self.snap_to_points:
            return None
        best = None
        best_d = self.snap_point_tolerance
        for path in self.paths:
            for (px, py) in path:
                d = abs(py - y)
                if d < best_d:
                    best_d = d
                    best = py
        return best

    def snap_guide_to_points_x(self, x: float) -> float | None:
        """X-only counterpart to snap_guide_to_points_y, for the
        advance guide specifically -- the one guide that's vertical
        (its own position is an X, not a Y), so it has no use for the
        Y-based version above. Same reasoning otherwise: lets advance
        lock onto an existing point's X for precise alignment (e.g.
        matching a shape's own rightmost point), gated by the same
        "Snap to existing segments" checkbox."""
        if not self.snap_to_points:
            return None
        best = None
        best_d = self.snap_point_tolerance
        for path in self.paths:
            for (px, py) in path:
                d = abs(px - x)
                if d < best_d:
                    best_d = d
                    best = px
        return best

    # Real auto-traced geometry routinely produces points meant to be
    # the same vertex landing a fraction of a unit apart rather than
    # bit-for-bit identical (confirmed directly against traced test
    # data: observed gaps of roughly 0.7-2.1 units at genuinely-meant-
    # to-be-shared corners, including a path's own start/end failing to
    # closely close). A near-bitwise tolerance like the previous 1e-6
    # treats those as entirely distinct points, so the "other" one never
    # makes it into a drag's exclusion set and sits there as a
    # legitimate, very-close snap target for the whole drag -- reported
    # as erratic jitter, not just a wrong-answer snap. 3.0 comfortably
    # covers every gap actually observed while staying well under both
    # click_line_tolerance (5.0) and snap_point_tolerance (10.0), so
    # "coincident enough to treat as one vertex" stays a meaningfully
    # TIGHTER notion than either operational snap radius -- this is
    # deliberately its own, smaller number rather than reusing
    # find_join_chain's 5.0 (a different, user-INITIATED action where
    # being generous costs nothing); reusing that value here would
    # swallow half of snap_point_tolerance's own candidate radius and
    # suppress a deliberate, immediate drag-one-point-onto-another-
    # nearby-but-genuinely-distinct-point snap for the whole escape-
    # latch window.
    _COINCIDENCE_TOLERANCE = 3.0

    def _points_coincident_with(self, x: float, y: float,
                                 tolerance: float = _COINCIDENCE_TOLERANCE
                                 ) -> set[tuple[int, int]]:
        """Every (path_index, point_index) currently sitting at (x, y)
        (within _COINCIDENCE_TOLERANCE -- see its own comment) -- used
        to build a snap exclusion set that covers not just the one
        point actually being dragged, but any OTHER, different point
        that happens to already share (or nearly share) its starting
        coordinate. Confirmed as a real, reported bug otherwise:
        excluding only the dragged point's own identity (its old
        behavior) still let "snap to existing points" treat a
        different, coincident point (e.g. a separate segment's endpoint
        landing exactly on an unrelated polyline or group's own vertex)
        as a perfectly valid, very-nearby snap target -- so the instant
        a drag moved even slightly, it snapped right back to the same
        spot it started at, making that endpoint feel permanently stuck
        to whatever else was already there, unable to be dragged away
        at all."""
        found: set[tuple[int, int]] = set()
        for pi, path in enumerate(self.paths):
            for qi, (px, py) in enumerate(path):
                if abs(px - x) < tolerance and abs(py - y) < tolerance:
                    found.add((pi, qi))
        return found

    def _lines_coincident_with(self, x: float, y: float, tolerance: float = 1e-6
                                ) -> set[tuple[int, int]]:
        """Every (path_index, seg_index) whose OWN line passes through
        (x, y) -- the line-snap counterpart to _points_coincident_with,
        used the same way: seeding a drag's escape-latch exclusion set
        (see DraggablePoint.itemChange) so a point that starts sitting
        exactly ON a different, unrelated segment's line doesn't stay
        magnetically stuck to that same line the instant it tries to
        move away, for exactly the same reason a coincident POINT
        couldn't be allowed to either."""
        found: set[tuple[int, int]] = set()
        for seg in self._segments:
            line = seg.line()
            p1, p2 = seg.mapToScene(line.p1()), seg.mapToScene(line.p2())
            if _point_to_segment_distance(QPointF(x, y), p1, p2) < tolerance:
                found.add((seg.path_index, seg.seg_index))
        return found

    def _snap_to_line_xy(self, x: float, y: float, exclude_lines: set[tuple[int, int]] | None
                          ) -> tuple[float, float] | None:
        """The line half of snap-to-segments (see _snap_to_segments_xy
        for how this combines with the point half) -- closest point on
        any segment's own line within click_line_tolerance, the same
        fixed, weight-independent distance ordinary click-to-select
        (SegmentItem.shape()) and ambiguity-detection
        (_find_ambiguous_candidates) both already use, rather than a
        separately-tuned value. Also checks the path currently being
        drawn, not just already-committed ones -- same reasoning as
        _snap_to_points_xy's own equivalent check: drawing a stroke
        that should snap onto an earlier segment of that SAME,
        still-in-progress stroke needs this too, not only committed
        glyph geometry."""
        best = None
        best_d = self.click_line_tolerance
        for seg in self._segments:
            if exclude_lines is not None and (seg.path_index, seg.seg_index) in exclude_lines:
                continue
            line = seg.line()
            p1, p2 = seg.mapToScene(line.p1()), seg.mapToScene(line.p2())
            p = QPointF(x, y)
            d = _point_to_segment_distance(p, p1, p2)
            if d < best_d:
                best_d = d
                best = _closest_point_on_segment(p, p1, p2)
        if self.draw_mode and len(self._drawing_points) >= 2:
            p = QPointF(x, y)
            for i in range(len(self._drawing_points) - 1):
                a = QPointF(*self._drawing_points[i])
                b = QPointF(*self._drawing_points[i + 1])
                d = _point_to_segment_distance(p, a, b)
                if d < best_d:
                    best_d = d
                    best = _closest_point_on_segment(p, a, b)
        return best

    def _snap_to_segments_xy(self, x: float, y: float,
                              exclude_points: set[tuple[int, int]] | None,
                              exclude_lines: set[tuple[int, int]] | None,
                              line_only: bool = False
                              ) -> tuple[float, float] | None:
        """Point, then line -- an unconditional priority order, not a
        distance-based tie-break: a segment's own endpoint always wins
        over any line whenever it's within its own (more generous)
        snap_point_tolerance at all, regardless of whether some
        segment's line happens to be marginally closer. This is the
        combined search "Snap to segments" actually runs on every
        drag/draw tick -- both exclusion sets exist for the same
        reason and get built the same way (see _points_coincident_with
        / _lines_coincident_with and the escape-latch they seed).

        line_only skips the point half entirely, going straight to the
        line search -- see _temp_suspend_endpoint_snap's own comment for why this
        exists as a modifier on segment-snap rather than a second,
        independent checkbox."""
        if not line_only:
            point_hit = self._snap_to_points_xy(x, y, exclude_points)
            if point_hit is not None:
                return point_hit
        return self._snap_to_line_xy(x, y, exclude_lines)

    def apply_snap(self, x: float, y: float, exclude: set[tuple[int, int]] | None = None,
                    exclude_lines: set[tuple[int, int]] | None = None
                    ) -> tuple[float, float]:
        # Hold Ctrl to temporarily bypass snapping entirely, without
        # having to toggle the checkboxes off and back on -- a standard
        # convention in vector editors. Checked live at the moment of
        # the move (not cached), so it applies for exactly as long as
        # the key is actually held.
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            return (x, y)

        # G/S/H now tap-toggle their own checkbox directly (see
        # GlyphCanvas.keyPressEvent), so there's no separate temporary-
        # override branch left to check here -- the persistent,
        # checkbox-driven logic below is the only path now, which also
        # removes what used to be a second, separately-maintained copy
        # of the same H-then-S-then-G precedence order.
        #
        # Persistent, checkbox-driven modes. X-height/baseline is
        # checked FIRST and can override the Y that points/grid below
        # would otherwise have produced -- but doesn't PREVENT them
        # from independently snapping X, since x-height/baseline only
        # ever has an opinion about Y in the first place.
        #
        # Segments (point, then line -- see _snap_to_segments_xy) is
        # tried first and, if it actually finds something nearby, wins
        # over grid -- but "snap to segments is enabled" and "something
        # actually matched" are different things. A plain elif here
        # (branching on the CHECKBOX state) meant grid never got a
        # chance to apply at all whenever the segments checkbox merely
        # happened to be on, even on a click nowhere near any existing
        # geometry -- confirmed as a real, reported bug: with all three
        # enabled, grid snapping appeared completely broken, when what
        # was actually happening was segments silently winning by
        # default every time regardless of whether it had anything to
        # offer.
        result_x, result_y = x, y
        matched_segment = False
        if self.snap_to_points:
            best = self._snap_to_segments_xy(
                x, y, exclude, exclude_lines, line_only=self._temp_suspend_endpoint_snap)
            if best is not None:
                result_x, result_y = best
                matched_segment = True
        if not matched_segment and self.snap_to_grid:
            snapped = self._snap_to_grid_xy(x, y)
            if snapped is not None:
                result_x, result_y = snapped
        if self.snap_to_xheight_baseline:
            snapped_y = self._snap_to_xheight_baseline_y(y)
            if snapped_y is not None:
                result_y = snapped_y
        return (result_x, result_y)

    def _ray_box_max_distance(self, rx: float, ry: float, cos_a: float, sin_a: float,
                               box: tuple[float, float, float, float]) -> float:
        """The largest distance along a ray (from (rx,ry), direction
        (cos_a,sin_a)) that stays within box -- a standard ray-vs-
        axis-aligned-box intersection, used to clamp an angle-
        constrained drag to the boundary WITHOUT ever leaving the
        chosen angle (see apply_angle_snap). This replaces clamping x
        and y independently after the fact, which is what silently
        broke the angle at the boundary before: independently pulling
        an out-of-range x back in, for instance, left y sitting at its
        original, ray-derived value, landing the point off the ray
        entirely -- confirmed as a real, reported bug (the point
        "disconnects" from the crosshair once this happens, since
        Qt's own cursor tracking and the actually-rendered, now
        off-ray point stop agreeing on where a further mouse move
        should land)."""
        x0, y0, x1, y1 = box
        best = float("inf")
        if abs(cos_a) > 1e-9:
            for bx in (x0, x1):
                t = (bx - rx) / cos_a
                if t > 1e-9:
                    y_at = ry + t * sin_a
                    if y0 - 1e-6 <= y_at <= y1 + 1e-6:
                        best = min(best, t)
        if abs(sin_a) > 1e-9:
            for by in (y0, y1):
                t = (by - ry) / sin_a
                if t > 1e-9:
                    x_at = rx + t * cos_a
                    if x0 - 1e-6 <= x_at <= x1 + 1e-6:
                        best = min(best, t)
        return best

    def _ray_segment_intersect(self, rx: float, ry: float, cos_a: float, sin_a: float,
                                ax: float, ay: float, bx: float, by: float) -> float | None:
        """Distance along the ray (from (rx,ry), direction (cos_a,sin_a))
        at which it crosses segment (a,b), or None if they don't
        intersect within both the ray's positive extent and the
        segment's own endpoints. Standard ray-vs-line-segment
        intersection (Cramer's rule on the 2x2 system); used by
        _angle_ray_snap_distance's line half -- a constrained drag
        "snapping to a line" most naturally means stopping exactly
        where the chosen angle actually crosses that existing line,
        not the closest approach to it, the way an unconstrained
        drag's own line-snap works."""
        sx, sy = bx - ax, by - ay
        denom = sx * sin_a - sy * cos_a
        if abs(denom) < 1e-9:
            return None   # parallel (or coincident) -- no single crossing point
        t = (sx * (ay - ry) - sy * (ax - rx)) / denom
        u = (cos_a * (ay - ry) - sin_a * (ax - rx)) / denom
        if t > 1e-9 and -1e-6 <= u <= 1 + 1e-6:
            return t
        return None

    def _angle_ray_snap_distance(self, rx: float, ry: float, cos_a: float, sin_a: float,
                                  exclude: set[tuple[int, int]] | None,
                                  exclude_lines: set[tuple[int, int]] | None,
                                  raw_dist: float,
                                  line_only: bool = False) -> float | None:
        """Point, then line -- the same unconditional priority
        _snap_to_segments_xy already uses, just measured as a distance
        along the angle-constrained ray instead of around a free
        (x, y). Only points/intersections AHEAD of the reference
        (positive distance) count -- one behind it isn't a reachable
        target for this drag. Returns the distance along the ray
        where the best match lies, or None if nothing qualifies.

        Both halves are bounded by real 2D distance from the RAW
        (unsnapped) target -- (rx + raw_dist*cos_a, ry + raw_dist*sin_a),
        i.e. where the cursor actually is right now, projected onto
        the chosen ray only in the sense of "this is roughly where
        along it the user is pointing". Confirmed as a real, reported
        bug otherwise: the point half used to measure only PERPENDICULAR
        distance to the ray's own infinite line, with no bound at all
        on how far along the ray a candidate was allowed to be, so a
        point much farther from the cursor than a genuinely close line
        could still win outright just by lying almost exactly on the
        ray. The line half had the same class of problem from the
        other direction -- it picked whichever crossing was nearest
        the REFERENCE point (smallest t from the start of the ray),
        not nearest the cursor's actual current position along it, so
        a line far past the reference could jump the snap forward
        regardless of where the drag currently was. Bounding both to
        real distance from the raw target fixes both at once, and
        keeps them on equal footing for comparison the way grid and
        x-height/baseline candidates now are too (see
        apply_angle_snap).

        line_only skips the point half entirely -- mirrors
        _snap_to_segments_xy's own use of the same name for the E-held
        endpoint-priority suspend (see _temp_suspend_endpoint_snap's
        own comment). Previously not wired in here at all: a real,
        reported bug where holding E had no effect once angle-snap
        was also active, unlike the unconstrained drag where it always
        worked."""
        target_x = rx + raw_dist * cos_a
        target_y = ry + raw_dist * sin_a
        if not line_only:
            best_t = None
            best_d = self.snap_point_tolerance
            for pi, path in enumerate(self.paths):
                for qi, (px, py) in enumerate(path):
                    if exclude is not None and (pi, qi) in exclude:
                        continue
                    t = (px - rx) * cos_a + (py - ry) * sin_a
                    if t <= 1e-9:
                        continue
                    d = math.hypot(px - target_x, py - target_y)
                    if d < best_d:
                        best_d = d
                        best_t = t
            if best_t is not None:
                return best_t
        best_t = None
        best_d = self.snap_point_tolerance
        for seg in self._segments:
            if exclude_lines is not None and (seg.path_index, seg.seg_index) in exclude_lines:
                continue
            line = seg.line()
            a, b = seg.mapToScene(line.p1()), seg.mapToScene(line.p2())
            t = self._ray_segment_intersect(rx, ry, cos_a, sin_a, a.x(), a.y(), b.x(), b.y())
            if t is None:
                continue
            cx, cy = rx + t * cos_a, ry + t * sin_a
            d = math.hypot(cx - target_x, cy - target_y)
            if d < best_d:
                best_d = d
                best_t = t
        return best_t

    def _angle_ray_grid_distance(self, rx: float, ry: float, cos_a: float, sin_a: float,
                                  raw_dist: float) -> float | None:
        """Nearest point along the ray where it actually crosses a
        real grid line (vertical x = grid_origin.x + n*spacing, or
        horizontal y = grid_origin.y + m*spacing) -- replaces an
        earlier version that instead rounded the raw ray-distance
        itself to the nearest multiple of the spacing. Confirmed as a
        real, reported bug: that only ever coincided with the true
        grid at the 4 cardinal/45-degree angles: for any other angle
        (22.5 degrees included, since tan(22.5 degrees) is irrational)
        a ray from an arbitrary reference point essentially never
        passes through an actual grid corner at a regular spacing
        along its own length, so rounding its distance landed
        systematically off the real grid -- visually "a different
        grid" than the one actually drawn, which is exactly what was
        reported.

        Only the two grid lines immediately bracketing the raw
        (unsnapped) target are checked per axis -- grid lines repeat
        regularly, so nothing farther out can ever be nearer."""
        if not self.grid_spacing:
            return None
        g = self.grid_spacing
        ox, oy = self.grid_origin
        target_x = rx + raw_dist * cos_a
        target_y = ry + raw_dist * sin_a
        candidates = []
        if abs(cos_a) > 1e-9:
            n0 = math.floor((target_x - ox) / g)
            for n in (n0, n0 + 1):
                t = (ox + n * g - rx) / cos_a
                if t > 1e-9:
                    candidates.append(t)
        if abs(sin_a) > 1e-9:
            m0 = math.floor((target_y - oy) / g)
            for m in (m0, m0 + 1):
                t = (oy + m * g - ry) / sin_a
                if t > 1e-9:
                    candidates.append(t)
        if not candidates:
            return None
        return min(candidates, key=lambda t: abs(t - raw_dist))

    def _angle_ray_xheight_baseline_distance(self, rx: float, ry: float, cos_a: float,
                                              sin_a: float, raw_dist: float) -> float | None:
        """Where the ray crosses the baseline or x-height guide,
        whichever is closer to the raw (unsnapped) target -- the ray
        equivalent of _snap_to_xheight_baseline_y, needed because X
        and Y move together once an angle is fixed (there's no way to
        independently override just Y the way the unconstrained drag
        does). Confirmed as a real, reported bug that this didn't
        exist at all before: x-height/baseline snap had zero effect
        whatsoever whenever angle-snap was also active, identical
        output whether the checkbox was on or off.

        The tolerance check is done in actual on-screen Y distance at
        the raw target (mirroring _snap_to_xheight_baseline_y's own
        check exactly), not a ray-distance delta -- a fixed ray-
        distance threshold would correspond to a different real
        vertical distance at every angle (shrinking toward zero as the
        ray flattens toward horizontal), which would make the
        tolerance inconsistent depending on which of the 8 angles
        happened to be chosen."""
        if abs(sin_a) < 1e-9:
            return None   # a horizontal ray never crosses a horizontal guide at a NEW point
        target_y = ry + raw_dist * sin_a
        candidates = []
        for name in ("baseline", "x-height"):
            guide = self._guides.get(name)
            if guide is None:
                continue
            gy = guide.current_scene_position()
            if abs(gy - target_y) >= self.snap_point_tolerance:
                continue
            t = (gy - ry) / sin_a
            if t > 1e-9:
                candidates.append(t)
        if not candidates:
            return None
        return min(candidates, key=lambda t: abs(t - raw_dist))

    def apply_angle_snap(self, x: float, y: float, reference: tuple[float, float] | None,
                          exclude: set[tuple[int, int]] | None = None,
                          exclude_lines: set[tuple[int, int]] | None = None
                          ) -> tuple[float, float]:
        """Constrain to 22.5-degree increments measured from the given
        reference point. Takes the reference directly rather than
        deriving it internally from a DraggablePoint -- the two
        callers need genuinely different lookups for "what am I
        measuring the angle from": an existing point being dragged
        measures from the OTHER end of whichever selected segment it
        belongs to (see _angle_reference, unambiguous because a point
        only becomes draggable once its segment is selected), while a
        freshly-drawn point has no segment or selection at all yet --
        its reference is simply the previous point placed in the SAME
        in-progress stroke (see _draw_angle_reference). Both funnel
        into this one, reference-agnostic core rather than duplicating
        the actual 22.5-degree math twice.

        Angle-snap ADDS a condition to grid/point-snap rather than
        replacing them outright (an earlier version did exactly that,
        confirmed as a real, reported limitation: there was no way to
        be both on a clean angle and aligned to the grid or existing
        geometry at once). Once the angle itself is chosen, every
        other snap source -- grid, segments, x-height/baseline, the
        working boundary -- is expressed as a candidate DISTANCE along
        that one already-fixed ray, never as an independent (x, y)
        that could pull the point off the ray again, and all of them
        are bounded by real on-screen distance from the RAW
        (unsnapped) target rather than some looser ray-only measure --
        see _angle_ray_snap_distance/_angle_ray_grid_distance/
        _angle_ray_xheight_baseline_distance's own docstrings for the
        real, reported bugs each of those replaced. The priority order
        mirrors apply_snap's own, unconstrained one exactly: segments
        (point, then line) tried first and, if they actually match,
        win outright over grid; grid is the fallback when they don't;
        x-height/baseline is checked independently of both, against
        the raw target rather than whatever segments/grid already
        produced, and can override the final result on its own -- the
        same shape as apply_snap's own Y-only override, just expressed
        as a ray-distance instead of a free Y. The boundary is always
        enforced last, regardless of what the other three produced,
        the same way it's the final word for an unconstrained drag too."""
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            return (x, y)   # Ctrl bypass applies here too, same as apply_snap
        if reference is None:
            # No previous point to measure an angle from -- the very
            # first point of a fresh stroke, or an existing point with
            # no OTHER end to reference (shouldn't normally happen,
            # see _angle_reference). Confirmed as a real, reported
            # bug otherwise: falling straight through to the raw,
            # completely unsnapped (x, y) meant the first point of a
            # stroke skipped grid/segment/x-height-baseline snap
            # entirely whenever angle-snap happened to already be on
            # -- there's nothing for angle-snap itself to constrain
            # yet, but that's not a reason to skip every OTHER snap
            # source too. Falling back to the ordinary combined snap
            # instead makes the first point behave exactly like
            # drawing it with angle-snap off and turning angle-snap on
            # afterward -- which already worked correctly, and is
            # what this was silently failing to match.
            return self.apply_snap(x, y, exclude, exclude_lines)
        rx, ry = reference
        dx, dy = x - rx, y - ry
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return (x, y)
        angle = math.atan2(dy, dx)
        step = math.radians(22.5)
        snapped_angle = round(angle / step) * step
        cos_a, sin_a = math.cos(snapped_angle), math.sin(snapped_angle)
        # The raw (pre-snap) distance -- every candidate below is
        # measured against where the cursor actually is right now, not
        # against whatever an earlier candidate already produced, so
        # this is captured once up front rather than re-derived from a
        # moving `dist`.
        raw_dist = dist

        # Same point-then-grid priority apply_snap already uses for an
        # unconstrained drag (see its own comment on why grid needs an
        # explicit "did points actually match" check, not a plain elif).
        matched = False
        if self.snap_to_points:
            candidate = self._angle_ray_snap_distance(
                rx, ry, cos_a, sin_a, exclude, exclude_lines, raw_dist,
                line_only=self._temp_suspend_endpoint_snap)
            if candidate is not None:
                dist = candidate
                matched = True
        if not matched and self.snap_to_grid and self.grid_spacing:
            candidate = self._angle_ray_grid_distance(rx, ry, cos_a, sin_a, raw_dist)
            if candidate is not None:
                dist = candidate

        # Independent of whichever of the two above ran, same as
        # apply_snap's own x-height/baseline check -- see this
        # function's own docstring for why X and Y can't be split the
        # same way here.
        if self.snap_to_xheight_baseline:
            candidate = self._angle_ray_xheight_baseline_distance(rx, ry, cos_a, sin_a, raw_dist)
            if candidate is not None:
                dist = candidate

        if self.box_rect is not None:
            max_dist = self._ray_box_max_distance(rx, ry, cos_a, sin_a, self.box_rect)
            dist = min(dist, max_dist)

        return (rx + dist * cos_a, ry + dist * sin_a)

    def _angle_reference(self, moving_point: "DraggablePoint") -> tuple[float, float] | None:
        for seg in self.selected_segments:
            if seg.point_a is moving_point:
                p = seg.point_b.pos()
                return (p.x(), p.y())
            if seg.point_b is moving_point:
                p = seg.point_a.pos()
                return (p.x(), p.y())
        return None

    def _draw_angle_reference(self) -> tuple[float, float] | None:
        """The angle-snap reference while drawing a new stroke: the
        previous point placed in this SAME in-progress polyline, or
        None for the very first point of a fresh stroke (nothing to
        measure an angle from yet -- apply_angle_snap already handles
        a None reference by returning the position unconstrained, the
        same fallback _angle_reference's own None case uses)."""
        if self._drawing_points:
            return self._drawing_points[-1]
        return None

    def clamp_to_source_bounds(self, x: float, y: float) -> tuple[float, float]:
        """Keeps a dragged/drawn point within the glyph's actual
        working area -- the metrics-defined box itself (cap to
        descender, 0 to advance), not the raw source canvas dimensions
        and NOT expanded by the weight-overshoot margin either. That
        margin exists purely as rendering space so the THICKENED ink's
        round-cap overshoot isn't clipped by the scene rect -- it was
        never meant as a placement zone for the skeleton itself.
        Confirmed as a real bug: allowing it let a drawn point land at
        y=564 on a glyph whose own descender guide sits at 506, well
        past where the box says content should end.

        Falls back to source_bounds when there's no box yet (nothing
        loaded), and is still bounded by the scene rect either way so a
        point can never land somewhere unreachable/unscrollable-to."""
        if self.box_rect is not None:
            x0, y0, x1, y1 = self.box_rect
            x = min(max(x, x0), x1)
            y = min(max(y, y0), y1)
        elif self.source_bounds is not None:
            w, h = self.source_bounds
            x, y = min(max(x, 0.0), w), min(max(y, 0.0), h)
        scene_rect = self.sceneRect()
        if not scene_rect.isEmpty():
            x = min(max(x, scene_rect.left()), scene_rect.right())
            y = min(max(y, scene_rect.top()), scene_rect.bottom())
        return (x, y)

    # -- grid overlay -------------------------------------------------------

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        # grid lines are now drawn by GridItem (a real scene item, see
        # its docstring for why) -- this override just keeps the plain
        # canvas background painted underneath everything else.

    def set_grid(self, spacing: float | None, snap_grid: bool, snap_points: bool,
                 snap_xheight_baseline: bool = False):
        self.grid_spacing = spacing
        self.snap_to_grid = snap_grid
        self.snap_to_points = snap_points
        self.snap_to_xheight_baseline = snap_xheight_baseline
        if self._grid_item is not None:
            self._grid_item.update()
        self.viewport().update()

    def set_angle_snap(self, enabled: bool):
        self.angle_snap_enabled = enabled

    # -- freehand drawing -------------------------------------------------

    def set_draw_mode(self, enabled: bool):
        if enabled and self.read_only:
            # Refuses to ENTER draw mode at all while viewing an
            # excluded glyph -- defensive backstop alongside
            # draw_action's own disabled state (see MainWindow's own
            # refresh of it), not the primary mechanism, since the
            # toolbar button itself should already be unclickable by
            # the time this could ever be reached this way. Exiting
            # (enabled=False) is never blocked, regardless of
            # read_only -- there's nothing to refuse there.
            return
        if not enabled and self._drawing_points:
            self._cancel_drawing()
        self.draw_mode = enabled
        self.setCursor(self._resting_cursor())

    def _resting_cursor(self):
        """What the cursor should look like whenever nothing more
        specific (panning's closed-hand) is overriding it -- blank
        while actually over the canvas, since the crosshair already
        shows position precisely and the OS cursor arrow/cross just
        sits on top of it, otherwise the normal draw-mode/arrow cursor
        for outside the canvas (e.g. hovering the toolbar)."""
        if self._mouse_over_canvas:
            return Qt.BlankCursor
        return Qt.CrossCursor if self.draw_mode else Qt.ArrowCursor

    def enterEvent(self, event):
        self._mouse_over_canvas = True
        if not self._panning:
            self.setCursor(self._resting_cursor())
        # Without this, keyboard focus stays wherever it last was (e.g.
        # a Grid & Snap checkbox just clicked to toggle a persistent
        # mode) even after the mouse moves back onto the canvas -- and
        # G/E/H are handled in THIS widget's own keyPressEvent, so they
        # silently went nowhere. Confirmed as the actual cause of a
        # real, reported bug: the temporary overrides appeared not to
        # work AT ALL in normal use, not just in some edge case,
        # because focus is essentially never on the canvas by default
        # after interacting with any other control first.
        self.setFocus()
        super().enterEvent(event)

    def set_onion_skin(self, paths: list | None, half_width: float, cap_style: str):
        """A faint reference outline (e.g. the nearest already-drawn
        neighbor glyph) shown behind the current glyph while drawing --
        purely a visual weight/proportion guide, never part of the
        glyph's own geometry."""
        if self._onion_item is not None:
            self.scene_.removeItem(self._onion_item)
            self._onion_item = None
        if not paths:
            return
        qpath = _paths_to_qpath(paths, half_width, cap_style)
        if qpath is None:
            return
        item = QGraphicsPathItem(qpath)
        item.setBrush(QBrush(QColor(0, 0, 0, 28)))   # faint -- a proportion guide, not competing with real ink
        item.setPen(QPen(Qt.NoPen))
        item.setZValue(0.5)   # above the grid/margin, below the real ink and points
        self.scene_.addItem(item)
        self._onion_item = item

    def _handle_draw_click(self, event):
        raw_pos = self.mapToScene(event.pos())
        # Same priority rule as an existing point's own drag (see
        # DraggablePoint.itemChange's own comment): angle-snap, when
        # on, ADDS a condition on top of whatever grid/segment/
        # x-height-baseline snap is already enabled, rather than
        # replacing it -- see apply_angle_snap's own docstring for the
        # full reasoning.
        if self.angle_snap_enabled:
            snapped = self.apply_angle_snap(raw_pos.x(), raw_pos.y(), self._draw_angle_reference())
        else:
            snapped = self.apply_snap(raw_pos.x(), raw_pos.y())
        snapped = self.clamp_to_source_bounds(*snapped)
        self._drawing_points.append(snapped)
        self._rebuild_drawing_preview()

    def _rebuild_drawing_preview(self):
        for item in self._drawing_preview_items:
            self.scene_.removeItem(item)
        self._drawing_preview_items = []
        pts = self._drawing_points
        color = QColor(style.GUIDE_COLOR)
        for i in range(len(pts) - 1):
            line = QGraphicsLineItem(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
            pen = QPen(color, 2 * self._visual_scale)
            line.setPen(pen)
            line.setZValue(29)
            self.scene_.addItem(line)
            self._drawing_preview_items.append(line)
        for x, y in pts:
            r = self.DRAW_DOT_RADIUS
            dot = QGraphicsEllipseItem(x - r, y - r, r * 2, r * 2)
            dot.setBrush(QBrush(color))
            dot.setPen(QPen(Qt.NoPen))
            dot.setZValue(29)
            self.scene_.addItem(dot)
            self._drawing_preview_items.append(dot)

    def _finish_drawing(self):
        if len(self._drawing_points) >= 2:
            self.pathDrawn.emit(list(self._drawing_points))
        self._clear_drawing_state()

    def _cancel_drawing(self):
        self._clear_drawing_state()

    def _clear_drawing_state(self):
        self._drawing_points = []
        for item in self._drawing_preview_items:
            self.scene_.removeItem(item)
        self._drawing_preview_items = []
        if self._rubber_band_line is not None:
            self.scene_.removeItem(self._rubber_band_line)
            self._rubber_band_line = None

    # -- paste-follows-cursor ---------------------------------------------

    def start_paste_mode(self, clip_paths: list, cursor_pos: QPointF | None = None) -> None:
        """Attaches a ghost preview of clip_paths to the cursor -- a
        single left-click places it (see mousePressEvent), right-click
        or Escape cancels (see mousePressEvent/keyPressEvent). Mirrors
        set_draw_mode's own "mode" pattern throughout.

        clip_paths: same [[(x, y), ...], ...] shape
        _build_clipboard_from_selection already produces -- caller
        (MainWindow._on_paste_clicked) owns deciding what's being
        pasted; this only owns the placement interaction itself, not
        the eventual insertion (see pastePlaced's own signal comment).

        Anchor: the top-left ENDPOINT among clip_paths' own actual
        points -- see paste_anchor_point's own docstring for why this
        must be a real vertex, not a synthetic bounding-box corner. The
        ghost is offset so THIS point lands exactly on the (snapped)
        cursor, not the shape's own first point or centroid.

        cursor_pos: where to show the ghost immediately, before the
        first mouseMoveEvent ever arrives. Callers should pass the
        real, live cursor position whenever the mouse is actually over
        the canvas (see MainWindow._on_paste_clicked, the one call
        site, for how); this fallback -- the visible center of the
        current viewport -- only covers the genuine remaining case,
        where the mouse isn't over the canvas at all."""
        if not clip_paths:
            return
        if self.read_only:
            # Defensive backstop alongside paste_action's own disabled
            # state (see MainWindow's own refresh of it) -- same
            # reasoning as set_draw_mode's own identical check.
            return
        if self._paste_mode:
            self._clear_paste_state()   # a paste was already in progress -- replace it, not stack two
        self._paste_mode = True
        self._paste_clip_paths = clip_paths
        self._paste_anchor = paste_anchor_point(clip_paths)

        qpath = _paths_to_qpath(clip_paths, self.half_width, self.cap_style)
        if qpath is not None:
            item = QGraphicsPathItem(qpath)
            # Distinct from onion_item's own faint gray reference look
            # (see set_onion_skin) -- this is an active placement
            # preview the user is about to commit, not a passive
            # weight/proportion guide, so it reads as "real ink, not
            # yet placed" rather than "background reference."
            item.setBrush(QBrush(QColor(style.GUIDE_COLOR).lighter(140)))
            item.setOpacity(0.55)
            item.setPen(QPen(Qt.NoPen))
            item.setZValue(31)   # above the rubber-band rect/drawing preview (29-30), still below real selection handles
            self.scene_.addItem(item)
            self._paste_ghost_item = item

        if cursor_pos is None:
            cursor_pos = self.mapToScene(self.viewport().rect()).boundingRect().center()
        snapped = self.apply_snap(cursor_pos.x(), cursor_pos.y())
        self._update_paste_ghost_at(snapped)
        self.setCursor(Qt.CrossCursor)

    def _update_paste_ghost_at(self, pos: tuple[float, float]) -> None:
        if self._paste_ghost_item is None or self._paste_anchor is None:
            return
        ax, ay = self._paste_anchor
        self._paste_ghost_item.setPos(pos[0] - ax, pos[1] - ay)

    def cancel_paste_mode(self) -> None:
        """Public -- callable from MainWindow too (e.g. a glyph-list
        switch, or starting a different placement-style action, while a
        paste is already in progress), not just from this widget's own
        mousePressEvent/keyPressEvent handling of it."""
        if self._paste_mode:
            self._clear_paste_state()

    def _clear_paste_state(self) -> None:
        self._paste_mode = False
        self._paste_clip_paths = None
        self._paste_anchor = None
        if self._paste_ghost_item is not None:
            self.scene_.removeItem(self._paste_ghost_item)
            self._paste_ghost_item = None
        self.setCursor(self._resting_cursor())

    # -- crosshair cursor -----------------------------------------------

    def mouseMoveEvent(self, event):
        # Re-asserted on every move, not just once in enterEvent -- the
        # enterEvent-only version was confirmed NOT reliable in actual
        # use: G/E/H worked after a click but not after just hovering,
        # meaning something (most likely QGraphicsView's focus-proxy
        # relationship with its own viewport, or the scene's own
        # internal hover handling) was contending with or undoing a
        # single setFocus() call made only at entry. Checking
        # hasFocus() first keeps this a cheap no-op on every one of the
        # many calls this gets during normal mouse movement, only
        # actually calling setFocus() on the rare tick it's actually
        # needed.
        if not self.hasFocus():
            self.setFocus()
        if self._panning and self._pan_last_pos is not None:
            delta = event.pos() - self._pan_last_pos
            self._pan_last_pos = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self._clamp_pan_to_box()
            self.viewChanged.emit()
            super().mouseMoveEvent(event)
            return

        if self._point_drag_item is not None:
            # Self-contained, explicit point move -- see
            # _point_drag_item's own comment for why this doesn't rely
            # on Qt's native ItemIsMovable dragging. setPos() still
            # goes through the item's own itemChange (snap/clamp,
            # on_point_moved) exactly as a native drag would -- only
            # the "what moves, and in response to what" wiring is
            # different, not the positioning logic itself.
            current = self.mapToScene(event.pos())
            self._point_drag_item.setPos(current)
            self._update_crosshair_at(current)
            event.accept()
            return

        if self._group_drag_item is not None:
            current = self.mapToScene(event.pos())
            press_dx = current.x() - self._group_drag_press_pos.x()
            press_dy = current.y() - self._group_drag_press_pos.y()
            if not self._group_drag_moved and (abs(press_dx) > GROUP_DRAG_THRESHOLD
                                                or abs(press_dy) > GROUP_DRAG_THRESHOLD):
                self._group_drag_moved = True
                self.begin_group_offset_drag()
            if self._group_drag_moved:
                # Raw cursor scene position, not a press-relative delta
                # -- update_group_offset_drag derives the actual offset
                # from where the group's ANCHOR point needs to land to
                # reach here (snapped), not from raw mouse movement.
                self.update_group_offset_drag(current.x(), current.y())
            self._update_crosshair_at(current)
            event.accept()
            return

        if self._rubber_band_start is not None:
            current = self.mapToScene(event.pos())
            dx = current.x() - self._rubber_band_start.x()
            dy = current.y() - self._rubber_band_start.y()
            if not self._rubber_band_moved and (abs(dx) > GROUP_DRAG_THRESHOLD
                                                 or abs(dy) > GROUP_DRAG_THRESHOLD):
                self._rubber_band_moved = True
            if self._rubber_band_moved:
                x0, y0 = self._rubber_band_start.x(), self._rubber_band_start.y()
                x1, y1 = current.x(), current.y()
                rect = QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
                if self._rubber_band_rect_item is None:
                    self._rubber_band_rect_item = QGraphicsRectItem()
                    self._rubber_band_rect_item.setPen(QPen(QColor(80, 140, 255), 1, Qt.DashLine))
                    self._rubber_band_rect_item.setBrush(QBrush(QColor(80, 140, 255, 40)))
                    self._rubber_band_rect_item.setZValue(30)
                    self.scene_.addItem(self._rubber_band_rect_item)
                self._rubber_band_rect_item.setRect(rect)
            self._update_crosshair_at(current)
            event.accept()
            return

        if self._crosshair_h is not None and self._crosshair_v is not None:
            raw_pos = self.mapToScene(event.pos())
            self._update_crosshair_at(raw_pos)
        super().mouseMoveEvent(event)

    def _update_crosshair_at(self, raw_pos: QPointF):
        """Moves the crosshair to follow raw_pos (a scene position) --
        factored out so it can be called from EVERY mouse-move path,
        not just the "nothing else is happening" default one.
        Group-drag and rubber-band-select both used to return early
        from mouseMoveEvent before ever reaching this, leaving the
        crosshair frozen at wherever it was when the drag started until
        release -- confirmed as a real, visible bug while dragging a
        segment (a group-of-one drag)."""
        if self._crosshair_h is None or self._crosshair_v is None:
            return
        if self._within_active_area(raw_pos.x(), raw_pos.y()):
            actively_drawing = self.draw_mode and bool(self._drawing_points)
            # Same priority rule as an actual draw-click (see
            # _handle_draw_click) -- so the dashed helper line the
            # crosshair draws while extending a stroke shows the SAME
            # position a click would actually place, rather than the
            # preview quietly disagreeing with the real result.
            if actively_drawing and self.angle_snap_enabled:
                snapped = self.apply_angle_snap(raw_pos.x(), raw_pos.y(), self._draw_angle_reference())
            else:
                snapped = self.apply_snap(raw_pos.x(), raw_pos.y())
            # While actively extending a line, the crosshair itself is
            # ALSO clamped to the strict box (not just the helper line)
            # -- _within_active_area above still allows the wider
            # margin so the cursor doesn't lose the crosshair entirely
            # while briefly passing through it, but the crosshair and
            # the helper line need to stay visually "connected" (the
            # same endpoint) rather than the crosshair continuing on
            # into the margin while the helper line stops short at the
            # boundary. Outside of active drawing, the crosshair keeps
            # showing the raw position -- it's still a useful alignment
            # reference there, and there's no helper line for it to
            # visually disagree with.
            if self._paste_mode and self._paste_anchor is not None and self._paste_clip_paths is not None:
                # Offset-based (see _clamp_offset_to_box), not a plain
                # clamp_to_source_bounds on the anchor alone -- confirmed
                # as a real, reported bug otherwise: clamping only the
                # anchor point kept THAT one point on-canvas while the
                # rest of the pasted content, extending away from it,
                # could still land well outside the canvas bounds. This
                # keeps the WHOLE clipboard's own collective bounding
                # box on-canvas, the same guarantee _clamp_group_offset
                # already provides for dragging an existing selection.
                ax, ay = self._paste_anchor
                dx, dy = self._clamp_offset_to_box(
                    snapped[0] - ax, snapped[1] - ay,
                    [pt for path in self._paste_clip_paths for pt in path])
                pos = QPointF(ax + dx, ay + dy)
            elif actively_drawing:
                pos = QPointF(*self.clamp_to_source_bounds(*snapped))
            else:
                pos = QPointF(*snapped)
            if self._paste_mode:
                self._update_paste_ghost_at((pos.x(), pos.y()))
            visible = self.mapToScene(self.viewport().rect()).boundingRect()
            self._crosshair_h.setLine(visible.left(), pos.y(), visible.right(), pos.y())
            self._crosshair_v.setLine(pos.x(), visible.top(), pos.x(), visible.bottom())
            self._crosshair_h.setVisible(True)
            self._crosshair_v.setVisible(True)
            self.cursorMoved.emit(pos.x(), pos.y())
            if actively_drawing:
                last = self._drawing_points[-1]
                if self._rubber_band_line is None:
                    self._rubber_band_line = QGraphicsLineItem()
                    pen = QPen(QColor(style.GUIDE_COLOR), 1.5 * self._visual_scale, Qt.DashLine)
                    self._rubber_band_line.setPen(pen)
                    self._rubber_band_line.setZValue(30)
                    self.scene_.addItem(self._rubber_band_line)
                # Same clamped point the crosshair now uses above, so
                # the helper line's end and the crosshair's
                # intersection are always the exact same point -- see
                # this method's own docstring addition for the
                # reasoning. Previously computed independently here,
                # which is what let the helper line stop at the
                # boundary while the crosshair kept going -- a real,
                # reported visual disconnect between the two.
                self._rubber_band_line.setLine(last[0], last[1], pos.x(), pos.y())
                self._rubber_band_line.setVisible(True)
        else:
            # outside the box + max-weight margin -- there's nothing
            # meaningful to align against out there, so don't dangle
            # a crosshair (or a coordinate readout) over empty space
            self._crosshair_h.setVisible(False)
            self._crosshair_v.setVisible(False)

    def _within_active_area(self, x: float, y: float) -> bool:
        if self.box_rect is None:
            return True
        x0, y0, x1, y1 = self.box_rect
        m = self.margin_width or 0.0
        return (x0 - m) <= x <= (x1 + m) and (y0 - m) <= y <= (y1 + m)

    def leaveEvent(self, event):
        if self._crosshair_h is not None and self._crosshair_v is not None:
            self._crosshair_h.setVisible(False)
            self._crosshair_v.setVisible(False)
        self._mouse_over_canvas = False
        if not self._panning:
            self.setCursor(self._resting_cursor())
        super().leaveEvent(event)

    def resizeEvent(self, event):
        # Re-fit whenever the window (and so this view) is resized --
        # UNLESS the user has manually zoomed, in which case resizing
        # would otherwise silently discard that zoom, which is worse
        # than leaving the content at a slightly different apparent
        # scale until they explicitly hit fit-to-window again.
        super().resizeEvent(event)
        if not self.sceneRect().isEmpty():
            if not self._user_zoomed:
                self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
            else:
                # Preserving zoom is right, but _zoom_level's baseline
                # ("1.0" = fit-to-window) still needs to track the
                # CURRENT widget size, or it silently goes stale the
                # moment a resize happens while already zoomed -- e.g.
                # the aspect-locked canvas resizing on every glyph
                # load. Left uncorrected, "1.0" quietly keeps meaning
                # whatever the OLD size's fit scale was, so zooming
                # out stops short of an actual fit, and the whole
                # zoom range ends up shifted relative to the real
                # current size.
                fresh_fit = self._compute_fit_scale()
                if fresh_fit > 1e-9:
                    self._zoom_level = self.transform().m11() / fresh_fit
        self.viewChanged.emit()

    def _compute_fit_scale(self) -> float:
        """What fitInView(KeepAspectRatio) would compute to fit the
        CURRENT box (+margin), not self.sceneRect() -- the scene rect
        is now fixed at the full canvas maximum (see load_glyph), so
        using it here would let both zoom bounds drift away from what
        the user is actually looking at: max zoom would compute a
        ceiling sized for the wrong (much larger) area, and, more
        visibly, zooming out via the wheel would have no reason to
        stop at the current box at all, continuing to reveal the
        mostly-empty space beyond it. Confirmed as a real, reported
        problem, not just a theoretical one."""
        if self.box_rect is None:
            return 1.0
        x0, y0, x1, y1 = self.box_rect
        m = self.margin_width or 0.0
        w = (x1 - x0) + 2 * m
        h = (y1 - y0) + 2 * m
        vp = self.viewport().rect()
        if w <= 0 or h <= 0:
            return 1.0
        return min(vp.width() / w, vp.height() / h)

    def _compute_max_zoom(self) -> float:
        """Canvas-size-aware zoom ceiling: a small canvas (this
        project's typical ~300x500) already shows plenty of detail at
        a modest zoom multiplier, but a much larger one (up to
        4096x4096) shrinks far more at fit-to-window, so the SAME
        multiplier reaches nowhere near as much actual on-screen
        detail. Targets a roughly constant on-screen scale at max zoom
        (screen pixels per scene unit) instead of a constant
        MULTIPLIER of fit -- 12 px/unit is what a fixed 10x already
        landed near for this project's typical size, so that size's
        max zoom is essentially unchanged, while a larger canvas (a
        smaller fit_scale) computes a proportionally higher one."""
        fit_scale = self._compute_fit_scale()
        if fit_scale <= 1e-9:
            return 10.0
        target_px_per_unit = 12.0
        return max(10.0, min(target_px_per_unit / fit_scale, 200.0))

    def _compute_zoom_step_factor(self) -> float:
        """The per-wheel-tick zoom multiplier, scaled so the full
        MIN_ZOOM..max_zoom range takes roughly the same NUMBER of wheel
        ticks to traverse regardless of canvas size -- a fixed 1.15x
        step felt fine for this project's typical ~15x range, but would
        take proportionally far more scrolling to reach a 4096x4096
        canvas's much larger max zoom (~100x). Solving for the step
        that covers the CURRENT range in a fixed target tick count
        keeps the feel roughly consistent across canvas sizes instead
        of the range just growing under a fixed step."""
        max_zoom = self._compute_max_zoom()
        if self.MIN_ZOOM <= 0 or max_zoom <= self.MIN_ZOOM:
            return 1.15
        target_ticks = 20
        return (max_zoom / self.MIN_ZOOM) ** (1.0 / target_ticks)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        step = self._compute_zoom_step_factor()
        factor = step if delta > 0 else 1.0 / step
        fit_scale = self._compute_fit_scale()
        if fit_scale <= 1e-9:
            event.accept()
            return
        # Computed FRESH from the actual current transform, not the
        # tracked _zoom_level variable -- that variable can go stale
        # relative to the CURRENT box whenever _user_zoomed skips a
        # resync's fitInView call (deliberately, to preserve the
        # user's manual zoom -- see resync_scene_to_box), so relying
        # on it for the min/max clamp could let zooming out continue
        # well past the current box into the mostly-empty scene beyond
        # it. Recomputing relative to _compute_fit_scale() (which
        # itself now targets the CURRENT box, not the fixed scene
        # rect) stays accurate regardless of that. Confirmed as a
        # real, reported problem, not just theoretical.
        current_relative = self.transform().m11() / fit_scale
        new_relative = max(self.MIN_ZOOM, min(current_relative * factor, self._compute_max_zoom()))
        applied = new_relative / current_relative
        if abs(applied - 1.0) > 1e-6:
            cursor_widget_pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            # Recorded from the event's OWN position, not relied on via
            # TransformationAnchor/ResizeAnchor's shared internal
            # AnchorUnderMouse mechanism -- both are still set (see
            # __init__) and still do real, useful work for scale()
            # itself, but confirmed as NOT sufficient on their own: this
            # same viewChanged emit below can trigger reflow(), which
            # resizes this widget again via a completely separate,
            # layout-driven resize event, and that second resize wasn't
            # reliably preserving the same cursor-anchored scene point
            # scale() had just established -- not a scroll-position
            # mismatch so much as the viewport's own pixel area growing
            # or shrinking around a fixed on-screen point, revealing (or
            # hiding) scene content at that point independent of any
            # scrollbar value. Explicitly re-deriving and re-centering
            # after everything above has already run corrects for both
            # causes at once, regardless of which one actually moved
            # anything on a given tick. Confirmed directly: right at
            # fit-scale, the scrollable range is itself tiny (there's
            # nowhere meaningful to scroll when the whole canvas already
            # fits), so a first zoom-in tick from exactly fit-scale can
            # still show a small residual offset -- but from any
            # meaningfully zoomed-in state, where the scrollable range
            # is genuinely large enough to act on, this closes the drift
            # to a small fraction of a scene unit.
            anchor_scene = self.mapToScene(cursor_widget_pos)
            self.scale(applied, applied)
            self._zoom_level = new_relative
            self._user_zoomed = True
            self.viewChanged.emit()
            drift = self.mapFromScene(anchor_scene) - cursor_widget_pos
            if drift.x() or drift.y():
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + drift.x())
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() + drift.y())
        event.accept()

    def fit_to_window(self):
        if self.box_rect is not None:
            x0, y0, x1, y1 = self.box_rect
            m = self.margin_width or 0.0
            self.fitInView(QRectF(x0 - m, y0 - m, (x1 - x0) + 2 * m, (y1 - y0) + 2 * m),
                            Qt.KeepAspectRatio)
        self._zoom_level = 1.0
        self._user_zoomed = False
        self.viewChanged.emit()

    # -- click-on-empty-space clears selection; ctrl-click on a segment
    #    toggles it into/out of a multi-selection ----------------------------

    def _active_plain_selected_segment(self) -> "SegmentItem | None":
        """The one segment whose own geometry gets unconditional
        priority on the next click -- exists only when the current
        selection is a genuine, single, plain-click-origin pick (the
        only case with any ACTIVE, draggable points at all -- see
        _refresh_selection_visuals). None for every other selection
        state (empty, multi-item CTRL/SHIFT selections, or anything
        not plain-click in origin), which is exactly when general,
        cross-segment resolution (and ambiguity-detection) applies."""
        if not self._last_action_was_plain_click:
            return None
        if len(self._individual_selected) != 1 or self._shift_selected:
            return None
        key = next(iter(self._individual_selected))
        for seg in self._segments:
            if (seg.path_index, seg.seg_index) == key:
                return seg
        return None

    def _hit_test_segment_own_geometry(self, seg: "SegmentItem", scene_pos: QPointF
                                        ) -> "DraggablePoint | SegmentItem | None":
        """Checks ONLY this one segment's own line and two endpoints --
        nothing else in the glyph is even looked at. This is what gives
        an already-selected segment's own geometry unconditional
        priority (see _segment_or_point_at), replacing the old
        distance-based tie-break that a real mouse's inherent few-pixel
        imprecision could -- and, confirmed via direct testing,
        regularly did -- lose to a merely-nearby, unrelated segment."""
        line = seg.line()
        p1 = seg.mapToScene(line.p1())
        p2 = seg.mapToScene(line.p2())
        best_pt, best_pt_dist = None, None
        for pt, ppos in ((seg.point_a, p1), (seg.point_b, p2)):
            dx = scene_pos.x() - ppos.x()
            dy = scene_pos.y() - ppos.y()
            d = (dx * dx + dy * dy) ** 0.5
            if d <= self.snap_point_tolerance and (best_pt_dist is None or d < best_pt_dist):
                best_pt, best_pt_dist = pt, d
        if best_pt is not None:
            return best_pt
        local_pos = seg.mapFromScene(scene_pos)
        if seg.shape().contains(local_pos):
            return seg
        return None

    def _segment_or_point_at(self, view_pos):
        """Two-step resolution, in order:

        STEP 1 -- if there's a single, genuinely plain-click-selected
        segment (see _active_plain_selected_segment), its own line and
        two endpoints are checked FIRST and UNCONDITIONALLY -- no
        distance comparison against anything else in the glyph. If the
        click lands on it at all, it wins, full stop. This is the
        actual mechanism behind the app's two-click disambiguation flow
        (select a segment, then click its endpoint) -- previously that
        flow only gave the selected segment's own point a distance-
        based ADVANTAGE (a forgiving hit radius, then later a tie-break
        epsilon), which could still lose to a sufficiently close,
        unrelated segment. Confirmed directly: ordinary mouse-click
        imprecision (a couple of real screen pixels, entirely normal)
        was enough to flip the outcome on roughly a third of attempts
        at a coincidence point -- an advantage can lose; an
        unconditional rule can't. Deliberately narrow in scope: this
        ONLY ever considers the one already-selected segment's own
        geometry, never reaching toward any other segment, which is
        what keeps this from reopening the EARLIER bug this design
        replaced (a global active-point search letting one segment's
        state steal a click clearly meant for a different, unrelated
        segment several units away).

        STEP 2 -- general fallback across every segment's own
        (weight-based) hit region, closest line wins. No point
        candidates here at all: the ONLY situation with any ACTIVE
        point in the whole scene is the single-plain-selection case
        step 1 already covers in full, so there is nothing left for
        step 2 to weigh a line against.

        Ambiguity between two or more DIFFERENT, genuinely competing
        candidates (nothing selected, or the click clearly misses
        whatever IS selected) is handled one level up, in
        mousePressEvent, via _find_ambiguous_candidates and the
        disambiguation popup -- not here. By the time this function is
        called for that case, any such ambiguity has already been
        resolved (a specific pick was made, or step 2's ordinary
        closest-line fallback is guaranteed unambiguous enough not to
        need it)."""
        scene_pos = self.mapToScene(view_pos)

        active_seg = self._active_plain_selected_segment()
        if active_seg is not None:
            hit = self._hit_test_segment_own_geometry(active_seg, scene_pos)
            if hit is not None:
                return hit

        best_item, best_dist = None, None
        for seg in self._segments:
            local_pos = seg.mapFromScene(scene_pos)
            if not seg.shape().contains(local_pos):
                continue
            line = seg.line()
            p1 = seg.mapToScene(line.p1())
            p2 = seg.mapToScene(line.p2())
            dist = _point_to_segment_distance(scene_pos, p1, p2)
            if best_dist is None or dist < best_dist:
                best_item, best_dist = seg, dist

        if best_item is not None:
            return best_item
        return self.itemAt(view_pos)

    def _find_ambiguous_candidates(self, scene_pos: QPointF) -> list["ClickCandidate"]:
        """Every plausible thing a fresh click at scene_pos could mean
        -- used ONLY when nothing is currently selected (or the click
        clearly misses whatever is), per _segment_or_point_at's own
        step 1/step 2 split: once something IS selected, its own
        geometry wins unconditionally with no ambiguity question left
        to ask.

        One candidate per SEGMENT, merged only across point-vs-line for
        that SAME segment (see ClickCandidate's own docstring for why
        that merge, specifically, is right) -- deliberately NOT
        deduplicated across different segments sharing a unit (polyline
        or group), even though picking any of them tints the same
        whole unit on hover. An earlier version did dedupe by unit, on
        the reasoning that an interior polyline vertex "isn't real
        ambiguity" since either pick resolves to the same unit -- but
        that missed that a pick still resolves to one SPECIFIC segment
        underneath (its own two endpoints are what become draggable
        next), and at a genuine multi-way convergence -- confirmed with
        a concrete repro: an asterisk regrouped into a single group,
        clicked where all 8 of its strokes meet -- collapsing them into
        one "candidate" meant there was never actually a choice to
        offer, and ordinary closest-match resolution picked one of the
        8 silently and arbitrarily. Which segment ends up closest at
        such a point can be genuinely difficult to guess correctly by
        eye, which is exactly the case this popup exists to hand back
        to the person instead of guessing.

        A segment qualifies if EITHER of its own endpoints is within
        point tolerance of the click, or its line is within the
        (tighter) line tolerance -- deliberately skeleton-based
        (weight-independent) for both, not SegmentItem.shape()'s
        rendered, weight-based hit region: ambiguity here is almost
        always about objects genuinely coincident or nearly so at the
        geometry level, and weight-based detection would flag pairs
        that are only visually close because of the current stroke
        weight, firing the popup far more often than the geometry
        actually warrants. Shares the exact tolerances used by
        ordinary click-to-select (see snap_point_tolerance,
        click_line_tolerance, and SegmentItem.shape()) rather than a
        separately-tuned pair, for the same reason those need to agree
        at all: a drifted-apart pair of "same idea, different number"
        constants is exactly the kind of bug this project keeps
        re-discovering."""
        candidates: list[ClickCandidate] = []
        for seg in self._segments:
            line = seg.line()
            p1 = seg.mapToScene(line.p1())
            p2 = seg.mapToScene(line.p2())
            pt_dist = min(
                math.hypot(scene_pos.x() - p1.x(), scene_pos.y() - p1.y()),
                math.hypot(scene_pos.x() - p2.x(), scene_pos.y() - p2.y()),
            )
            if pt_dist <= self.snap_point_tolerance:
                dist = pt_dist
            else:
                line_dist = _point_to_segment_distance(scene_pos, p1, p2)
                if line_dist > self.click_line_tolerance:
                    continue
                dist = line_dist
            candidates.append(ClickCandidate(seg, dist))

        return candidates

    def mousePressEvent(self, event):
        if self.read_only and event.button() == Qt.LeftButton:
            # Blocks selection, dragging, drawing, and paste-placement
            # clicks alike -- everything a left-click could otherwise
            # start here eventually leads to a mutation, so this is
            # blocked at the single point every one of those paths
            # already passes through, rather than needing a separate
            # check inside each one individually. Right-click panning
            # (below) is deliberately left untouched -- viewing an
            # excluded glyph, including panning/zooming around it,
            # still needs to work.
            event.accept()
            return
        if self._paste_mode:
            # Right-click cancels rather than panning -- overrides the
            # normal right-click-always-pans behavior below for
            # exactly this one moment, the same way draw_mode's own
            # left-click check overrides the normal click-to-select
            # handling further down. Confirmed as the wanted behavior
            # (not Escape-only): right-click is the natural "back out
            # of this" gesture already used for cancelling a capture
            # rectangle elsewhere in this app.
            if event.button() == Qt.RightButton:
                self.cancel_paste_mode()
                event.accept()
                return
            if event.button() == Qt.LeftButton:
                pos = self.mapToScene(event.pos())
                snapped = self.apply_snap(pos.x(), pos.y())
                # Same offset-based clamping the ghost preview itself
                # used (see _update_crosshair_at's own comment) -- a
                # plain clamp_to_source_bounds on the anchor alone would
                # let this commit a placement where the rest of the
                # pasted content, extending away from the anchor,
                # lands outside the canvas bounds even though the
                # anchor point itself didn't.
                if self._paste_anchor is not None and self._paste_clip_paths is not None:
                    ax, ay = self._paste_anchor
                    dx, dy = self._clamp_offset_to_box(
                        snapped[0] - ax, snapped[1] - ay,
                        [pt for path in self._paste_clip_paths for pt in path])
                    clamped = (ax + dx, ay + dy)
                else:
                    clamped = self.clamp_to_source_bounds(*snapped)
                self.pastePlaced.emit(*clamped)
                self._clear_paste_state()
                event.accept()
                return
        if event.button() == Qt.RightButton:
            self._panning = True
            self._pan_last_pos = event.pos()
            self._right_click_press_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if self.draw_mode and event.button() == Qt.LeftButton:
            self._handle_draw_click(event)
            event.accept()
            return
        # Ambiguity check -- only when the click would otherwise be a
        # genuinely fresh pick: SHIFT is never in scope here (a single
        # item is trivially removable again via CTRL-click, so the
        # popup adds nothing there), and if there's a currently, singly
        # plain-click-selected segment whose OWN geometry the click
        # actually lands on, _segment_or_point_at's own step 1 already
        # gives it unconditional priority -- nothing to disambiguate.
        # Only once that's ruled out is it worth the cost of scanning
        # for genuine multi-candidate ambiguity.
        if event.button() == Qt.LeftButton and not (event.modifiers() & Qt.ShiftModifier):
            scene_pos = self.mapToScene(event.pos())
            active_seg = self._active_plain_selected_segment()
            already_resolved = (
                active_seg is not None
                and self._hit_test_segment_own_geometry(active_seg, scene_pos) is not None
            )
            if not already_resolved:
                candidates = self._find_ambiguous_candidates(scene_pos)
                if len(candidates) >= 2:
                    popup = DisambiguationPopup(self, candidates, QCursor.pos())
                    popup.show()
                    event.accept()
                    return
        item = self._segment_or_point_at(event.pos())
        if isinstance(item, SegmentItem):
            if event.modifiers() & Qt.ShiftModifier:
                self._select_shift_click(item)
                # SHIFT always results in the clicked item being
                # selected -- arm a potential group-move the same way
                # a plain click does, so continuing to drag without
                # releasing moves the whole (now SHIFT-expanded)
                # selection rigidly. No endpoint sub-case: points in a
                # SHIFT-built selection are highlighted-but-locked, not
                # active (see _refresh_selection_visuals), so a press
                # on one falls through to this same segment-level path
                # rather than DraggablePoint's own handling.
                self._group_drag_item = item
                self._group_drag_press_pos = self.mapToScene(event.pos())
                self._group_drag_moved = False
                self._group_drag_defer_collapse = False
                event.accept()
                return
            if event.modifiers() & Qt.ControlModifier:
                self._select_ctrl_click(item)
                # Only arm a drag if the toggle left this item actually
                # selected -- toggling it OFF means there's nothing of
                # THIS item left to drag. Same "no endpoint sub-case"
                # reasoning as SHIFT above: CTRL-selected points are
                # never active, so this is always a rigid group-move,
                # never a single-point drag, even for a lone item.
                if item.isSelected():
                    self._group_drag_item = item
                    self._group_drag_press_pos = self.mapToScene(event.pos())
                    self._group_drag_moved = False
                    self._group_drag_defer_collapse = False
                event.accept()
                return
            if item.isSelected():
                # Already part of the current selection -- a plain click
                # on it would normally collapse the selection down to
                # just this one item immediately, on press. That's
                # exactly wrong here: if this turns into a drag, an
                # immediate collapse would destroy the rest of the group
                # before the drag even starts. Defer the collapse until
                # release, and only actually do it if this turns out to
                # be a plain click with no movement, not a group-offset
                # drag.
                self._group_drag_item = item
                self._group_drag_press_pos = self.mapToScene(event.pos())
                self._group_drag_moved = False
                self._group_drag_defer_collapse = True
                event.accept()
                return
            self._select_plain_click(item)
            # Re-resolve hit-testing AFTER selecting, not before --
            # confirmed as a real, reported bug otherwise: switching
            # directly from one selected segment to a DIFFERENT,
            # previously-unselected one always grabbed the whole
            # segment even when the click was precisely on one of its
            # endpoints, because _segment_or_point_at only considers
            # ACTIVE points (see its own docstring), and this segment's
            # own points don't become active until AFTER this same
            # click selects it -- a chicken-and-egg gap where the
            # original hit-test could never have found the point, no
            # matter how precisely it was clicked. Re-running it now
            # picks up the point that just became active as a result
            # of the selection above, so this correctly becomes a
            # point-drag instead of unconditionally arming a whole-
            # segment one.
            reresolved = self._segment_or_point_at(event.pos())
            if isinstance(reresolved, DraggablePoint):
                self._point_drag_item = reresolved
                self._point_drag_start = reresolved.pos()
                self._point_drag_escaped_start = False
                event.accept()
                return
            # Also track a potential drag for a FRESHLY-selected single
            # item -- select-and-drag in one gesture should work too,
            # moving just this one path as a "group of one".
            self._group_drag_item = item
            self._group_drag_press_pos = self.mapToScene(event.pos())
            self._group_drag_moved = False
            self._group_drag_defer_collapse = False
            event.accept()
            return
        if isinstance(item, DraggablePoint):
            # Deliberately reverted a single-click-activates attempt
            # here (see DraggablePoint's own docstring for the ORIGINAL
            # reasoning this preserves): at a junction where 3+
            # segments share one point, there's no way for a click on
            # the point ALONE to know which segment the user actually
            # means -- an attempted fix that auto-selected "whichever
            # owning segment is found first" was arbitrary from the
            # user's perspective and, confirmed in real use (not just
            # the 2-segment case this was tested against), still
            # produced the original whole-polyline-move symptom instead
            # of fixing it. Click-cycling through candidates at a
            # multi-way junction was also considered and rejected as
            # too cumbersome once more than two segments meet at a
            # point. The two-click flow -- select the specific segment
            # first, then click its endpoint -- remains the only
            # reliable way to know with certainty which segment is
            # meant, so it stays as the one true source of that
            # disambiguation rather than being partially bypassed.
            self._point_drag_item = item
            self._point_drag_start = item.pos()
            self._point_drag_escaped_start = False
            event.accept()
            return
        # Empty canvas -- either a plain click (clears selection, as
        # before) or the start of a rubber-band drag, decided once we
        # see whether the mouse actually moves past the threshold (see
        # mouseMoveEvent). Ctrl held means additive: don't clear what's
        # already selected, whether this turns into a plain click or a
        # drag. SHIFT takes priority over CTRL if somehow both are
        # held (matching the segment-click branch above) and is
        # tracked separately -- see _select_shift_drag_box for why
        # clearing here would be wrong for it: whether a SHIFT box-
        # select starts fresh or stays additive depends on whether the
        # key has been continuously held since the LAST one, which
        # that method itself resolves via _shift_hold_generation, not
        # on anything decided at press-time here.
        self._rubber_band_shift = bool(event.modifiers() & Qt.ShiftModifier)
        if not self._rubber_band_shift and not (event.modifiers() & Qt.ControlModifier):
            self.clear_all_selection()
        self._rubber_band_start = self.mapToScene(event.pos())
        self._rubber_band_additive = bool(event.modifiers() & Qt.ControlModifier)
        self._rubber_band_moved = False
        event.accept()

    def mouseDoubleClickEvent(self, event):
        # The first click of a double-click already went through
        # mousePressEvent above (Qt dispatches press/release, then a
        # SEPARATE double-click event for the second press, not a
        # second ordinary press) -- so the second click placed its OWN
        # point too, before Qt even recognized this as a double-click.
        # With snap on, both clicks round to the same grid position and
        # this is invisible; with snap off, natural hand tremor between
        # the two clicks leaves two adjacent, slightly-offset points.
        #
        # Only discard that second point when it's actually a tremor,
        # though -- not unconditionally. The right threshold for "close
        # enough to be the same point" is the dot marker's own radius
        # (scene-space, so it stays consistent with the dots as drawn
        # at any zoom level), not the stroke weight: weight changes
        # dynamically via the slider and segments shorter than it are
        # visually meaningless regardless of whether they were
        # intentional, so it's not actually a reliable signal for
        # "was this a tremor" one way or the other.
        if self.draw_mode and event.button() == Qt.LeftButton and self._drawing_points:
            if len(self._drawing_points) >= 2:
                (x1, y1), (x2, y2) = self._drawing_points[-2], self._drawing_points[-1]
                dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
                if dist < self.DRAW_DOT_RADIUS * 2:
                    self._drawing_points.pop()
            self._finish_drawing()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if self._paste_mode and event.key() == Qt.Key_Escape and not event.isAutoRepeat():
            # Consistent with every other modal-style placement in this
            # app (drawing, a capture rectangle) supporting Escape to
            # back out, even though right-click (see mousePressEvent)
            # is the one explicitly confirmed gesture for this.
            self.cancel_paste_mode()
            event.accept()
            return
        # G/S/H tap-toggle their matching checkbox; E alone still
        # requires HOLDING it (see below) -- checked regardless of draw
        # mode, since drawing is exactly when a quick snap change
        # without leaving the canvas to touch the Grid & Snap checkboxes
        # is most useful. autoRepeat() guards against Qt re-firing
        # keyPressEvent continuously while a key is held down (which it
        # does, on most platforms) -- without it, a tap would register
        # as many repeated toggles in a row (flipping back and forth) if
        # held even slightly too long, and E's own held-flag would be
        # redundantly re-set on every repeat tick (harmless, but not
        # what the check is for).
        if not event.isAutoRepeat():
            # G/S/H are now a tap-toggle -- flips the matching checkbox
            # in MainWindow (which is the actual, single source of
            # truth for these three; the canvas's own snap_to_grid/
            # snap_to_points/snap_to_xheight_baseline flags update
            # automatically through the SAME toggled-signal path the
            # checkbox's own click already uses, so there's nothing
            # else to do here beyond asking for the flip). Replaces an
            # earlier held-key override (_temp_snap_grid and friends,
            # now removed) that required awkwardly holding a key down
            # for the whole gesture -- see apply_snap's own comment for
            # why that duplicated the checkbox-driven priority order in
            # a second place that had to be kept in sync with it.
            #
            # E is the deliberate exception, and stays a HELD key: it
            # has no checkbox of its own to toggle (it's a temporary
            # modifier of "S", not an independent snap mode), so a
            # toggle would have nothing to visually represent its own
            # state -- see _temp_suspend_endpoint_snap's own comment.
            if event.key() == Qt.Key_G:
                self.snapToggleRequested.emit("grid")
                event.accept()
                return
            if event.key() == Qt.Key_S:
                self.snapToggleRequested.emit("points")
                event.accept()
                return
            if event.key() == Qt.Key_H:
                self.snapToggleRequested.emit("xheight_baseline")
                event.accept()
                return
            if event.key() == Qt.Key_E:
                self._temp_suspend_endpoint_snap = True
                event.accept()
                return
            if event.key() == Qt.Key_Shift:
                # A fresh press (key was NOT already down) starts a
                # new "hold session" -- see _shift_hold_generation's
                # own comment. Super() still runs after this (no
                # accept+return): Shift needs to keep behaving as an
                # ordinary modifier for everything else that reads
                # event.modifiers() elsewhere, this is purely
                # additional bookkeeping alongside that.
                self._shift_hold_generation += 1
        if self.draw_mode and self._drawing_points:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self._finish_drawing()
                event.accept()
                return
            if event.key() in (Qt.Key_Escape, Qt.Key_Delete):
                # Delete, while actively drawing, discards the WHOLE
                # in-progress path -- the same meaning Delete already
                # has outside drawing (removing a selected path
                # entirely), just applied to the path being built
                # rather than an already-committed one. Backspace below
                # is the finer-grained one-point-at-a-time undo.
                self._cancel_drawing()
                event.accept()
                return
            if event.key() == Qt.Key_Backspace:
                # Removes the LAST placed point, one click at a time --
                # distinct from Delete/Escape, which both act on the
                # whole path (a selected path's segments, or the whole
                # in-progress path respectively). Undoing one click
                # while actively drawing is a different, more common
                # need than discarding everything just drawn.
                self._drawing_points.pop()
                self._rebuild_drawing_preview()
                if self._drawing_points:
                    last = self._drawing_points[-1]
                    if self._rubber_band_line is not None:
                        self._rubber_band_line.setLine(last[0], last[1], last[0], last[1])
                elif self._rubber_band_line is not None:
                    self.scene_.removeItem(self._rubber_band_line)
                    self._rubber_band_line = None
                event.accept()
                return
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.selected_segments and not self.read_only:
            self.deleteRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if not event.isAutoRepeat():
            if event.key() == Qt.Key_E:
                self._temp_suspend_endpoint_snap = False
                event.accept()
                return
        super().keyReleaseEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton and self._panning:
            self._panning = False
            self._pan_last_pos = None
            # Back to whatever's appropriate now that panning's
            # override is gone -- blank if still over the canvas
            # (the common case), otherwise the normal draw-mode/
            # arrow cursor, rather than always reverting to arrow
            # and discarding either of those.
            self.setCursor(self._resting_cursor())
            # Genuine pan (moved beyond the threshold) vs. a plain
            # right-click that never went anywhere -- same movement-
            # threshold pattern GROUP_DRAG_THRESHOLD already uses for
            # left-click group drags. A plain right-click auto-selects
            # whatever segment it landed on (if it isn't already part
            # of the selection -- standard desktop convention, and
            # matches how a left-click already picks a fresh segment),
            # then asks MainWindow to show a Cut/Copy/Paste/Delete menu
            # for it -- MainWindow owns those actions and the clipboard
            # already, so it builds the menu itself rather than this
            # view reaching back into that state.
            moved = (self._right_click_press_pos is not None
                     and (event.pos() - self._right_click_press_pos).manhattanLength() > GROUP_DRAG_THRESHOLD)
            self._right_click_press_pos = None
            if not moved and not self.read_only:
                # Confirmed as a real, reported bug when this wasn't
                # gated at all: a plain right-click on an excluded
                # glyph's own segment/polyline/group still selected it
                # (highlighting it) and still opened the Cut/Copy/
                # Paste/Delete menu, with Delete fully usable from
                # there -- exactly the mutation this whole read-only
                # lock exists to prevent, just reached through a path
                # the original lock never covered (see mousePressEvent's
                # own read_only check above, which only ever gated
                # LEFT-click). Right-click panning itself (this whole
                # method's own surrounding block) stays untouched either
                # way -- viewing an excluded glyph, including panning
                # around it, still needs to work; only the "landed
                # without moving, so treat it as a selection+menu
                # request" case needs to stay inert here.
                item = self._segment_or_point_at(event.pos())
                if isinstance(item, SegmentItem) and not item.isSelected():
                    self._select_plain_click(item)
                self.segmentContextMenuRequested.emit(event.globalPosition().toPoint())
            event.accept()
            return
        if self._point_drag_item is not None:
            pt = self._point_drag_item
            if self._point_drag_start is not None and self._point_drag_start != pt.pos():
                self.on_point_committed(pt.path_index, pt.point_index, pt.pos().x(), pt.pos().y())
            self._point_drag_item = None
            self._point_drag_start = None
            self._point_drag_escaped_start = False
            event.accept()
            return
        if self._group_drag_item is not None:
            item = self._group_drag_item
            if self._group_drag_moved:
                self.end_group_offset_drag()
            elif self._group_drag_defer_collapse and len(self.selected_segments) > 1:
                # A plain click, with no drag, on a segment that was
                # already part of a larger selection -- NOW collapse to
                # just this one, the behavior a plain click would
                # normally have had immediately, deferred until it's
                # clear no drag was actually intended. Not needed for a
                # freshly-selected single item: pressing already made it
                # the sole selection at press time. Gated on
                # _group_drag_defer_collapse specifically, not just
                # "was something armed" -- CTRL-click and SHIFT-click
                # also arm this same tracking (so a continued drag can
                # move the result), but a plain release right after
                # THOSE must leave the just-built selection alone, not
                # collapse it back down to the one clicked item.
                self._select_plain_click(item)
            self._group_drag_item = None
            self._group_drag_press_pos = None
            self._group_drag_moved = False
            self._group_drag_defer_collapse = False
            event.accept()
            return
        if self._rubber_band_start is not None:
            if self._rubber_band_moved and self._rubber_band_rect_item is not None:
                if self._rubber_band_shift:
                    self._select_shift_drag_box(self._rubber_band_rect_item.rect())
                else:
                    self._select_drag_box(self._rubber_band_rect_item.rect(),
                                                   additive=self._rubber_band_additive)
            if self._rubber_band_rect_item is not None:
                self.scene_.removeItem(self._rubber_band_rect_item)
                self._rubber_band_rect_item = None
            self._rubber_band_start = None
            self._rubber_band_moved = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

