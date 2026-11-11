"""
Renders a glyph's actual traced/edited outline to a small QPixmap --
used so the glyph browser shows the real shape (including any edits)
rather than falling back to however the system UI font happens to draw
that character, which can differ significantly for symbols, box-drawing
characters, or anything that's been hand-edited.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPixmap, QPainter, QPainterPath, QColor, QBrush, QPen

from core import geometry
from . import style


def render_glyph_pixmap(paths, half_width: float, cap_style: str,
                         size: int = 40,
                         box: tuple[float, float, float, float] | None = None,
                         bg: QColor | None = None) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(bg or QColor(style.CANVAS_BG))

    geom = geometry.thicken(paths, half_width, cap_style=cap_style) if paths else None

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    if geom is not None and not geom.is_empty:
        if box:
            x0, y0, x1, y1 = box
        else:
            all_x = [x for p in paths for x, _y in p]
            all_y = [y for p in paths for _x, y in p]
            x0, x1 = min(all_x), max(all_x)
            y0, y1 = min(all_y), max(all_y)
        w, h = max(x1 - x0, 1), max(y1 - y0, 1)
        pad = 0.12
        scale = min(size * (1 - 2 * pad) / w, size * (1 - 2 * pad) / h)
        painter.translate(size / 2, size / 2)
        painter.scale(scale, scale)
        painter.translate(-(x0 + x1) / 2, -(y0 + y1) / 2)

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
        painter.setBrush(QBrush(QColor(style.INK_COLOR)))
        painter.setPen(QPen(Qt.NoPen))
        painter.drawPath(qpath)

    painter.end()
    return pixmap
