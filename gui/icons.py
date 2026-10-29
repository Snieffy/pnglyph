"""
Custom toolbar icons, drawn programmatically rather than relying on Qt's
built-in QStyle::StandardPixmap set -- there's no standard "undo" (as
opposed to a plain back-arrow, which reads as navigation, not history),
"break a path", "join two paths", or "select all" icon available, and
generic X/checkmark icons don't convey what Break/Join actually do.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPen, QColor, QPixmap, QTransform

_SIZE = 22
_INK = QColor("#333333")


def _new_painter(pix: QPixmap) -> QPainter:
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    return p


def _icon_from(draw_fn) -> QIcon:
    pix = QPixmap(_SIZE, _SIZE)
    p = _new_painter(pix)
    draw_fn(p)
    p.end()
    return QIcon(pix)


def _pen(width: float, cap=Qt.RoundCap) -> QPen:
    pen = QPen(_INK, width)
    pen.setCapStyle(cap)
    return pen


def _curved_arrow(clockwise: bool) -> QIcon:
    """A history undo/redo arrow. Redo is built directly; undo is a
    horizontal mirror of it -- an independent set of angles/tangent
    math for the counter-clockwise case produced a nearly-closed loop
    with no visible arrowhead the first time, so mirroring the version
    that's already confirmed to look right is more reliable than
    re-deriving separate geometry for each direction."""
    cx, cy, r = 11.0, 12.0, 6.5
    start_deg, end_deg = 40, -220   # ~260 degree clockwise sweep
    sweep = end_deg - start_deg

    def draw(p: QPainter):
        p.setPen(_pen(2.3))
        rect = QRectF(cx - r, cy - r, r * 2, r * 2)
        p.drawArc(rect, int(start_deg * 16), int(sweep * 16))

        end_angle_rad = math.radians(start_deg + sweep)
        ex = cx + r * math.cos(end_angle_rad)
        ey = cy - r * math.sin(end_angle_rad)
        tangent_rad = end_angle_rad - math.pi / 2
        tx, ty = math.cos(tangent_rad), -math.sin(tangent_rad)

        head_len, head_w = 5.0, 4.2
        tip = QPointF(ex + tx * head_len * 0.3, ey + ty * head_len * 0.3)
        back = QPointF(ex - tx * head_len * 0.7, ey - ty * head_len * 0.7)
        nx, ny = -ty, tx
        left = QPointF(back.x() + nx * head_w / 2, back.y() + ny * head_w / 2)
        right = QPointF(back.x() - nx * head_w / 2, back.y() - ny * head_w / 2)

        head = QPainterPath()
        head.moveTo(tip)
        head.lineTo(left)
        head.lineTo(right)
        head.closeSubpath()
        p.setBrush(_INK)
        p.setPen(Qt.NoPen)
        p.drawPath(head)

    icon = _icon_from(draw)
    if not clockwise:
        pix = icon.pixmap(_SIZE, _SIZE).transformed(QTransform().scale(-1, 1))
        icon = QIcon(pix)
    return icon


def _break_icon() -> QIcon:
    def draw(p: QPainter):
        # two clearly separated segments with a visible gap and rounded
        # ends -- reads as "this connection is cut here" without extra
        # decoration that (as tried first) can start looking like a
        # lightning bolt instead of a clean break
        p.setPen(_pen(2.6))
        p.drawLine(QPointF(3.5, 11), QPointF(9.0, 11))
        p.drawLine(QPointF(13.0, 11), QPointF(18.5, 11))
    return _icon_from(draw)


def _join_icon() -> QIcon:
    def draw(p: QPainter):
        # two segments running into a solid connecting link -- the
        # mirror image of Break's gap
        p.setPen(_pen(2.4))
        p.drawLine(QPointF(3.5, 11), QPointF(8.0, 11))
        p.drawLine(QPointF(14.0, 11), QPointF(18.5, 11))
        p.drawLine(QPointF(8.0, 11), QPointF(14.0, 11))
        p.setBrush(_INK)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(11, 11), 2.6, 2.6)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(11, 11), 1.1, 1.1)
    return _icon_from(draw)


def _select_all_icon() -> QIcon:
    def draw(p: QPainter):
        # four corner brackets, like a camera/selection viewfinder --
        # a common, unambiguous "select everything in view" symbol
        p.setPen(_pen(2.0, cap=Qt.SquareCap))
        L, arm = 3, 5
        R = _SIZE - 3
        corners = [
            [(L, L + arm), (L, L), (L + arm, L)],
            [(R - arm, L), (R, L), (R, L + arm)],
            [(L, R - arm), (L, R), (L + arm, R)],
            [(R - arm, R), (R, R), (R, R - arm)],
        ]
        for pts in corners:
            path = QPainterPath()
            path.moveTo(*pts[0])
            path.lineTo(*pts[1])
            path.lineTo(*pts[2])
            p.drawPath(path)
    return _icon_from(draw)


def _import_icon() -> QIcon:
    def draw(p: QPainter):
        # a classic folder silhouette (tab + body) in a warm tone, so it
        # reads as "folder" at a glance rather than a generic outline,
        # plus a downward arrow INTO it -- "bring this folder's
        # contents into the project", distinct from the plain open-
        # folder glyph used before, which didn't convey the action
        folder = QPainterPath()
        folder.moveTo(2, 8)
        folder.lineTo(2, 5)
        folder.lineTo(8, 5)
        folder.lineTo(10, 7)
        folder.lineTo(20, 7)
        folder.lineTo(20, 18)
        folder.lineTo(2, 18)
        folder.closeSubpath()
        p.setBrush(QColor("#e3a939"))
        p.setPen(_pen(1.2))
        p.drawPath(folder)

        white_pen = _pen(2.0)
        white_pen.setColor(QColor("#ffffff"))
        p.setPen(white_pen)
        p.drawLine(QPointF(11, 9), QPointF(11, 14))

        head = QPainterPath()
        head.moveTo(QPointF(7.5, 12.5))
        head.lineTo(QPointF(11, 16.5))
        head.lineTo(QPointF(14.5, 12.5))
        head.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawPath(head)
    return _icon_from(draw)


def _export_icon() -> QIcon:
    def draw(p: QPainter):
        # "TTF" as the primary label -- the specific output FORMAT is
        # the one thing an icon alone can't convey -- plus a small
        # corner arrow escaping up-and-out, the same motif used for
        # "opens externally"/"exports out" elsewhere (browser new-tab
        # icons, etc.), picked specifically to avoid reading as
        # "upload" (a plain up arrow) or "download" (a plain down one)
        font = p.font()
        font.setPixelSize(10)
        font.setBold(True)
        p.setFont(font)
        p.setPen(_INK)
        p.drawText(QRectF(0, 7, 16, 14), Qt.AlignCenter, "TTF")

        p.setPen(_pen(1.8))
        p.drawLine(QPointF(15, 7), QPointF(19.5, 2.5))
        head = QPainterPath()
        head.moveTo(QPointF(15, 2.5))
        head.lineTo(QPointF(19.5, 2.5))
        head.lineTo(QPointF(19.5, 7))
        p.setBrush(Qt.NoBrush)
        p.drawPath(head)
    return _icon_from(draw)


def _angle_snap_icon() -> QIcon:
    def draw(p: QPainter):
        # a fan of rays from a shared vertex at fixed angular steps --
        # reads clearly as "constrained angle steps", unlike a single
        # angle+arc motif which rendered more like a diagonal slash
        # than a protractor at this size
        vertex = QPointF(3, 19)
        p.setPen(_pen(1.6))
        for deg in (0, 22.5, 45, 67.5, 90):
            rad = math.radians(deg)
            length = 15
            end = QPointF(vertex.x() + length * math.cos(rad),
                           vertex.y() - length * math.sin(rad))
            p.drawLine(vertex, end)
        p.setBrush(_INK)
        p.setPen(Qt.NoPen)
        p.drawEllipse(vertex, 1.6, 1.6)
    return _icon_from(draw)


def _fit_window_icon() -> QIcon:
    def draw(p: QPainter):
        # arrows pointing INWARD toward the center, from each corner --
        # the inverse of select_all's outward-facing corner brackets,
        # so the two read as clearly opposite actions at a glance
        p.setPen(_pen(1.8))
        arrows = [
            ((3, 3), (8, 8)),
            ((19, 3), (14, 8)),
            ((3, 19), (8, 14)),
            ((19, 19), (14, 14)),
        ]
        for (sx, sy), (ex, ey) in arrows:
            p.drawLine(QPointF(sx, sy), QPointF(ex, ey))
        p.setBrush(_INK)
        p.setPen(Qt.NoPen)
        for _s, (ex, ey) in arrows:
            p.drawEllipse(QPointF(ex, ey), 1.3, 1.3)
    return _icon_from(draw)


def _settings_icon() -> QIcon:
    def draw(p: QPainter):
        # a simple gear: a ring with evenly-spaced teeth, the
        # near-universal "settings" symbol
        cx, cy, r_in, r_out = 11, 11, 5.0, 7.5
        teeth = 8
        path = QPainterPath()
        for i in range(teeth * 2):
            angle = math.pi * i / teeth
            r = r_out if i % 2 == 0 else r_in
            x, y = cx + r * math.cos(angle), cy + r * math.sin(angle)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        path.closeSubpath()
        p.setBrush(_INK)
        p.setPen(Qt.NoPen)
        p.drawPath(path)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(cx, cy), 2.6, 2.6)
    return _icon_from(draw)


def _pen_icon() -> QIcon:
    def draw(p: QPainter):
        # a simple pen nib: a slanted body + a pointed tip, the
        # standard "draw" motif, distinct from the angle-snap fan and
        # everything else already in the toolbar
        body = QPainterPath()
        body.moveTo(QPointF(4, 18))
        body.lineTo(QPointF(6, 12))
        body.lineTo(QPointF(15, 3))
        body.lineTo(QPointF(19, 7))
        body.lineTo(QPointF(10, 16))
        body.lineTo(QPointF(4, 18))
        body.closeSubpath()
        p.setBrush(_INK)
        p.setPen(_pen(1.2))
        p.drawPath(body)
        # tip highlight
        p.setBrush(QColor("#ffffff"))
        p.setPen(Qt.NoPen)
        tip = QPainterPath()
        tip.moveTo(QPointF(4, 18))
        tip.lineTo(QPointF(6, 12))
        tip.lineTo(QPointF(8, 14))
        tip.closeSubpath()
        p.drawPath(tip)
    return _icon_from(draw)


def _cut_icon() -> QIcon:
    def draw(p: QPainter):
        # scissors: two blades crossing at a shared pivot, each
        # running from one of the two handle loops to the tip on the
        # OPPOSITE side -- what actually makes it read as scissors
        # rather than a plain X is that crossing, not just two circles.
        # Handle loops use a thinner pen than the blades -- at this
        # small a radius, the blades' own pen width nearly filled them
        # solid instead of reading as a ring.
        p.setPen(_pen(2.0))
        p.drawLine(QPointF(6, 6), QPointF(19, 15))
        p.drawLine(QPointF(6, 16), QPointF(19, 7))
        p.setPen(_pen(1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(4, 5), 2.8, 2.8)
        p.drawEllipse(QPointF(4, 17), 2.8, 2.8)
    return _icon_from(draw)


def _copy_icon() -> QIcon:
    def draw(p: QPainter):
        # two overlapping sheets -- the back one drawn first and
        # plain-stroked, the front one filled white before its own
        # stroke so it visibly occludes the overlap, reading as one
        # sheet sitting ON TOP of another rather than two outlines
        # merely touching
        p.setPen(_pen(1.8))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(7, 3, 11, 13), 1.5, 1.5)
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(QRectF(4, 7, 11, 13), 1.5, 1.5)
    return _icon_from(draw)


def _paste_icon() -> QIcon:
    def draw(p: QPainter):
        # a clipboard: outer board, a small solid clip tab at the top,
        # and a few short lines standing in for pasted content
        p.setPen(_pen(1.8))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(5, 4, 12, 15), 1.5, 1.5)
        p.setBrush(_INK)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(9, 2, 4, 3), 1, 1)
        p.setPen(_pen(1.3))
        p.drawLine(QPointF(7.5, 10), QPointF(14.5, 10))
        p.drawLine(QPointF(7.5, 13), QPointF(14.5, 13))
        p.drawLine(QPointF(7.5, 16), QPointF(12, 16))
    return _icon_from(draw)


def _capture_icon() -> QIcon:
    def draw(p: QPainter):
        # Viewfinder-style corner brackets (the universal "capture a
        # region" motif) around a small filled dot -- reads as "grab a
        # rectangle" distinctly from Add Glyph's plain file icon.
        pen = _pen(2.2)
        p.setPen(pen)
        arm = 5
        corners = [(2, 2, 1, 1), (20, 2, -1, 1), (2, 20, 1, -1), (20, 20, -1, -1)]
        for cx, cy, dx, dy in corners:
            p.drawLine(QPointF(cx, cy), QPointF(cx + arm * dx, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy + arm * dy))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e0662f"))
        p.drawEllipse(QPointF(11, 11), 3.2, 3.2)
    return _icon_from(draw)


def _clear_icon() -> QIcon:
    def draw(p: QPainter):
        # An empty dashed box (the "workspace", now empty) with a small
        # X through it -- reads as "wipe/reset" distinctly from Undo's
        # curved arrow, which implies stepping back rather than
        # discarding everything at once.
        pen = _pen(2.0)
        pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(3, 3, 16, 16), 2, 2)
        solid_pen = _pen(2.2)
        solid_pen.setStyle(Qt.SolidLine)
        p.setPen(solid_pen)
        p.drawLine(QPointF(7, 7), QPointF(15, 15))
        p.drawLine(QPointF(15, 7), QPointF(7, 15))
    return _icon_from(draw)


def get_icons() -> dict[str, QIcon]:
    return {
        "undo": _curved_arrow(clockwise=False),
        "redo": _curved_arrow(clockwise=True),
        "break": _break_icon(),
        "join": _join_icon(),
        "select_all": _select_all_icon(),
        "import": _import_icon(),
        "export": _export_icon(),
        "angle_snap": _angle_snap_icon(),
        "fit_window": _fit_window_icon(),
        "pen": _pen_icon(),
        "settings": _settings_icon(),
        "cut": _cut_icon(),
        "copy": _copy_icon(),
        "paste": _paste_icon(),
        "capture": _capture_icon(),
        "clear": _clear_icon(),
    }
