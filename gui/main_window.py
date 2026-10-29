from __future__ import annotations

import os
import copy
import math
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QRectF, QPointF, QPoint, QTimer, Signal, QEvent
from PySide6.QtGui import QFont, QAction, QColor, QBrush, QKeySequence, QPixmap, QPainter, QPen, QFontDatabase, QIcon, QImage, QFontMetrics, QCursor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QListWidget, QListWidgetItem, QSplitter, QSplitterHandle, QVBoxLayout,
    QHBoxLayout, QGridLayout, QLabel, QSlider, QSpinBox, QComboBox, QLineEdit, QPushButton, QFileDialog,
    QToolBar, QStatusBar, QMessageBox, QGroupBox, QFormLayout, QCheckBox,
    QAbstractItemView,
    QDoubleSpinBox, QProgressDialog, QApplication, QInputDialog, QDialog, QDialogButtonBox, QMenu,
    QSizePolicy, QFrame, QScrollArea,
)
from dataclasses import dataclass as _dataclass

from core import tracing, calibration, geometry
from core.project import Project, FontSettings, Metrics, EditStep, GlyphRecipe, auto_vertical_metrics
from .canvas_view import GlyphCanvas, paste_anchor_point
from .render_utils import render_glyph_pixmap
from .icons import get_icons
from .export_dialog import ExportDialog
from .help_dialog import HelpDialog
from . import style
from .capture.codepoint_parse import parse_codepoint as _parse_capture_codepoint
from .codepoint_entry import CodepointLineEdit
from .capture.session_controller import CaptureSessionController
from .capture.capture_window import _CaptureRow
from .capture import settings as capture_settings
from .capture import instance_lock as capture_instance_lock
from .capture.manifest import CaptureSessionStore
from . import app_settings


def _nice_step(range_size: float, target_ticks: int = 6) -> float:
    """A standard axis-tick algorithm: picks a round number (1/2/5 times
    a power of ten) close to range_size/target_ticks, so tick labels
    read as 50, 100, 200 rather than arbitrary fractional values."""
    if range_size <= 0:
        return 1.0
    rough_step = range_size / target_ticks
    magnitude = 10 ** math.floor(math.log10(rough_step))
    residual = rough_step / magnitude
    if residual < 1.5:
        nice = 1
    elif residual < 3:
        nice = 2
    elif residual < 7:
        nice = 5
    else:
        nice = 10
    return nice * magnitude


class Ruler(QWidget):
    """Thin horizontal ruler above the canvas. Ticks are at round-number
    intervals relative to the box's own left edge (x=0), rather than
    arbitrary fractions of the padded/visible range -- and the box's
    own right edge (the advance width) is always explicitly marked,
    even when it doesn't land on a round tick, so the actual glyph-
    space extent is never just implied."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(20)
        self.view_left: float | None = None
        self.view_right: float | None = None
        self.box_left: float | None = None
        self.box_right: float | None = None
        self.average_advance: float | None = None
        self.canvas_max: float | None = None

    def set_values(self, view_left: float | None, view_right: float | None,
                    box_left: float | None, box_right: float | None,
                    average_advance: float | None = None, canvas_max: float | None = None):
        self.view_left = view_left
        self.view_right = view_right
        self.box_left = box_left
        self.box_right = box_right
        self.average_advance = average_advance
        # The detected/source canvas width, shown on the bold max label
        # instead of box_right - box_left -- those happen to be the
        # same number here (box_left is always 0 by construction), but
        # relying on that coincidence is what let the vertical ruler's
        # equivalent label drift from the real canvas height (box_top
        # is NOT 0, so its own box_bottom - box_top produces a
        # different, smaller number -- confirmed: 505 shown vs the
        # real, draggable-to canvas height of 509). Explicit is more
        # robust for this one too, not just correct for the other.
        self.canvas_max = canvas_max if canvas_max is not None else box_right
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(style.CANVAS_BG))
        w = self.width()

        if self.box_left is None or self.box_right is None:
            # no detected box for this glyph yet -- fall back to a
            # simple reference line at the project's average advance,
            # if one is known, rather than round-number ticks that
            # would have nothing real to be relative to
            p.setPen(QPen(QColor("#999999")))
            if not self.average_advance:
                p.drawText(self.rect(), Qt.AlignCenter, "no advance width set")
                p.end()
                return
            for frac in (0, 0.25, 0.5, 0.75, 1.0):
                x = int(frac * w)
                p.drawLine(x, self.height() - 6, x, self.height())
                p.drawText(x + 2, self.height() - 6, f"{frac * self.average_advance:.0f}")
            p.end()
            return

        if self.view_left is None or self.view_right is None or self.view_right <= self.view_left:
            p.end()
            return
        view_span = self.view_right - self.view_left

        def x_to_widget(x):
            return (x - self.view_left) / view_span * w

        # Pixel-based, not value-based: two labels a few raw units
        # apart can still be many pixels apart on screen at a tight
        # zoom, and conversely two labels far apart in value can be
        # right on top of each other on screen at a loose zoom -- only
        # actual on-screen distance means anything for whether text
        # will visually collide. Confirmed as a real, reported bug: a
        # value-only threshold let bold and regular labels overlap
        # whenever they happened to be visually close despite not
        # being "the same" by raw value. Uses a representative digit
        # count's width as the horizontal label footprint (text width
        # varies per label, unlike the vertical ruler's fixed line
        # height, so this is an estimate, not exact -- generous on
        # purpose to avoid under-spacing).
        label_w = p.fontMetrics().horizontalAdvance("0000") + 6

        # Bold marker positions computed FIRST, before any drawing --
        # both so the two can be checked against each other, and so
        # the regular round-number ticks below know where to yield.
        show_canvas_max = (self.canvas_max is not None
                            and self.view_left <= self.canvas_max <= self.view_right)
        wx_end = x_to_widget(self.canvas_max) if show_canvas_max else None
        show_box_right = (self.box_right is not None
                           and self.view_left <= self.box_right <= self.view_right)
        wx_box = x_to_widget(self.box_right) if show_box_right else None
        # The guide's own live position wins a collision with the
        # static canvas-edge marker -- it's the more useful, actively-
        # changing information moment to moment, and a viewer can
        # already tell the box is at (or very near) the canvas edge
        # from the guide-strip pill sitting right there.
        if show_canvas_max and show_box_right and abs(wx_end - wx_box) < label_w:
            show_canvas_max = False
        bold_positions: list[float] = []
        if show_canvas_max:
            bold_positions.append(wx_end)
        if show_box_right:
            bold_positions.append(wx_box)

        p.setPen(QPen(QColor("#999999")))
        step = _nice_step(self.box_right - self.box_left)
        # never show a regular tick beyond the box's own actual range
        # in the first place (the margin isn't "size" that should get
        # its own ticks)
        k_start = math.floor((self.view_left - self.box_left) / step)
        k_end = math.ceil((self.view_right - self.box_left) / step)
        for k in range(k_start, k_end + 1):
            raw_x = self.box_left + k * step
            if raw_x < self.box_left or raw_x > self.box_right:
                continue
            if not (self.view_left <= raw_x <= self.view_right):
                continue
            wx = x_to_widget(raw_x)
            # Yield to a bold marker rather than overlap it -- a
            # regular round-number tick landing right on top of the
            # canvas-edge or guide-position marker is exactly the
            # "smaller user-area, everything crowds together" overlap
            # that was reported.
            if any(abs(wx - bp) < label_w for bp in bold_positions):
                continue
            p.drawLine(int(wx), self.height() - 6, int(wx), self.height())
            p.drawText(int(wx) + 2, self.height() - 6, f"{k * step:.0f}")

        # The TRUE canvas edge (not the box's own draggable edge --
        # e.g. the advance guide, which can sit anywhere and often
        # doesn't match the canvas boundary at all) -- explicitly
        # marked, bold/accented so it reads as "the outer extent",
        # distinct from the regular round-number ticks. Position and
        # label both come from canvas_max; the whole block is skipped
        # (show_canvas_max already False) when it would overlap the
        # guide's own position marker below.
        if show_canvas_max:
            font = p.font()
            font.setBold(True)
            p.setFont(font)
            p.setPen(QPen(QColor(style.GUIDE_COLOR)))
            p.drawLine(int(wx_end), self.height() - 6, int(wx_end), self.height())
            text = f"{self.canvas_max:.0f}"
            tw = p.fontMetrics().horizontalAdvance(text)
            tx = int(wx_end) + 2
            if tx + tw > self.width():
                tx = max(int(wx_end) - tw - 2, 0)
            p.drawText(tx, self.height() - 6, text)

        # The advance guide's OWN current position, separately --
        # dropped entirely once the canvas-edge marker above was fixed
        # to stay at the true canvas edge, since advance usually ISN'T
        # there (that's the whole point of it being a draggable guide).
        # Without this there was no numeric readout of advance's actual
        # value anywhere on the ruler at all.
        if show_box_right:
            font = p.font()
            font.setBold(True)
            p.setFont(font)
            p.setPen(QPen(QColor(style.GUIDE_COLOR)))
            p.drawLine(int(wx_box), self.height() - 6, int(wx_box), self.height())
            text = f"{self.box_right:.0f}"
            tw = p.fontMetrics().horizontalAdvance(text)
            tx2 = int(wx_box) + 2
            if tx2 + tw > self.width():
                tx2 = max(int(wx_box) - tw - 2, 0)
            p.drawText(tx2, self.height() - 6, text)
        p.end()


class VerticalRuler(QWidget):
    """Thin vertical ruler along the left edge of the canvas -- the same
    box-relative, round-number design as Ruler, just on the Y axis:
    origin at the box's own top edge, and the box's bottom edge always
    explicitly marked with its actual value."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(38)
        self.view_top: float | None = None
        self.view_bottom: float | None = None
        self.box_top: float | None = None
        self.box_bottom: float | None = None
        self.canvas_max: float | None = None

    def set_values(self, view_top: float | None, view_bottom: float | None,
                    box_top: float | None = None, box_bottom: float | None = None,
                    canvas_max: float | None = None):
        self.view_top = view_top
        self.view_bottom = view_bottom
        self.box_top = box_top if box_top is not None else view_top
        self.box_bottom = box_bottom if box_bottom is not None else view_bottom
        # The detected/source canvas height for the bold max label --
        # NOT box_bottom - box_top, which is relative to box_top (e.g.
        # cap_top_px), not to the true canvas origin. Confirmed as a
        # real, visible mismatch: the real dataset's descender_bottom_px
        # (506) minus cap_top_px (1) is 505, which is what used to show
        # here -- but the canvas itself, and the actual draggable-to
        # limit, is 509.
        self.canvas_max = canvas_max if canvas_max is not None else (
            (self.box_bottom - self.box_top) if self.box_bottom is not None and self.box_top is not None else None)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(style.CANVAS_BG))
        h = self.height()
        if None in (self.view_top, self.view_bottom, self.box_top, self.box_bottom):
            p.end()
            return
        view_span = self.view_bottom - self.view_top
        if view_span <= 0:
            p.end()
            return

        def y_to_widget(y):
            return (y - self.view_top) / view_span * h

        # Pixel-based, not value-based: two labels a few raw units
        # apart can still be many pixels apart on screen at a tight
        # zoom, and conversely two labels far apart in value can be
        # right on top of each other on screen at a loose zoom -- only
        # actual on-screen distance means anything for whether text
        # will visually collide. Confirmed as a real, reported bug: a
        # value-only threshold let bold and regular labels overlap
        # whenever they happened to be visually close despite not
        # being "the same" by raw value.
        label_h = p.fontMetrics().ascent() + 4

        # Bold marker positions computed FIRST, before any drawing --
        # both so the two can be checked against each other, and so
        # the regular round-number ticks below know where to yield.
        bold_positions: list[float] = []
        show_canvas_max = (self.canvas_max is not None
                            and self.view_top <= self.canvas_max <= self.view_bottom)
        wy_end = y_to_widget(self.canvas_max) if show_canvas_max else None
        show_box_bottom = (self.box_bottom is not None
                            and self.view_top <= self.box_bottom <= self.view_bottom)
        wy_box = y_to_widget(self.box_bottom) if show_box_bottom else None
        # The guide's own live position wins a collision with the
        # static canvas-edge marker -- it's the more useful, actively-
        # changing information moment to moment, and a viewer can
        # already tell the box is at (or very near) the canvas edge
        # from the guide-strip pill sitting right there.
        if show_canvas_max and show_box_bottom and abs(wy_end - wy_box) < label_h:
            show_canvas_max = False
        if show_canvas_max:
            bold_positions.append(wy_end)
        if show_box_bottom:
            bold_positions.append(wy_box)

        p.setPen(QPen(QColor("#999999")))
        step = _nice_step(self.box_bottom - self.box_top)
        min_gap = step * 0.3
        k_start = math.floor((self.view_top - self.box_top) / step)
        k_end = math.ceil((self.view_bottom - self.box_top) / step)
        metrics = p.fontMetrics()
        for k in range(k_start, k_end + 1):
            raw_y = self.box_top + k * step
            if raw_y < self.box_top or raw_y > self.box_bottom:
                continue
            if abs(raw_y - self.box_bottom) < min_gap:
                continue
            # k_start/k_end are deliberately loose (floor/ceil, so a
            # tick that's only PARTIALLY in view still gets included) --
            # without this explicit check, a tick genuinely scrolled
            # past the view edge still passed through, and the ty clamp
            # below then forced its label to sit right at the bottom
            # edge instead of actually disappearing as you zoomed past it.
            if not (self.view_top <= raw_y <= self.view_bottom):
                continue
            wy = y_to_widget(raw_y)
            # Yield to a bold marker rather than overlap it -- a
            # regular round-number tick landing right on top of the
            # canvas-edge or guide-position marker is exactly the
            # "smaller user-area, everything crowds together" overlap
            # that was reported.
            if any(abs(wy - bp) < label_h for bp in bold_positions):
                continue
            text = f"{k * step:.0f}"
            tw = metrics.horizontalAdvance(text)
            p.drawLine(self.width() - 6, int(wy), self.width(), int(wy))
            # now just a small edge-safety nudge (a couple px at most,
            # since wy is already confirmed within [0, h] above) rather
            # than a clamp that could drag a label far from its true position
            ty = min(max(int(wy) + metrics.ascent() // 2, metrics.ascent()), h - 2)
            p.drawText(self.width() - 8 - tw, ty, text)

        # The TRUE canvas edge (not the box's own draggable edge -- the
        # descender guide can sit anywhere and often doesn't match the
        # canvas boundary at all). Previously this positioned the tick
        # at box_bottom while labeling it with canvas_max -- correct
        # text, wrong position, so the label ended up drawn at the
        # WRONG spot on the ruler (confirmed: a "1000" label rendered
        # at the pixel position corresponding to 992, not 1000).
        # Position and label now both come from canvas_max, and the
        # whole block is skipped (show_canvas_max already False) when
        # it would overlap the guide's own position marker below.
        if show_canvas_max:
            font = p.font()
            font.setBold(True)
            p.setFont(font)
            p.setPen(QPen(QColor(style.GUIDE_COLOR)))
            p.drawLine(self.width() - 6, int(wy_end), self.width(), int(wy_end))
            text = f"{self.canvas_max:.0f}"
            tw = p.fontMetrics().horizontalAdvance(text)
            ty2 = min(max(int(wy_end) + p.fontMetrics().ascent() // 2, p.fontMetrics().ascent()), h - 2)
            p.drawText(self.width() - 8 - tw, ty2, text)

        # The descender guide's OWN current position, separately --
        # dropped entirely once the canvas-edge marker above was fixed
        # to stay at the true canvas edge, since descender usually
        # ISN'T there (that's the whole point of it being a draggable
        # guide). Without this there was no numeric readout of
        # descender's actual value anywhere on the ruler at all.
        if show_box_bottom:
            font = p.font()
            font.setBold(True)
            p.setFont(font)
            p.setPen(QPen(QColor(style.GUIDE_COLOR)))
            p.drawLine(self.width() - 6, int(wy_box), self.width(), int(wy_box))
            text = f"{self.box_bottom:.0f}"
            tw = p.fontMetrics().horizontalAdvance(text)
            ty3 = min(max(int(wy_box) + p.fontMetrics().ascent() // 2, p.fontMetrics().ascent()), h - 2)
            p.drawText(self.width() - 8 - tw, ty3, text)
        p.end()


class MetricsDrawing(QWidget):
    """LEFT half of the split metrics diagram: the schematic drawing
    and row NAME labels only -- no numeric values at all (those live
    in the sibling MetricsValues widget, see MetricsDiagram below).
    Splitting drawing from text this way was a deliberate simplification:
    the previous single-widget version had to juggle label widths
    against column positions in the same paintEvent, which is exactly
    what kept causing clipping/collision bugs across several draft
    iterations. Now this widget only ever paints fixed, schematic
    geometry -- nothing here depends on how long any text turns out
    to be.

    Also owns the single Ascent/Descent override checkbox and the
    connector line linking it down to those two rows -- kept here,
    tied visually to the drawing's own leader lines, rather than in
    the values panel: the connector reads as one continuous line
    (checkbox -> connector -> dimension-line leader -> row) precisely
    because it's part of the same drawing, not a separate element
    bolted onto the value column. Confirmed directly this was the
    right call: an earlier draft moved the checkbox into the values
    panel and it read as visually disconnected from what it actually
    controls."""

    _Y = {"em": 20, "ascent": 70, "cap": 115, "xheight": 160, "baseline": 205,
          "descender": 270, "descent": 315, "advance": 355}
    _Y_MAX = 400
    _DESIGN_W = 260   # the reference width the constants below were
                       # calibrated at (the approved draft's left panel).
                       # Widened from the draft's own 235 after directly
                       # measuring that "Cap height" and "Descender"
                       # (~64-65px each) didn't fit in the ~48px that
                       # remained between LABEL_X and 235 -- confirmed
                       # mathematically, not just by eye. Since __init__
                       # pins minimumWidth to this same value (dscale
                       # locked at 1.0), raising it simply gives the
                       # label column more trailing room without moving
                       # any other coordinate.
    _BOX_L, _BOX_R = 60, 170
    _MARGIN = 15
    _COL_OUTER, _COL_MID, _COL_INNER = 105, 125, 145   # ascent/descent, cap/descender, x-height
    _LEADER_END = 181
    _LABEL_X = 187
    _CONNECTOR_X = 14
    _CONNECTOR_BRANCH = 75
    _CONNECTOR_COLOR = "#8a5cc4"   # distinct from GUIDE_COLOR (dimension
                                    # lines) and BOX_COLOR (leaders/margin)
                                    # on purpose -- this is neither of
                                    # those, it's a third, separate kind
                                    # of relationship being drawn.

    def __init__(self, parent=None):
        super().__init__(parent)
        # Full design width, not something smaller -- confirmed
        # directly that letting this shrink further (dscale below 1.0)
        # runs out of room for row-name text before the widget's own
        # edge (e.g. "Cap height" needs more space than remained at
        # 190px). Guaranteeing dscale==1.0 in practice means labels
        # always get their full, already-verified-to-fit allotment
        # rather than degrading unpredictably at whatever width a
        # layout happens to squeeze this into.
        self.setMinimumWidth(self._DESIGN_W)
        self.setMinimumHeight(300)
        self.override_check = QCheckBox(self)
        self.override_check.setToolTip("Override Ascent/Descent (unchecked: both track Units per EM automatically)")
        self._position_checkbox()

    def _dscale(self) -> float:
        # Same reasoning as the original combined widget's _dscale --
        # shrink proportionally on a narrower panel than the design,
        # never grow past the design size on a wider one.
        return min(self.width() / self._DESIGN_W, 1.0) if self.width() > 0 else 1.0

    def _y(self, key: str) -> float:
        return self._Y[key] / self._Y_MAX * self.height()

    def resizeEvent(self, event):
        self._position_checkbox()
        super().resizeEvent(event)

    def _position_checkbox(self):
        s = self._dscale()
        cx = int(self._CONNECTOR_X * s) - 6
        cy = int(self._y("em")) - 8
        self.override_check.setGeometry(max(cx, 2), max(cy, 2), 16, 16)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        y = {k: self._y(k) for k in self._Y}
        s = self._dscale()
        box_l, box_r = self._BOX_L * s, self._BOX_R * s
        m = self._MARGIN * s
        col_outer, col_mid, col_inner = self._COL_OUTER * s, self._COL_MID * s, self._COL_INNER * s
        leader_end = self._LEADER_END * s
        label_x = self._LABEL_X * s
        conn_x = self._CONNECTOR_X * s
        conn_branch = self._CONNECTOR_BRANCH * s

        # "Units per EM" label, next to the checkbox.
        p.setPen(QColor(style.INK_COLOR))
        p.drawText(QPointF(conn_x + 20, y["em"] + 4), "Units per EM")

        # Connector: checkbox down to Ascent's row, and separately down
        # to Descent's row, branching right to meet the drawing at the
        # box edge -- continuous with the leader lines drawn below, not
        # a separate visual element.
        pen_conn = QPen(QColor(self._CONNECTOR_COLOR), 1.2)
        p.setPen(pen_conn)
        p.setOpacity(0.6)
        p.drawLine(QPointF(conn_x, y["em"] + 7), QPointF(conn_x, y["ascent"]))
        p.drawLine(QPointF(conn_x, y["ascent"]), QPointF(conn_branch, y["ascent"]))
        p.drawLine(QPointF(conn_x, y["ascent"]), QPointF(conn_x, y["descent"]))
        p.drawLine(QPointF(conn_x, y["descent"]), QPointF(conn_branch, y["descent"]))
        p.setOpacity(1.0)

        # Canvas rect + hatched weight margin on all four sides (not
        # just top/bottom -- round-cap overshoot happens in every
        # direction a stroke can extend, confirmed as an oversight in
        # an earlier draft).
        hatch = QBrush(QColor(style.BOX_COLOR))
        hatch.setStyle(Qt.BDiagPattern)
        p.setPen(Qt.NoPen)
        p.setBrush(hatch)
        p.drawRect(QRectF(box_l - m, y["cap"] - m, (box_r - box_l) + 2 * m, m))            # top
        p.drawRect(QRectF(box_l - m, y["descender"], (box_r - box_l) + 2 * m, m))          # bottom
        p.drawRect(QRectF(box_l - m, y["cap"], m, y["descender"] - y["cap"]))              # left
        p.drawRect(QRectF(box_r, y["cap"], m, y["descender"] - y["cap"]))                  # right
        p.setPen(QPen(QColor(style.BOX_COLOR), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(box_l - m, y["cap"] - m,
                                  (box_r - box_l) + 2 * m, (y["descender"] - y["cap"]) + 2 * m), 3, 3)

        # Working box (dashed) -- the actual cap-to-descender extent.
        pen_dash = QPen(QColor(style.BOX_COLOR), 1, Qt.DashLine)
        p.setPen(pen_dash)
        p.drawRect(QRectF(box_l, y["cap"], box_r - box_l, y["descender"] - y["cap"]))

        # "Canvas" text removed from the drawing entirely -- the
        # GroupBox wrapping this widget now carries the label AND the
        # actual dimensions directly in its own title ("Canvas (WxH)",
        # see _refresh_metrics_label), which was also the fix for a
        # separate, real gap: this drawing has no natural place to show
        # the canvas's actual pixel dimensions at all, and Advance
        # alone only coincidentally matches canvas width, and only
        # when it hasn't been dragged away from that default.

        # Baseline -- solid, bold-weight, no value (the reference
        # everything else measures from, not a measured quantity).
        pen_main = QPen(QColor(style.INK_COLOR), 1.3)
        p.setPen(pen_main)
        p.drawLine(QPointF(box_l, y["baseline"]), QPointF(box_r, y["baseline"]))

        # Dimension lines (double-headed) and their leaders.
        pen_dim = QPen(QColor(style.GUIDE_COLOR), 1)
        pen_leader = QPen(QColor(style.BOX_COLOR), 1, Qt.DashLine)

        def dim_line(x, y0, y1):
            p.setPen(pen_dim)
            self._draw_arrow_line(p, x, y0, x, y1)

        def leader(x0, y0, x1, y1):
            p.setPen(pen_leader)
            p.drawLine(QPointF(x0, y0), QPointF(x1, y1))

        dim_line(col_outer, y["ascent"], y["baseline"])
        dim_line(col_mid, y["cap"], y["baseline"])
        dim_line(col_inner, y["xheight"], y["baseline"])
        dim_line(col_mid, y["baseline"], y["descender"])
        dim_line(col_outer, y["baseline"], y["descent"])
        p.setPen(pen_dim)
        self._draw_arrow_line(p, box_l, y["advance"], box_r, y["advance"])

        leader(col_outer, y["ascent"], leader_end, y["ascent"])
        leader(box_r, y["cap"], leader_end, y["cap"])
        leader(col_inner, y["xheight"], leader_end, y["xheight"])
        leader(box_r, y["baseline"], leader_end, y["baseline"])
        leader(box_r, y["descender"], leader_end, y["descender"])
        leader(col_outer, y["descent"], leader_end, y["descent"])
        leader(box_l, y["advance"], leader_end, y["advance"])

        # Row name labels only -- no values here (see MetricsValues).
        p.setPen(QColor(style.INK_COLOR))
        names = {"ascent": "Ascent", "cap": "Cap height", "xheight": "X-height",
                 "baseline": "Baseline", "descender": "Descender",
                 "descent": "Descent", "advance": "Advance"}
        for key, label in names.items():
            p.drawText(QPointF(label_x, y[key] + 4), label)
        p.end()

    @staticmethod
    def _draw_arrow_line(p: QPainter, x0, y0, x1, y1, size=4.0):
        p.drawLine(QPointF(x0, y0), QPointF(x1, y1))
        for (ax, ay), (bx, by) in (((x0, y0), (x1, y1)), ((x1, y1), (x0, y0))):
            angle = math.atan2(ay - by, ax - bx)
            for da in (0.5, -0.5):
                a = angle + da
                p.drawLine(QPointF(ax, ay), QPointF(ax - size * math.cos(a), ay - size * math.sin(a)))


class MetricsValues(QWidget):
    """RIGHT half of the split metrics diagram: numeric values only,
    right-aligned, with Ascent/Descent as real keyboard-entry fields
    (not spinboxes -- a compact override target doesn't need step
    buttons) plus a px-equivalent hint beneath each. Row y-positions
    mirror MetricsDrawing's own _Y table exactly (scaled to THIS
    widget's height, which the parent HBoxLayout keeps equal to the
    drawing widget's height) so rows line up across the two bordered
    panels without either widget needing to know about the other's
    internals."""

    _Y = MetricsDrawing._Y
    _Y_MAX = MetricsDrawing._Y_MAX
    _EDIT_W = 70
    _PAD_R = 10

    emEdited = Signal(str)
    ascentEdited = Signal(str)
    descentEdited = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Just enough for the edit boxes (_EDIT_W=70) plus padding
        # (_PAD_R=10), with a small buffer -- was 160, nearly double
        # what's actually needed. Confirmed as the actual source of a
        # reported "huge gap between the graphic and values": with
        # every value right-aligned against this panel's own right
        # edge, an oversized panel just pushes the numbers far to the
        # right of where the drawing panel ends, reading as a gap even
        # though the two panels are directly adjacent with no spacing
        # between them.
        self.setMinimumWidth(95)
        self.setMinimumHeight(300)
        self.em_edit = QLineEdit(self)
        self.ascent_edit = QLineEdit(self)
        self.descent_edit = QLineEdit(self)
        for w in (self.em_edit, self.ascent_edit, self.descent_edit):
            w.setAlignment(Qt.AlignRight)
            w.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.em_edit.editingFinished.connect(lambda: self.emEdited.emit(self.em_edit.text()))
        self.ascent_edit.editingFinished.connect(lambda: self.ascentEdited.emit(self.ascent_edit.text()))
        self.descent_edit.editingFinished.connect(lambda: self.descentEdited.emit(self.descent_edit.text()))

        self._cap_label = QLabel(self)
        self._xheight_label = QLabel(self)
        self._descender_label = QLabel(self)
        self._advance_label = QLabel(self)
        self._ascent_px_label = QLabel(self)
        self._descent_px_label = QLabel(self)
        for lbl in (self._cap_label, self._xheight_label, self._descender_label, self._advance_label):
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        for lbl in (self._ascent_px_label, self._descent_px_label):
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl.setStyleSheet("color: #888;")
        self._position_children()

    def resizeEvent(self, event):
        self._position_children()
        super().resizeEvent(event)

    def _y(self, key: str) -> float:
        return self._Y[key] / self._Y_MAX * self.height()

    def _position_children(self):
        right = self.width() - self._PAD_R
        w = self._EDIT_W
        self.em_edit.setGeometry(right - w, int(self._y("em")) - 9, w, 18)
        self.ascent_edit.setGeometry(right - w, int(self._y("ascent")) - 9, w, 18)
        self._ascent_px_label.setGeometry(right - w, int(self._y("ascent")) + 10, w, 14)
        self._cap_label.setGeometry(0, int(self._y("cap")) - 9, right, 18)
        self._xheight_label.setGeometry(0, int(self._y("xheight")) - 9, right, 18)
        self._descender_label.setGeometry(0, int(self._y("descender")) - 9, right, 18)
        self.descent_edit.setGeometry(right - w, int(self._y("descent")) - 9, w, 18)
        self._descent_px_label.setGeometry(right - w, int(self._y("descent")) + 10, w, 14)
        self._advance_label.setGeometry(0, int(self._y("advance")) - 9, right, 18)

    def set_override_enabled(self, enabled: bool):
        self.ascent_edit.setEnabled(enabled)
        self.descent_edit.setEnabled(enabled)

    def set_values(self, em, ascent, cap, xheight, descender, descent, advance):
        def fmt(v, decimals=1):
            return f"{v:.{decimals}f}" if v is not None else "\u2014"

        if not self.em_edit.hasFocus():
            self.em_edit.setText(str(em) if em is not None else "\u2014")
        if not self.ascent_edit.hasFocus():
            self.ascent_edit.setText(fmt(ascent))
        if not self.descent_edit.hasFocus():
            self.descent_edit.setText(fmt(descent))
        self._cap_label.setText(fmt(cap))
        self._xheight_label.setText(fmt(xheight))
        self._descender_label.setText(fmt(descender))
        self._advance_label.setText(fmt(advance))

    def set_px_hints(self, ascent_px, descent_px):
        self._ascent_px_label.setText(f"\u2248 {ascent_px:.0f}px" if ascent_px is not None else "")
        self._descent_px_label.setText(f"\u2248 {descent_px:.0f}px" if descent_px is not None else "")


class MetricsDiagram(QWidget):
    """Thin wrapper holding MetricsDrawing (left) and MetricsValues
    (right) side by side, sharing height via this layout so their rows
    align, and translating project-level state (font.em,
    ascent_override, descent_override, live metrics/guide values) into
    what each half needs to show. Exposes the same set_values() call
    signature callers already use, so nothing calling this had to
    change when the single combined widget was split into two."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Fixed, not Preferred, horizontally -- Preferred happily
        # accepts extra width the splitter offers it, and with no
        # explicit stretch factors set on the splitter (see _build_ui),
        # a wider window had nowhere else defined for that extra space
        # to go, so it landed here instead of the canvas. Confirmed
        # directly as a real, reported problem: going fullscreen pushed
        # this widget wide enough to open a large gap between the
        # drawing and the right-aligned value fields, and forced every
        # OTHER sidebar box wider along with it, narrowing the glyph
        # list as a side effect. Fixed at its own natural width now;
        # extra window width goes to the canvas instead (see the
        # splitter's own stretch factors).
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.drawing = MetricsDrawing()
        self.values = MetricsValues()
        # Stretch factors match the approved draft's own drawing:values
        # width ratio (235:275), not an even 1:1 split -- confirmed
        # directly that equal stretch starves the drawing side of room
        # for its row-name labels ("Cap height", "X-height",
        # "Descender" all visibly clipped) well before the values side
        # actually needed all of its own share.
        layout.addWidget(self.drawing, 260)
        layout.addWidget(self.values, 275)
        self.setMinimumHeight(320)

        self.project = None
        self._px_from_design: dict[str, float] = {}

        self.drawing.override_check.toggled.connect(self._on_override_toggled)
        self.values.emEdited.connect(self._on_em_edited)
        self.values.ascentEdited.connect(self._on_ascent_edited)
        self.values.descentEdited.connect(self._on_descent_edited)
        self.values.set_override_enabled(False)

    def set_project(self, project):
        # A genuinely NEW project is the only time the checkbox should
        # be synced from stored data (was an override actually saved in
        # THIS project) -- see set_values below for why every other
        # refresh must leave the checkbox alone.
        is_new = project is not self.project
        self.project = project
        if is_new and project is not None:
            has_override = project.font.ascent_override is not None or project.font.descent_override is not None
            self.drawing.override_check.blockSignals(True)
            self.drawing.override_check.setChecked(has_override)
            self.drawing.override_check.blockSignals(False)
            self.values.set_override_enabled(has_override)

    def set_values(self, ascent, cap, xheight, descender, descent, advance, canvas_size):
        # cap/xheight/descender/advance/canvas_size are CANVAS-PIXEL
        # values (guide positions); ascent/descent are the EM-DESIGN-
        # SPACE export values -- genuinely different unit systems, kept
        # deliberately visible side by side rather than converted into
        # one another (see the Advanced Settings px-hint feature this
        # mirrors).
        #
        # Deliberately does NOT touch the override checkbox's checked
        # state (that's set_project's job, on a genuine project
        # change, and _on_override_toggled's job, on the user's own
        # click) -- confirmed as a real, reported bug when it did:
        # QLineEdit.editingFinished fires on focus loss even when the
        # text didn't actually change, so clicking from Ascent into
        # Descent (or back) without editing either fired this method
        # via the edit handlers, recomputed "no override currently
        # set" (correctly true, since neither field had diverged from
        # auto), and silently unchecked a box the user never touched.
        # Whether editing is ENABLED and whether an override CURRENTLY
        # EXISTS are related but different things; only the checkbox's
        # own explicit state should ever decide the former.
        em = self.project.font.em if self.project else None
        self.values.set_values(em, ascent, cap, xheight, descender, descent, advance)
        self._refresh_px_hints(cap)

    def _refresh_px_hints(self, cap_px):
        # Inverse of the cap_design/cap_px scale factor -- see the
        # Advanced Settings dialog's own _design_to_canvas_px for the
        # identical reasoning; duplicated here rather than shared
        # since the two live in different modules and this is a small
        # enough calculation not to be worth a new shared helper for.
        if not self.project or cap_px is None or cap_px <= 0:
            self.values.set_px_hints(None, None)
            return
        em = self.project.font.em
        cap_design = round(em * 0.68)
        if cap_design <= 0:
            self.values.set_px_hints(None, None)
            return
        scale = cap_px / cap_design
        ascent_design = float(self.values.ascent_edit.text() or 0) if self.values.ascent_edit.text() != "\u2014" else None
        descent_design = float(self.values.descent_edit.text() or 0) if self.values.descent_edit.text() != "\u2014" else None
        ascent_px = ascent_design * scale if ascent_design is not None else None
        descent_px = descent_design * scale if descent_design is not None else None
        self.values.set_px_hints(ascent_px, descent_px)

    def _on_override_toggled(self, checked: bool):
        if not self.project:
            return
        self.values.set_override_enabled(checked)
        if not checked:
            # Clearing auto-returns both to the auto-calculated values
            # -- explicit, immediate, not just "editing disabled while
            # the old override value lingers underneath."
            self.project.font.ascent_override = None
            self.project.font.descent_override = None
            self._resync_from_project()

    def _on_em_edited(self):
        if not self.project:
            return
        try:
            em = int(self.values.em_edit.text())
        except ValueError:
            self._resync_from_project()
            return
        em = max(64, min(16384, em))
        self.project.font.em = em
        # Deliberately does NOT touch ascent_override/descent_override
        # -- an explicit manual value is a deliberate choice and
        # shouldn't be silently discarded just because EM changed for
        # an unrelated reason; anything still auto-tracking (because it
        # wasn't manually diverged) updates naturally on the next
        # refresh regardless.
        self._resync_from_project()

    def _parse_or_none(self, text: str, auto_value: float | None) -> int | None:
        """None means "keep tracking auto" -- returned when the typed
        text is blank, unparseable, or matches what auto currently
        computes (within rounding), so leaving a field alone (or
        re-typing the same number auto would already show) doesn't
        create a spurious stored override.

        Returns an int, not a raw float -- confirmed as a real, export-
        crashing bug otherwise: the 'hhea' table's ascent/descent
        fields are packed as signed shorts, which fontTools rejects a
        float for outright (auto_vertical_metrics already returns
        plain ints via round() for exactly this reason; the override
        path needs to match it, not bypass it)."""
        try:
            val = float(text)
        except ValueError:
            return None
        if auto_value is not None and abs(val - auto_value) < 0.5:
            return None
        return round(val)

    def _on_ascent_edited(self):
        if not self.project:
            return
        auto_ascent, _ = auto_vertical_metrics(self.project.font, self.project.metrics)
        self.project.font.ascent_override = self._parse_or_none(self.values.ascent_edit.text(), auto_ascent)
        self._resync_from_project()

    def _on_descent_edited(self):
        if not self.project:
            return
        _, auto_descent = auto_vertical_metrics(self.project.font, self.project.metrics)
        self.project.font.descent_override = self._parse_or_none(self.values.descent_edit.text(), auto_descent)
        self._resync_from_project()

    def _resync_from_project(self):
        """Re-derives everything this widget shows from the project's
        own current state -- the single path both external callers
        (set_values, via _refresh_metrics_label) and this widget's own
        edit handlers go through, so there's no separate "internal"
        update logic that could drift from what a fresh external
        refresh would show."""
        if not self.project:
            return
        m = self.project.metrics

        def _dist(a, b):
            return (b - a) if a is not None and b is not None else None

        auto_ascent, auto_descent = auto_vertical_metrics(self.project.font, m)
        ascent = self.project.font.ascent_override if self.project.font.ascent_override is not None else auto_ascent
        descent = self.project.font.descent_override if self.project.font.descent_override is not None else auto_descent
        self.set_values(
            ascent=ascent, cap=_dist(m.cap_top_px, m.baseline_px),
            xheight=_dist(m.xheight_top_px, m.baseline_px),
            descender=_dist(m.baseline_px, m.descender_bottom_px),
            descent=descent, advance=m.advance_px,
            canvas_size=tuple(m.canvas_size) if m.canvas_size else None,
        )


class GuideLabelStrip(QWidget):
    """A fixed-position strip on the RIGHT of the canvas showing
    draggable name pills for the four horizontal guides (cap/x-height/
    baseline/descender) -- a pill only appears while that guide's line
    is actually within the current view, at the height corresponding
    to where the line currently sits, same visibility rule the old
    on-canvas labels used.

    This -- not the thin line drawn across the canvas -- is now how
    guides get dragged. The line itself lost ItemIsMovable specifically
    because a 1px target was hard to grab precisely and easy to grab
    by accident while working on nearby glyph segments; a strip
    entirely outside the canvas's own drawable area has neither problem."""

    PILL_H = 20

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.setFixedWidth(90)
        self.setMouseTracking(True)
        self.view_top: float | None = None
        self.view_bottom: float | None = None
        self._pill_rects: dict[str, QRectF] = {}
        self._drag_name: str | None = None
        # Delta accumulation, not absolute-position mapping (interpreting
        # event.pos() directly) -- the drag can start from anywhere
        # within the pill's own rect, not necessarily exactly on the
        # guide's own scene position, so tracking relative movement from
        # wherever the drag began is what keeps the guide from jumping
        # to align with the click point the instant the drag starts.
        # view_top/view_bottom (used for sensitivity and the boundary
        # clamp -- see mouseMoveEvent) stay fixed for the whole drag now
        # that the view no longer rescales mid-drag at all, so unlike an
        # earlier version of this comment, there's no longer a
        # feedback-loop risk to design around here.
        self._drag_last_y: float | None = None
        self._drag_accum_y: float | None = None
        # Right-click-drag panning, independent of any left-button
        # guide drag -- both can be held at once (left on a pill here,
        # right anywhere on this same strip), the same way panning
        # already works alongside drawing on the canvas itself. This
        # strip is a separate widget from the canvas, though, so a
        # right-button press delivered HERE (which it will be, while
        # the left button's own press already has this widget) needs
        # its own handling rather than falling through to the canvas's
        # existing pan logic, which only ever sees events the canvas
        # itself receives.
        self._pan_last_pos: QPointF | None = None

    def set_view(self, view_top: float | None, view_bottom: float | None):
        self.view_top = view_top
        self.view_bottom = view_bottom
        self.update()

    def _y_to_widget(self, y: float) -> float:
        span = self.view_bottom - self.view_top
        return (y - self.view_top) / span * self.height()

    def _widget_to_y(self, wy: float) -> float:
        span = self.view_bottom - self.view_top
        return self.view_top + (wy / self.height()) * span

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(style.CANVAS_BG))
        self._pill_rects = {}
        if self.view_top is None or self.view_bottom is None or self.view_bottom <= self.view_top:
            p.end()
            return

        font = p.font()
        font.setPointSize(8)
        font.setBold(True)
        p.setFont(font)
        metrics = p.fontMetrics()

        for name, guide in self.canvas._guides.items():
            if guide.vertical:
                continue
            y = guide.current_scene_position()
            if not (self.view_top <= y <= self.view_bottom):
                continue
            wy = self._y_to_widget(y)
            color = QColor(style.BASELINE_COLOR if name == "baseline" else style.GUIDE_COLOR)

            p.setPen(QPen(color, 1, Qt.DashLine if name != "baseline" else Qt.SolidLine))
            p.drawLine(0, int(wy), 8, int(wy))

            text_w = metrics.horizontalAdvance(name)
            w, h = text_w + 12, self.PILL_H
            rect = QRectF(10, wy - h / 2, w, h)
            bg = QColor(color)
            bg.setAlpha(220 if self._drag_name == name else 255)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(rect, 4, 4)
            p.setPen(QColor("#ffffff"))
            p.drawText(rect, Qt.AlignCenter, name)
            self._pill_rects[name] = rect
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            # Independent of any left-button guide drag already in
            # progress -- see this widget's own _pan_last_pos comment.
            self._pan_last_pos = event.position() if hasattr(event, "position") else event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        pos = event.position() if hasattr(event, "position") else event.pos()
        for name, rect in self._pill_rects.items():
            if rect.contains(pos):
                self._drag_name = name
                guide = self.canvas._guides.get(name)
                self._drag_accum_y = guide.current_scene_position() if guide is not None else None
                self._drag_last_y = pos.y()
                self.update()
                return

    def mouseMoveEvent(self, event):
        if self._pan_last_pos is not None:
            # Same scrollbar-translation mechanism the canvas's own
            # panning already uses -- delegated to here explicitly
            # since GlyphCanvas.mouseMoveEvent never sees events this
            # strip receives directly.
            pos = event.position() if hasattr(event, "position") else event.pos()
            delta = pos - self._pan_last_pos
            self._pan_last_pos = pos
            self.canvas.horizontalScrollBar().setValue(
                self.canvas.horizontalScrollBar().value() - int(delta.x()))
            self.canvas.verticalScrollBar().setValue(
                self.canvas.verticalScrollBar().value() - int(delta.y()))
            self.canvas._clamp_pan_to_box()
            self.canvas.viewChanged.emit()
            # Not a return -- a guide drag can be active on the SAME
            # tick (left button held on a pill, right button also held
            # for panning), and both should keep progressing together,
            # the same way panning and drawing already coexist on the
            # canvas itself.
        if self._drag_name is None or self._drag_accum_y is None:
            return
        pos = event.position() if hasattr(event, "position") else event.pos()
        delta_y = pos.y() - self._drag_last_y
        self._drag_last_y = pos.y()
        # Sensitivity is exactly this strip's own current view-to-widget
        # ratio -- (view_bottom - view_top) / height -- the same slope
        # _widget_to_y already uses to place the pill on screen from a
        # scene position. Matching it exactly (rather than an
        # independent, canvas-size-derived ratio, which is what this
        # used to be) means one physical pixel of mouse travel now moves
        # the pill by exactly one pixel on screen, always, regardless of
        # canvas size or current zoom -- a true 1:1 follow, not an
        # approximation of one. That in turn is what makes the
        # Shift-for-precision modifier and the hide-and-recenter cursor
        # trick both unnecessary rather than just simplified: precision
        # now comes purely from how far the user has zoomed in (the
        # same as ordinary point-dragging already works), and the real
        # cursor and the pill can no longer drift apart in the first
        # place, since they're both driven by the same 1:1 mapping --
        # there was never anything left to hide or correct for.
        span = (self.view_bottom - self.view_top) if (self.view_top is not None
                and self.view_bottom is not None) else 0.0
        sensitivity = span / self.height() if self.height() > 0 else 1.0
        self._drag_accum_y += delta_y * sensitivity
        guide = self.canvas._guides.get(self._drag_name)
        if guide is not None:
            target_y = self._drag_accum_y
            # Ctrl bypasses guide-snap entirely, same convention as
            # point-dragging/apply_snap -- checked from the event's own
            # modifiers, since this method receives the actual event
            # directly. Confirmed as a real, reported problem otherwise:
            # snap_point_tolerance's fixed radius could repeatedly
            # re-grab a slow, deliberate drag near an existing point.
            ctrl_bypass = bool(event.modifiers() & Qt.ControlModifier)
            # Point-snap, then grid-snap if point-snap didn't match --
            # same priority order apply_snap already uses for ordinary
            # point dragging (see its own comment on why grid needs an
            # explicit "did segments actually match" check rather than
            # a plain elif on the checkbox alone).
            #
            # Point-snap now covers all four horizontal guides, not
            # just x-height/baseline -- cap and descender have exactly
            # the same "align to what's already drawn" use case (e.g.
            # cap matching a shape's own highest point) that x-height/
            # baseline already had; there was never a real reason only
            # two of the four should get it.
            matched = False
            if not ctrl_bypass and self._drag_name in ("cap", "x-height", "baseline", "descender"):
                snapped = self.canvas.snap_guide_to_points_y(target_y)
                if snapped is not None:
                    target_y = snapped
                    matched = True
            if not ctrl_bypass and not matched and self.canvas.snap_to_grid:
                snapped = self.canvas._snap_to_grid_xy(0.0, target_y)
                if snapped is not None:
                    target_y = snapped[1]
            # Clamped to whatever's currently visible, not left free to
            # run arbitrarily far off screen -- confirmed as a real
            # design problem with the earlier live-rescaling approach,
            # not just its jitter bug: auto-panning meant the user had
            # no fixed, visible frame of reference for how close a
            # guide was to the edge or to another guide (unless
            # deliberately watching the legends), and the view itself
            # was what generated the jitter in the first place (see
            # resync_scene_to_box's own docstring). Reachable range is
            # now exactly what's on screen -- zoomed out, that's the
            # whole canvas; zoomed in for fine work, it's deliberately
            # narrower, which is the right tradeoff: fine positioning
            # is specifically the case where a guide silently leaping
            # off screen from one drag gesture would be least wanted.
            if self.view_top is not None and self.view_bottom is not None:
                target_y = max(self.view_top, min(self.view_bottom, target_y))
            guide.setPos(QPointF(0, target_y - guide.line().y1()))
            # Cheap, visual-only box/margin outline update -- NOT
            # resync_scene_to_box's own fitInView/re-zoom, which used to
            # run here on every tick and was confirmed as the actual
            # cause of a real, reported jitter bug: as cap and descender
            # converged, box_rect's own height shrank toward its margin
            # floor, driving fitInView's zoom factor into instability
            # tick to tick. The view no longer moves during this drag
            # at all (see the clamp above), so there's nothing left
            # that could reproduce that instability.
            self.canvas._update_box_visuals()
            # setPos() on the guide item repaints the CANVAS live via
            # Qt's own scene graph, but this strip is a separate plain
            # widget entirely -- nothing tells it to repaint on its own,
            # so its pill was only catching up to the guide's new
            # position whenever some unrelated event happened to
            # trigger a repaint, not during the drag itself.
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton and self._pan_last_pos is not None:
            self._pan_last_pos = None
            self.unsetCursor()
            return
        self._drag_name = None
        self._drag_last_y = None
        self._drag_accum_y = None
        self.update()


class AdvanceLabelStrip(QWidget):
    """The horizontal counterpart to GuideLabelStrip, along the BOTTOM
    of the canvas -- a single draggable pill for the "advance" guide,
    shown only while its (vertical) line is within the current view."""

    PILL_W_PAD = 12
    PILL_H = 20   # matches GuideLabelStrip.PILL_H -- these were mismatched
                  # (16 vs 20) despite using the same font size, which is
                  # exactly what made "advance" read as smaller text when
                  # it was really just a smaller background box around
                  # identically-sized text

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.setFixedHeight(32)
        self.setMouseTracking(True)
        self.view_left: float | None = None
        self.view_right: float | None = None
        self._pill_rect: QRectF | None = None
        self._dragging = False
        # See GuideLabelStrip's own attributes of the same name for
        # the full reasoning -- delta accumulation, scaled per pixel by
        # this strip's own current view-to-widget ratio.
        self._drag_last_x: float | None = None
        self._drag_accum_x: float | None = None
        # Right-click-drag panning, independent of the left-button
        # advance drag -- see GuideLabelStrip's own attribute of the
        # same name for the full reasoning.
        self._pan_last_pos: QPointF | None = None

    def set_view(self, view_left: float | None, view_right: float | None):
        self.view_left = view_left
        self.view_right = view_right
        self.update()

    def _x_to_widget(self, x: float) -> float:
        span = self.view_right - self.view_left
        return (x - self.view_left) / span * self.width()

    def _widget_to_x(self, wx: float) -> float:
        span = self.view_right - self.view_left
        return self.view_left + (wx / self.width()) * span

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(style.CANVAS_BG))
        self._pill_rect = None
        guide = self.canvas._guides.get("advance")
        if (guide is None or self.view_left is None or self.view_right is None
                or self.view_right <= self.view_left):
            p.end()
            return
        x = guide.current_scene_position()
        if not (self.view_left <= x <= self.view_right):
            p.end()
            return
        wx = self._x_to_widget(x)

        font = p.font()
        font.setPointSize(8)
        font.setBold(True)
        p.setFont(font)
        metrics = p.fontMetrics()
        color = QColor(style.GUIDE_COLOR)

        p.setPen(QPen(color, 1, Qt.DashLine))
        p.drawLine(int(wx), 0, int(wx), 8)

        text_w = metrics.horizontalAdvance("advance")
        w, h = text_w + self.PILL_W_PAD, self.PILL_H
        # Centered on wx, but clamped to stay fully within the widget --
        # otherwise the pill's right edge extends past self.width() (or
        # the left edge past 0) whenever the guide itself sits near
        # either edge of the current view, visibly clipping the text.
        # Confirmed: dragging advance to a canvas's far-right boundary
        # cut "advance" off mid-word. The dashed connector line still
        # marks the guide's TRUE position at wx; only the pill's own
        # box shifts to stay legible.
        pill_x = min(max(wx - w / 2, 0.0), self.width() - w)
        rect = QRectF(pill_x, 8, w, h)
        bg = QColor(color)
        bg.setAlpha(220 if self._dragging else 255)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect, 4, 4)
        p.setPen(QColor("#ffffff"))
        p.drawText(rect, Qt.AlignCenter, "advance")
        self._pill_rect = rect
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            # Same reasoning and convention as GuideLabelStrip's
            # mousePressEvent above.
            self._pan_last_pos = event.position() if hasattr(event, "position") else event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        pos = event.position() if hasattr(event, "position") else event.pos()
        if self._pill_rect is not None and self._pill_rect.contains(pos):
            self._dragging = True
            guide = self.canvas._guides.get("advance")
            self._drag_accum_x = guide.current_scene_position() if guide is not None else None
            self._drag_last_x = pos.x()
            self.update()

    def mouseMoveEvent(self, event):
        if self._pan_last_pos is not None:
            # Same reasoning and convention as GuideLabelStrip's
            # mouseMoveEvent above -- not a return, since an advance
            # drag can be active on the same tick.
            pos = event.position() if hasattr(event, "position") else event.pos()
            delta = pos - self._pan_last_pos
            self._pan_last_pos = pos
            self.canvas.horizontalScrollBar().setValue(
                self.canvas.horizontalScrollBar().value() - int(delta.x()))
            self.canvas.verticalScrollBar().setValue(
                self.canvas.verticalScrollBar().value() - int(delta.y()))
            self.canvas._clamp_pan_to_box()
            self.canvas.viewChanged.emit()
        if not self._dragging or self._drag_accum_x is None:
            return
        pos = event.position() if hasattr(event, "position") else event.pos()
        delta_x = pos.x() - self._drag_last_x
        self._drag_last_x = pos.x()
        # Sensitivity is this strip's own current view-to-widget ratio,
        # same reasoning as GuideLabelStrip's mouseMoveEvent above.
        span = (self.view_right - self.view_left) if (self.view_left is not None
                and self.view_right is not None) else 0.0
        sensitivity = span / self.width() if self.width() > 0 else 1.0
        self._drag_accum_x += delta_x * sensitivity
        guide = self.canvas._guides.get("advance")
        if guide is not None:
            target_x = self._drag_accum_x
            # Ctrl bypasses guide-snap entirely, same reasoning and
            # convention as GuideLabelStrip's mouseMoveEvent above.
            ctrl_bypass = bool(event.modifiers() & Qt.ControlModifier)
            # Same point-then-grid priority as GuideLabelStrip's
            # mouseMoveEvent above, just X-based (see
            # snap_guide_to_points_x's own docstring for why advance
            # needs its own X-only variant rather than reusing the
            # Y-based one every other guide shares).
            matched = False
            if not ctrl_bypass:
                snapped = self.canvas.snap_guide_to_points_x(target_x)
                if snapped is not None:
                    target_x = snapped
                    matched = True
            if not ctrl_bypass and not matched and self.canvas.snap_to_grid:
                snapped = self.canvas._snap_to_grid_xy(target_x, 0.0)
                if snapped is not None:
                    target_x = snapped[0]
            # Clamped to the currently-visible range, same reasoning as
            # GuideLabelStrip's mouseMoveEvent above.
            if self.view_left is not None and self.view_right is not None:
                target_x = max(self.view_left, min(self.view_right, target_x))
            guide.setPos(QPointF(target_x - guide.line().x1(), 0))
            # Cheap, visual-only box/margin outline update, not a live
            # re-fit -- same reasoning as GuideLabelStrip's
            # mouseMoveEvent above.
            self.canvas._update_box_visuals()
            self.update()   # same reasoning as GuideLabelStrip's mouseMoveEvent above

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton and self._pan_last_pos is not None:
            self._pan_last_pos = None
            self.unsetCursor()
            return
        self._dragging = False
        self._drag_last_x = None
        self._drag_accum_x = None
        self.update()


class SliderLegend(QWidget):
    """Tick labels under a percentage slider, positioned proportionally
    to where each value actually falls in the slider's range -- rather
    than naively evenly-spaced text, which drifts out of sync with the
    actual handle position unless the range happens to be symmetric.
    Shared by the Weight and Flatten sliders -- generic on min/max/tick
    values, nothing specific to either one.

    abs_labels shows each tick's DISTANCE from the slider's own zero
    point rather than its raw (possibly negative) value -- for
    Flatten/Stretch specifically, where the slider's position itself
    already conveys which side (Flatten vs. Stretch) a tick belongs
    to, so a plain, unsigned "50%" reads correctly there without also
    needing a minus sign to disambiguate a slider whose two directions
    already have their own distinct labels doing that job."""

    def __init__(self, slider_min: int, slider_max: int, ticks: list[int],
                 parent=None, abs_labels: bool = False):
        super().__init__(parent)
        self.slider_min = slider_min
        self.slider_max = slider_max
        self.ticks = ticks
        self.abs_labels = abs_labels
        self.setFixedHeight(16)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setPen(QPen(QColor("#666666")))
        span = self.slider_max - self.slider_min
        for v in self.ticks:
            frac = (v - self.slider_min) / span
            x = frac * self.width()
            text = f"{abs(v) if self.abs_labels else v}%"
            metrics = p.fontMetrics()
            tw = metrics.horizontalAdvance(text)
            # clamp so end labels don't clip past the widget edges
            tx = min(max(x - tw / 2, 0), self.width() - tw)
            p.drawText(int(tx), 12, text)
        p.end()


class CenterPanel(QWidget):
    """Wraps the ruler+strip+canvas grid and keeps the CANVAS portion's
    displayed size matched to the glyph content's own aspect ratio.

    Without this, QGraphicsView's own fitInView(KeepAspectRatio)
    letterboxes INSIDE the canvas widget whenever the widget's aspect
    ratio doesn't match the content's -- which is almost always, since
    the widget just stretches to fill whatever space the splitter gives
    it. The adjacent ruler/strip widgets are sized to that FULL widget,
    not to the smaller letterboxed content within it, so they end up
    visually far from the actual glyph -- worse the more the window's
    proportions diverge from the glyph's own, which is exactly why
    resizing (especially to full-screen) made the gap worse rather
    than better.

    This keeps the same rulers/strips/canvas grid, just resizes the
    WHOLE grid (not just the canvas) to match the content's aspect
    ratio and centers it -- so any necessary "letterboxing" becomes
    plain background OUTSIDE the grid, where the strips are still snug
    against the canvas, rather than hidden empty space INSIDE the
    canvas widget that the adjacent strips have no way to account for.

    Has no layout of its own -- the wrapped grid widget's geometry is
    set directly in _reflow(), the standard Qt pattern for custom
    positioning logic a QLayout can't easily express."""

    # matches the fixed-size ruler/strip margins around the canvas
    LEFT = 38     # VerticalRuler width
    RIGHT = 90    # GuideLabelStrip width
    TOP = 20      # Ruler height
    BOTTOM = 32   # AdvanceLabelStrip height

    def __init__(self, grid_widget: QWidget, canvas, parent=None):
        super().__init__(parent)
        self.grid_widget = grid_widget
        self.canvas = canvas
        self.grid_widget.setParent(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def _content_aspect(self) -> float | None:
        if self.canvas.box_rect is None:
            return None
        x0, y0, x1, y1 = self.canvas.box_rect
        m = self.canvas.margin_width or 0.0
        w = (x1 - x0) + 2 * m + 20   # matches load_glyph's setSceneRect padding
        h = (y1 - y0) + 2 * m + 20
        return w / h if h > 0 else None

    def reflow(self):
        """Recompute the grid's size/position -- called on resize, and
        also explicitly whenever the loaded glyph/project changes or
        the zoom level changes, since none of those happen on their
        own via a resize event.

        Aspect-locking only makes sense exactly AT fit-to-window: that's
        the one state where the full content needs to be visible
        without cropping, which is what actually requires matching the
        content's own aspect ratio to avoid internal letterboxing.
        Once zoomed in, the canvas should grow -- but PROPORTIONALLY
        with the zoom level, not as an instant jump to full size the
        moment zoom exceeds 1.0. A modest zoom (say 1.14x) only makes
        the content occupy about 1.14x its fit-to-window size; jumping
        the widget straight to full available width at that same
        moment asks it to show more scene than exists at that zoom
        level, which is exactly what showed up as dead space between
        the content and the strips. Scaling canvas size with zoom
        keeps the widget only ever as big as the content actually
        fills, capped at the available space once zoom is high enough
        to warrant using all of it."""
        avail_w = max(self.width() - self.LEFT - self.RIGHT, 1)
        avail_h = max(self.height() - self.TOP - self.BOTTOM, 1)
        aspect = self._content_aspect()

        if not aspect or aspect <= 0:
            canvas_w, canvas_h = avail_w, avail_h
        else:
            if avail_w / avail_h > aspect:
                base_h = avail_h
                base_w = avail_h * aspect
            else:
                base_w = avail_w
                base_h = avail_w / aspect
            zoom = max(self.canvas._zoom_level, self.canvas.MIN_ZOOM)
            canvas_w = min(avail_w, base_w * zoom)
            canvas_h = min(avail_h, base_h * zoom)

        total_w = canvas_w + self.LEFT + self.RIGHT
        total_h = canvas_h + self.TOP + self.BOTTOM
        x_off = (self.width() - total_w) / 2
        y_off = (self.height() - total_h) / 2
        self.grid_widget.setGeometry(round(x_off), round(y_off), round(total_w), round(total_h))


def effective_canvas_size_range() -> tuple[int, int, str]:
    """(min_dim, effective_max, hint_text) -- shared by every place a
    canvas width/height can be typed (NewProjectDialog, and the
    canvas-size prompts used when a capture-driven project reaches the
    end of a session with no real size established yet -- see
    MainWindow._prompt_canvas_size), so the same limits and the same
    explanation of them apply everywhere, not just wherever this was
    first written.

    The maximum itself is dynamic, not a flat MAX_DIM: the box gets
    surrounded by a margin reserved for weight-overshoot rendering (up
    to 30% weight, scaled from the box's own cap height), so a canvas
    entered right at the hard ceiling would leave no room for that
    margin at all -- solving h + max_half_width(h) = MAX_DIM for h,
    since the margin itself scales with whatever height ends up
    chosen, not a fixed worst case."""
    max_dim = 4096
    min_dim = 32
    # weight_slider's own max (30%) and the cap-height fraction of
    # canvas height new_blank actually uses (baseline_frac -
    # cap_top_frac from this project's own calibrated dataset) --
    # hardcoded here rather than imported, since this can run before
    # any Project/canvas exists to read them from.
    max_weight_ratio = 0.30
    cap_height_fraction = 0.7654223968565815 - 0.0019646365422396855
    divisor = 1 + (max_weight_ratio * cap_height_fraction / 2)
    effective_max = int(max_dim / divisor)
    hint = (f"Width and Height can range from {min_dim} to {effective_max}px\n"
            f"({max_dim} - maximum weight overshoot)\n"
            f"The optimal height range is {OPTIMAL_RANGE_PX[0]}-{OPTIMAL_RANGE_PX[1]}px.")
    return min_dim, effective_max, hint


class NewProjectDialog(QDialog):
    """Name and canvas dimensions for a from-scratch project, combined
    into one form -- previously three separate popups in sequence
    (name, then width, then height), with the 4096px maximum never
    actually stated anywhere the user could see it before hitting it.

    The maximum itself is dynamic, not a flat 4096: the box gets
    surrounded by a margin reserved for weight-overshoot rendering (up
    to 30% weight, scaled from the box's own cap height), so a canvas
    entered right at the hard ceiling would leave no room for that
    margin at all. Rather than let the user hit 4096, find it doesn't
    actually work at full weight, and have to guess why, the spinner's
    own maximum -- and the hint text -- account for that margin
    up front. See effective_canvas_size_range for the actual formula,
    shared with every other place a canvas size can be typed."""

    def __init__(self, parent=None, size_label: str = "Canvas", show_size_fields: bool = True):
        super().__init__(parent)
        self.setWindowTitle("New Project")
        self.name: str = ""
        self.chosen_width: int = 300
        self.chosen_height: int = 500
        self.chosen_folder: str = ""
        self._folder_name_manually_edited = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("Project name:", self.name_edit)

        # Folder location merged directly into this dialog -- a
        # previous version showed this as a SEPARATE screen
        # (ProjectFolderDialog) after this one closed. Folded in since
        # the two are directly related (the folder name defaults to
        # the project name) and splitting them cost an extra click-
        # through for no real benefit.
        parent_row = QHBoxLayout()
        self.parent_edit = QLineEdit()
        parent_row.addWidget(self.parent_edit, 1)
        browse_btn = QPushButton("Browse\u2026")
        browse_btn.clicked.connect(self._browse_parent)
        parent_row.addWidget(browse_btn)
        form.addRow("Parent folder:", parent_row)

        self.folder_name_edit = QLineEdit()
        form.addRow("Folder name:", self.folder_name_edit)

        # No width/height fields at all here (not just hidden behind a
        # checkbox -- that checkbox itself is gone too, see below)
        # whenever this project is about to have a real glyph coming
        # right after this dialog closes -- capture-from-scratch and
        # _add_blank_glyph's own "no project yet" path both qualify.
        # Confirmed as a deliberate removal, not an oversight: the user
        # can never actually know the right size at THIS point --
        # that's the whole reason every from-scratch canvas
        # finalization now runs its own dedicated review-and-edit
        # prompt once a real capture/file/largest-of-several is
        # actually known (see CanvasSizePromptDialog) -- so asking here
        # first, whether behind a checkbox or not, never served any
        # purpose these two entry points could actually use. The plain
        # "New Project" toolbar action (_new_blank_project) is
        # genuinely different: nothing else is coming right after it to
        # ever determine a size, so it still needs one specified here,
        # same as before.
        if show_size_fields:
            self.width_spin = QSpinBox()
            form.addRow(f"{size_label} width (px):", self.width_spin)
            self.height_spin = QSpinBox()
            form.addRow(f"{size_label} height (px):", self.height_spin)
        else:
            self.width_spin = None
            self.height_spin = None
        layout.addLayout(form)

        self.hint = QLabel()
        self.hint.setStyleSheet("color: #777777;")
        if show_size_fields:
            layout.addWidget(self.hint)

        self.preview_label = QLabel()
        self.preview_label.setStyleSheet("color: #777777;")
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)

        if show_size_fields:
            # Range must be established (via _update_max, since it's
            # dynamic) BEFORE setting these -- QSpinBox defaults to
            # [0, 99] until setRange is called, so setting any value
            # any earlier would silently clamp it down to 99.
            self._update_max()
            self.width_spin.setValue(self.chosen_width)
            self.height_spin.setValue(self.chosen_height)
            self.height_spin.valueChanged.connect(self._update_max)

        # Defaults to the last folder browsed anywhere in the app (see
        # gui/app_settings.py -- one shared value across every folder/
        # file dialog, not a separate one per dialog), falling back to
        # the home directory the very first time there's nothing to
        # remember yet.
        self.parent_edit.setText(app_settings.get_last_browsed_dir() or os.path.expanduser("~"))
        self.name_edit.textChanged.connect(self._on_name_changed)
        self.folder_name_edit.textEdited.connect(self._on_folder_name_manually_edited)
        self.parent_edit.textChanged.connect(self._update_preview_and_enabled)
        self.folder_name_edit.textChanged.connect(self._update_preview_and_enabled)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_preview_and_enabled()

    def _on_name_changed(self, text: str):
        # Auto-fills the folder name from the project name -- the
        # classic "slug until manually overridden" pattern: keeps
        # following the project name right up until the user actually
        # types into the folder-name field themselves (see
        # _on_folder_name_manually_edited), at which point their own
        # choice takes over permanently for the rest of this dialog's
        # lifetime, even if they go back and change the project name
        # again afterward.
        if not self._folder_name_manually_edited:
            self.folder_name_edit.setText(text)

    def _on_folder_name_manually_edited(self, _text: str):
        self._folder_name_manually_edited = True

    def _browse_parent(self):
        chosen = QFileDialog.getExistingDirectory(self, "Choose parent folder", self.parent_edit.text())
        if chosen:
            chosen = app_settings.normalize_path(chosen)
            self.parent_edit.setText(chosen)
            app_settings.remember_last_browsed_dir(chosen)

    def _update_preview_and_enabled(self):
        parent = self.parent_edit.text().strip()
        folder_name = self.folder_name_edit.text().strip()
        name_ok = bool(self.name_edit.text().strip())
        folder_ok = bool(parent) and bool(folder_name) and os.path.isdir(parent)
        self.ok_button.setEnabled(name_ok and folder_ok)
        # Only ever shows a SHORT guidance/error message -- never the
        # resulting full path. Confirmed as a real, reported layout
        # bug otherwise: that path can run long and word-wraps onto
        # multiple lines as it's typed, and this dialog has no scroll
        # area to absorb the growth -- the whole form above visibly
        # compressed to make room, at one point squeezing the Browse
        # button below its own text's height. Dropped rather than
        # given more vertical room, since the parent-folder and folder-
        # name fields already show everything a resulting path would
        # -- repeating it here added no real information.
        if not parent:
            self.preview_label.setText("Choose a parent folder above.")
        elif not os.path.isdir(parent):
            self.preview_label.setText(f'"{parent}" doesn\'t exist.')
        elif not folder_name:
            self.preview_label.setText("Enter a folder name.")
        else:
            self.chosen_folder = os.path.join(parent, folder_name)
            self.preview_label.setText("")

    def _update_max(self):
        """Recomputes the effective maximum from the CURRENT height --
        see effective_canvas_size_range's own docstring for the
        reasoning; this dialog just applies it to its own spinners.
        A no-op when there are none to apply it to (show_size_fields
        was False)."""
        if self.width_spin is None:
            return
        min_dim, effective_max, hint = effective_canvas_size_range()
        for spin in (self.width_spin, self.height_spin):
            spin.blockSignals(True)
            spin.setRange(min_dim, effective_max)
            spin.blockSignals(False)
        self.hint.setText(hint)

    def _on_accept(self):
        name = self.name_edit.text().strip()
        if not name:
            # Stays open, same reasoning as AddGlyphDialog: the user
            # corrects it in place rather than the dialog closing and
            # needing to be reopened with the width/height re-entered.
            QMessageBox.warning(self, "Missing name", "Enter a project name.")
            return
        if self.width_spin is None:
            # No size fields at all -- see __init__'s own comment on
            # why. chosen_width/height simply stay at their __init__
            # defaults (300x500); the caller is expected to finalize
            # the REAL size later, once it's actually known.
            self.name = name
            self.accept()
            return
        width, height = self.width_spin.value(), self.height_spin.value()
        # Same reasoning as _import_folder's own check (see
        # _check_canvas_size_limits) -- MAX_DIM above is this dialog's
        # own technical ceiling (what still leaves room for full-weight
        # overshoot margin), not a statement about what canvas size
        # actually suits PNGlyph's intended, modest scale of work.
        # Declining here returns to this SAME open dialog, not out of
        # the whole New Project flow, so adjusting width/height doesn't
        # cost the name already entered.
        if not _check_canvas_size_limits(self, width, height):
            return
        self.name = name
        self.chosen_width = width
        self.chosen_height = height
        self.accept()


class CanvasSizePromptDialog(QDialog):
    """Shown whenever a from-scratch project's canvas size is about to
    be finalized and the user should get a chance to review or adjust
    it first -- replaces the earlier "silently finalize, only warn if
    the result is too small" behavior entirely (see
    _confirm_tiny_capture_size, no longer used by any caller): every
    path that finalizes a from-scratch canvas now shows this instead,
    pre-filled with whatever size WOULD be used (a specific file/
    capture's own dimensions, the largest capture in a queue, or the
    hardcoded 300x500 fallback when nothing else is available yet),
    always editable, with the same width/height range New Project
    itself uses (see effective_canvas_size_range)."""

    def __init__(self, parent, message: str, default_width: int, default_height: int):
        super().__init__(parent)
        self.setWindowTitle("Canvas size")
        # Confirmed as a real, reported bug otherwise: this can be
        # shown while the capture window is still up, which has
        # WindowStaysOnTopHint set (see capture/capture_window.py) --
        # without the same flag here, a perfectly normal dialog renders
        # BELOW it regardless of z-order or raise() calls, effectively
        # hidden behind a window that's still on screen. Same fix
        # already applied to the overwrite-conflict confirmation inside
        # the capture window itself, for the identical reason.
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        layout = QVBoxLayout(self)

        label = QLabel(message)
        label.setWordWrap(True)
        layout.addWidget(label)

        form = QFormLayout()
        min_dim, max_dim, hint = effective_canvas_size_range()
        self.width_spin = QSpinBox()
        self.height_spin = QSpinBox()
        for spin, default in ((self.width_spin, default_width), (self.height_spin, default_height)):
            spin.setRange(min_dim, max_dim)
            # Clamped, not just handed the raw value -- a real capture/
            # file could exceed the range New Project itself enforces,
            # and setValue() on a QSpinBox silently clamps anyway, so
            # this just makes that explicit rather than relying on it.
            spin.setValue(max(min_dim, min(max_dim, default)))
        form.addRow("Width:", self.width_spin)
        form.addRow("Height:", self.height_spin)
        layout.addLayout(form)

        hint_label = QLabel(hint)
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ProjectNameDialog(QDialog):
    """Shown the FIRST time a project is ever saved (see
    MainWindow._save_project's own check of _saved_project_path) --
    only a NAME is asked for, never a full path or a folder to browse
    to, since the .json is now required to live in the same folder as
    its own PNGs (source_dir): there's no other valid location to even
    offer. Every SUBSEQUENT save silently reuses whichever path this
    established, with no dialog at all -- this is a one-time,
    first-save naming step, not a repeating "Save As" prompt.

    default_name: the enclosing folder's own name, pre-filled -- for a
    brand-new project (New Project) or a freshly-imported one (Import
    folder), that's normally already the closest thing to an intended
    project name that exists yet."""

    def __init__(self, parent, source_dir: str, default_name: str):
        super().__init__(parent)
        self.setWindowTitle("Name this project")
        self.result_name: str | None = None

        layout = QVBoxLayout(self)
        label = QLabel("This project's file will be saved alongside its "
                        "own source images, so only a name is needed:")
        label.setWordWrap(True)
        layout.addWidget(label)

        form = QFormLayout()
        self.name_edit = QLineEdit(default_name)
        form.addRow("Name:", self.name_edit)
        layout.addLayout(form)

        self._location_label = QLabel()
        self._location_label.setWordWrap(True)
        self._location_label.setStyleSheet("color: #666666;")
        layout.addWidget(self._location_label)

        self._error_label = QLabel("")
        self._error_label.setStyleSheet("color: #b23333;")
        layout.addWidget(self._error_label)

        self._source_dir = source_dir
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok_btn = buttons.button(QDialogButtonBox.Ok)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.name_edit.textChanged.connect(self._validate)
        self.name_edit.selectAll()
        self._validate(default_name)

    def _validate(self, text: str) -> None:
        name = text.strip()
        # Same invalid-character set Windows/macOS/Linux all reject in
        # a filename, checked up front rather than letting the actual
        # save attempt fail afterward with a lower-level OS error.
        invalid = set('<>:"/\\|?*')
        bad_chars = invalid & set(name)
        if not name:
            self._error_label.setText("Enter a name.")
            self._ok_btn.setEnabled(False)
        elif bad_chars:
            self._error_label.setText(f"Can't contain: {' '.join(sorted(bad_chars))}")
            self._ok_btn.setEnabled(False)
        else:
            self._error_label.setText("")
            self._ok_btn.setEnabled(True)
        self._location_label.setText(
            f"Will be saved as: {os.path.join(self._source_dir, name or '…')}.pnglyph.json")

    def _on_accept(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            return
        self.result_name = name
        self.accept()

    @staticmethod
    def get_name(parent, source_dir: str, default_name: str) -> str | None:
        dlg = ProjectNameDialog(parent, source_dir, default_name)
        if dlg.exec() == QDialog.Accepted:
            return dlg.result_name
        return None


class AddGlyphDialog(QDialog):
    """Character/codepoint entry (for Blank glyph only -- see below),
    plus loading one or more source PNGs for new glyphs -- distinct
    from the bulk u<hex>.png folder scan. A custom dialog rather than
    plain QInputDialog.getText because Blank glyph needs a validation
    failure that returns the user to the SAME open dialog to correct
    their input, rather than closing and forcing them to reopen the
    whole thing from scratch.

    File... is always enabled, regardless of what (if anything) is
    typed into the character field -- confirmed as the right design,
    not an oversight: asking for a codepoint BEFORE the user has even
    picked a file (or files) forces a decision before there's anything
    to make it with any precision against, and for a multi-file pick
    it was never even usable in the first place (only the very first
    file could ever have benefited from it). The codepoint is asked
    for AFTER the pick instead, via MultiFileAssignmentDialog -- with
    a real thumbnail on screen, for one file exactly the same way as
    for many. Capture was removed from here entirely: Capture Glyphs
    already covers a single capture at least as well (build → assign →
    review, in the correct order, already in place), so duplicating
    that flow here a second time added complexity without adding
    anything Capture Glyphs didn't already do. Blank glyph is the one
    remaining case that still needs its codepoint upfront -- there's
    no file/capture content to preview that would make "ask
    afterward" mean anything for it, since a blank glyph's own
    content doesn't exist yet to look at."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add a new glyph")
        self.file_path: str | None = None
        self.codepoint: int | None = None
        # Set (instead of self.codepoint/self.file_path) whenever
        # File... is used at all -- one file or several, always
        # routed the same way now (see _browse_file's own comment).
        # This dialog closes itself via reject() once set, and
        # MainWindow reads this after exec() returns to route into
        # MultiFileAssignmentDialog instead of the ordinary Blank-glyph
        # commit path below.
        self.multi_file_paths: list[str] | None = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.char_edit = CodepointLineEdit()
        form.addRow("Character or U+XXXX codepoint (Blank glyph only):", self.char_edit)
        layout.addLayout(form)

        # File... is always enabled (see this class's own docstring for
        # why); Blank glyph alone still needs a valid codepoint typed
        # in first, since there's no preview to ask about it afterward
        # the way there is for a real file. No status label here --
        # both actions close this dialog immediately with their own
        # outcome, so there's no remaining moment for a "file picked:
        # ..." label to actually be seen before the dialog vanishes.
        file_row = QHBoxLayout()
        self.file_btn = QPushButton("File…")
        self.blank_btn = QPushButton("Blank glyph")
        self.blank_btn.setEnabled(False)
        self.blank_btn.setToolTip("Enter a character or codepoint above first")
        file_row.addStretch(1)
        file_row.addWidget(self.file_btn)
        file_row.addWidget(self.blank_btn)
        layout.addLayout(file_row)

        self.char_edit.textChanged.connect(self._update_blank_button_enabled)
        self.file_btn.clicked.connect(self._browse_file)
        self.blank_btn.clicked.connect(self._on_blank_glyph_clicked)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _parse_codepoint(text: str) -> tuple[int | None, str | None]:
        """Returns (codepoint, None) on success, (None, error message)
        on failure, or (None, None) for an empty/not-yet-typed field --
        that last case isn't an error, just "nothing entered yet". Now a
        thin delegate to capture.codepoint_parse.parse_codepoint, so the
        capture-session assignment window validates identically rather
        than carrying a second, possibly-drifting copy of this logic."""
        return _parse_capture_codepoint(text)

    def _update_blank_button_enabled(self, text: str):
        cp, _err = self._parse_codepoint(text.strip())
        enabled = cp is not None
        self.blank_btn.setEnabled(enabled)
        self.blank_btn.setToolTip("" if enabled else "Enter a character or codepoint above first")

    def _browse_file(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose source PNG(s)", app_settings.get_last_browsed_dir(), filter="PNG images (*.png)")
        if not paths:
            return   # cancelled the file picker -- stay in this dialog
        paths = [app_settings.normalize_path(p) for p in paths]
        app_settings.remember_last_browsed_dir(paths[0])
        # Always routed through MultiFileAssignmentDialog now, one file
        # or many alike -- see MainWindow's own handling of
        # multi_file_paths. No prefill from char_edit passed along
        # either: that field is now scoped to Blank glyph alone (see
        # this class's own docstring), so it plays no role here even
        # for a single file.
        self.multi_file_paths = paths
        self.reject()

    def _on_blank_glyph_clicked(self):
        text = self.char_edit.text().strip()
        cp, err = self._parse_codepoint(text)
        if not text or cp is None:
            # Stays open on invalid input -- no accept()/close() here --
            # so the user corrects it in place instead of the whole
            # dialog vanishing and needing to be reopened from scratch.
            # Shouldn't normally be reachable (the button is disabled
            # until this is valid -- see _update_blank_button_enabled),
            # but don't proceed on a guess if it somehow is.
            QMessageBox.warning(self, "Invalid input",
                                 err or "Enter a single character or a U+XXXX codepoint.")
            return
        self.codepoint = cp
        self.file_path = None
        self.accept()


@_dataclass
class _PendingFileItem:
    """Mirrors just enough of capture.manifest.CaptureItem's own shape
    (seq, filename, codepoint) for _CaptureRow to work completely
    unmodified against a plain, already-on-disk file instead of a
    captured screenshot -- see MultiFileAssignmentDialog's own
    docstring for why nothing else about CaptureItem/CaptureSessionStore
    carries over here at all."""
    seq: int
    filename: str
    codepoint: int | None
    file_path: str   # not read by _CaptureRow itself, but needed by MultiFileAssignmentDialog to resolve the real source file at commit time


def _apply_import_scale_and_edits(paths, recipe):
    """Applies recipe's own structural import_scale first (see
    GlyphRecipe.import_scale's own docstring for why this is tracked
    separately from edits at all), then every recorded edit in order
    -- the exact position import_scale always occupied as the first
    entry in a freshly-imported glyph's own edits list, before being
    split out specifically to stop it from triggering the "edited"
    marker (see MainWindow._refresh_glyph_icon's own check of
    recipe.edits) for a resize the user never actually asked for or
    touched themselves.

    Shared by every place that traces a glyph's own source PNG and
    then needs its edits applied on top -- _load_glyph_into_canvas,
    _refresh_glyph_icon, and onion skin's own _build_glyph_paths --
    rather than duplicating this same two-step sequence at each one
    separately."""
    if recipe.import_scale != 1.0:
        paths = geometry.scale_uniform(paths, factor=recipe.import_scale, origin=[0.0, 0.0])
    for step in recipe.edits:
        paths = getattr(geometry, step.op)(paths, **step.args)
    return paths


def _guess_codepoint_from_filename(filename: str) -> int | None:
    """Best-effort codepoint guess from a filename's own stem -- ONLY
    the u<hex>.png convention used throughout this app already
    (Project.scan_source_dir, staging, etc.), never a bare single
    character or hex string on its own. Confirmed as the right scope,
    not an oversight: the u<hex> convention is this app's own,
    explicit, unambiguous naming scheme -- there's no other reasonable
    reading of "u0041.png". A single-letter filename like "A.png"
    doesn't carry that same certainty (it could just as easily be a
    sequential scan label, an unrelated placeholder, or something else
    entirely), so it's deliberately NOT guessed at all -- that row
    starts blank, entered manually, same as any other file whose name
    doesn't encode a codepoint at all.

    Returns None (not an error) when the file isn't named this way --
    that row simply starts blank, same as every row always did before
    this guess existed."""
    stem = os.path.splitext(filename)[0]
    if len(stem) > 1 and stem[0].lower() == "u":
        try:
            return int(stem[1:], 16)
        except ValueError:
            pass
    return None


class MultiFileAssignmentDialog(QDialog):
    """Codepoint assignment for one or more already-existing PNG files
    picked via "Add a new glyph -> File..." (see AddGlyphDialog.
    _browse_file, which now always routes here -- a single file is
    just a one-row instance of the same thing, not a separate path).
    Reuses _CaptureRow -- the SAME row widget the capture-session
    assignment window uses (thumbnail, editable codepoint field with
    live validation, delete) -- since the per-row interaction is
    identical either way, but deliberately WITHOUT CaptureSessionStore
    underneath it: these files already exist wherever they are on
    disk, nothing about a plain file selection is "in progress" or
    needs resuming/discarding across a crash the way an in-progress
    capture session genuinely does.

    Modal (.exec()), not the capture assignment window's own non-modal
    .show() -- this is a one-shot batch tied to a single "Add a new
    glyph" action, not a session that can be left open, suspended, and
    resumed later the way a multi-capture run can. Nothing is written
    to the project here at all; get_assignments() returns a resolved
    {codepoint: file_path} mapping once accepted, and the caller (see
    MainWindow's own handling in _add_blank_glyph) is responsible for
    actually adding each glyph through the same _commit_new_glyph path
    every other add-glyph flow already uses -- staging-aware, undo-
    aware, with no separate, parallel logic of its own to keep in sync."""

    def __init__(self, file_paths: list[str], existing_codepoints_cb, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Assign codepoints")
        self.resize(420, 480)
        self._existing_codepoints_cb = existing_codepoints_cb
        self._resolved_assignments: dict[int, str] = {}

        layout = QVBoxLayout(self)
        intro = QLabel(f"Assign a codepoint to each of the {len(file_paths)} selected files:")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.addStretch(1)
        self._scroll.setWidget(self._rows_widget)
        layout.addWidget(self._scroll, 1)

        self._rows: dict[int, _CaptureRow] = {}
        self._file_paths: dict[int, str] = {}

        # Constructed here, before the row-building loop below (which
        # needs it -- see the explicit _on_row_commit calls inside it),
        # even though it isn't ADDED to the layout until after that loop
        # (its visual position, right below the row list, is unrelated
        # to when the widget object itself needs to already exist).
        # Unchecked every time -- same reasoning as the capture
        # assignment window's own identical checkbox: a conscious,
        # per-batch opt-in, not a remembered preference.
        self._overwrite_check = QCheckBox("Overwrite existing glyphs without asking")
        self._overwrite_check.setToolTip(
            "When checked, assigning a codepoint that already exists in\n"
            "this project replaces it immediately instead of asking each\n"
            "time.")

        # Guessed from each file's own name (u<hex>.png ONLY -- see
        # _guess_codepoint_from_filename's own docstring for why a bare
        # single character or hex string is deliberately NOT guessed at
        # all). AddGlyphDialog's own character field plays no role here
        # at all anymore -- that field is scoped to Blank glyph alone
        # now (see AddGlyphDialog's own docstring), so every row here,
        # including the first, starts purely from its own filename,
        # with nothing else to fall back on.
        guessed = [_guess_codepoint_from_filename(os.path.basename(p)) for p in file_paths]

        # Duplicate guesses are cleared back to blank rather than
        # silently letting one collide with (and overwrite) another --
        # e.g. two files whose names both happen to resolve to the same
        # codepoint. Left for the user to resolve by hand, the same way
        # a manually-typed duplicate already would be (see
        # _on_row_commit's own identical check for that case).
        seen_counts: dict[int, int] = {}
        for cp in guessed:
            if cp is not None:
                seen_counts[cp] = seen_counts.get(cp, 0) + 1
        guessed = [cp if cp is None or seen_counts[cp] == 1 else None for cp in guessed]

        # Sorted once, here, never re-sorted afterward as the user
        # assigns/edits codepoints by hand -- re-sorting rows out from
        # under an in-progress edit would be disorienting, not helpful.
        # Recognized files (a real, still-unique guess survived the
        # duplicate-clearing above) come first, ordered by their own
        # NUMERIC codepoint value -- not the raw filename string, which
        # could misorder differently-length hex strings (e.g. "u9.png"
        # sorting after "u10.png" lexicographically despite 9 < 16).
        # Everything else follows, ordered by filename alone (case-
        # insensitive), since there's no codepoint to sort those by at
        # all. A file whose guess got cleared for colliding with
        # another one's is sorted as "unrecognized" here too, matching
        # what's actually displayed for it (blank), not where its own,
        # since-discarded guess would have placed it.
        order = sorted(
            range(len(file_paths)),
            key=lambda i: (0, guessed[i]) if guessed[i] is not None
                          else (1, os.path.basename(file_paths[i]).lower()))

        for seq, i in enumerate(order):
            path = file_paths[i]
            item = _PendingFileItem(seq=seq, filename=os.path.basename(path), codepoint=guessed[i], file_path=path)
            self._add_row(item)
            if guessed[i] is not None:
                # editingFinished never fires for a value set
                # programmatically at construction time, only for an
                # actual manual edit -- run the SAME existing-glyph
                # overwrite confirmation here explicitly, so an
                # auto-guessed codepoint that already exists in the
                # project gets exactly the same confirmation a
                # manually-typed one would, rather than silently
                # overwriting it the moment Add All is clicked.
                self._on_row_commit(seq)

        layout.addWidget(self._overwrite_check)

        btn_row = QHBoxLayout()
        self._add_btn = QPushButton("Add All Assigned")
        self._add_btn.clicked.connect(self._on_add_all)
        btn_row.addWidget(self._add_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _add_row(self, item: _PendingFileItem) -> None:
        # item.file_path doubles as its own thumbnail source -- these
        # are already real PNGs on disk, not captured screenshots
        # living in a separate session folder the way _CaptureRow
        # normally expects a distinct thumb_path for.
        row = _CaptureRow(item, item.file_path)
        row.delete_requested.connect(self._on_delete_requested)
        row.commit_requested.connect(self._on_row_commit)
        self._rows[item.seq] = row
        self._file_paths[item.seq] = item.file_path
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)

    def _on_delete_requested(self, seq: int) -> None:
        row = self._rows.pop(seq, None)
        self._file_paths.pop(seq, None)
        if row is not None:
            row.setParent(None)
            row.deleteLater()

    def _on_row_commit(self, seq: int) -> None:
        """Same conflict-checking logic as CaptureWindow's own
        _on_row_commit -- confirmed here, once, at the moment each
        field is actually finished (not per keystroke), rather than as
        a burst of separate popups at Add-All time for however many
        conflicts happened to be typed in."""
        row = self._rows.get(seq)
        if row is None:
            return
        cp = row.current_codepoint()
        if cp is None:
            return   # empty or invalid entry -- nothing to confirm
        for other_seq, other_row in self._rows.items():
            if other_seq != seq and other_row.current_codepoint() == cp:
                QMessageBox.warning(
                    self, "Already assigned this batch",
                    f"U+{cp:04X} is already assigned to another file in this batch.")
                row.revert_codepoint()
                return
        if cp in self._existing_codepoints_cb() and not self._overwrite_check.isChecked():
            confirm = QMessageBox(
                QMessageBox.Question, "Already exists",
                f"U+{cp:04X} is already in this project. Overwrite it when added?",
                QMessageBox.Yes | QMessageBox.No, self)
            confirm.setDefaultButton(QMessageBox.No)
            if confirm.exec() != QMessageBox.Yes:
                row.revert_codepoint()
                return

    def _on_add_all(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "Nothing to add", "No files remaining in this batch.")
            return
        self.setFocus()   # forces any actively-edited field to fire editingFinished (and its own conflict check) first
        unassigned = [seq for seq, row in self._rows.items() if row.current_codepoint() is None]
        if unassigned:
            reply = QMessageBox.question(
                self, "Unassigned files",
                f"{len(unassigned)} file(s) have no codepoint assigned and will be "
                "skipped, not added. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
        self._resolved_assignments = {
            row.current_codepoint(): self._file_paths[seq]
            for seq, row in self._rows.items()
            if row.current_codepoint() is not None
        }
        self.accept()

    def get_assignments(self) -> dict[int, str]:
        """codepoint -> file_path, only for rows that ended up with a
        valid, non-conflicting codepoint assigned. Call only after
        exec() has returned QDialog.Accepted."""
        return self._resolved_assignments


class CollapsedPaneTab(QWidget):
    """A small, named, clickable tab shown floating over a splitter's
    edge whenever the pane it represents is collapsed to zero width.

    Deliberately NOT a QSplitterHandle subclass, after a QSplitterHandle
    subclass that queried its own splitter's widget()/sizes()/indexOf()
    from within paintEvent/mousePressEvent -- even deferred via
    QTimer.singleShot(0, ...) to a fresh event-loop iteration, and even
    though the handle object itself was confirmed to stay valid
    throughout via shiboken6.isValid() -- reproducibly segfaulted.
    Isolated carefully: a bare createHandle() override alone was fine;
    paintEvent/mousePressEvent/mouseMoveEvent overrides that only
    called super() were fine; the crash appeared specifically and only
    once splitter.widget()/sizes()/indexOf() got called from within
    one of a QSplitterHandle subclass's own methods, regardless of
    when. Rather than continue chasing what looks like a PySide6
    binding issue with that specific combination, this sidesteps it
    entirely: an ordinary QWidget, positioned and shown/hidden by
    MainWindow's own regular code (never inside a QSplitterHandle
    subclass), which is the same context every other splitter.sizes()/
    setSizes() call throughout this app has always worked fine in."""

    clicked_signal = Signal()

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._label = label
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(18)
        self.hide()

    def mousePressEvent(self, event):
        self.clicked_signal.emit()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(style.GUIDE_COLOR))
        p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 4, 4)
        p.save()
        p.translate(w / 2, h / 2)
        p.rotate(-90)
        p.setPen(QColor("#ffffff"))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(self._label)
        p.drawText(QPointF(-tw / 2, fm.ascent() / 2), self._label)
        p.restore()
        p.end()


class SplitterGripIndicator(QWidget):
    """A small, always-present vertical row of dots overlaid on a
    splitter handle, visible whenever the pane it sits next to is
    expanded -- the common "this is a drag grip" pattern seen in many
    apps' resizable panels, added because the collapse/resize gesture
    itself had no visual hint beyond a hover-triggered cursor change
    and a tooltip (see the handles' own setToolTip calls).

    An ordinary QWidget synced to its handle's geometry by MainWindow's
    own existing timer poll (_update_collapsed_tabs), painting itself
    directly rather than loading an image -- same reasoning as
    CollapsedPaneTab, immediately above, for why this isn't a
    QSplitterHandle subclass: that specific combination reproducibly
    segfaults (see CollapsedPaneTab's own docstring for the full
    isolation story), while an ordinary overlay widget positioned on
    top of a handle has never had that problem anywhere else in this
    app. A QSS attempt at the same visual (image/background-image on
    QSplitter::handle) was tried first and abandoned: the image
    property didn't reliably render at all, and giving the handle rule
    explicit width/height to try to coax it into rendering instead
    resized the HANDLE itself unpredictably -- worse than not styling
    it at all."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)   # clicks/drags pass through to the real handle underneath
        self.hide()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#a9b3c2"))
        cx = self.width() / 2
        cy = self.height() / 2
        for dy in (-8, 0, 8):
            p.drawEllipse(QPointF(cx, cy + dy), 1.6, 1.6)
        p.end()


# Canvas-size guardrails for BOTH ways a project's canvas size gets
# set -- auto-calibrated from a source PNG folder (_import_folder) and
# manually entered for a from-scratch project (NewProjectDialog). Not
# about what the app CAN technically handle (nothing breaks above or
# below these), but about what PNGlyph is actually FOR: converting
# small, DOS-era-scale bitmap fonts (a large one was rarely more than
# 32x32 per glyph) to TTF, plus a deliberately simple from-scratch
# mode -- straight segments only, no curve/arc tools -- for the same
# modest scale of work, not a general-purpose vector editor competing
# with full-featured ones that already exist for anything larger. A
# canvas far beyond the upper end isn't unsafe, just increasingly
# disconnected from what the tool was actually built and tuned for
# (see, for instance, why snap distance is deliberately NOT
# canvas-size-relative -- docs/snapping.md -- which only stays
# sensible within a bounded range of canvas sizes to begin with). The
# lower end is a different, more concrete risk: tracing genuinely
# produces unreliable results well before it fails outright -- a
# 60x100px real-world capture was confirmed to trace incorrectly, and
# a from-scratch project started this small has no source pixels to
# fall back on at all, so a blocky result only becomes visible after
# the fact, once UPM is raised at export.
#
# SOFT_LOWER_LIMIT_PX/SOFT_UPPER_LIMIT_PX ask for confirmation before
# proceeding; HARD_UPPER_LIMIT_PX refuses outright (there's no
# equivalent hard lower limit -- tracing degrades gradually rather
# than hitting a wall the way an oversized canvas doesn't). The upper
# checks compare against whichever of width/height is LARGER (too
# large if EITHER dimension crosses the line); the lower check compares
# against whichever is SMALLER, the same reasoning in the other
# direction (too small if EITHER dimension is too small, regardless of
# the other -- a 1000x100 canvas is still a 100px problem).
#
# Reference points: 291x509 is PNGlyph's own original Ultiboard set,
# used to calibrate _visual_scale, guide-drag sensitivity, and every
# other canvas-relative constant elsewhere in this app. SOFT_LOWER is
# half that set's own size (rounded); the optimal range is +/-25% of
# an approximate 300x500 rounding of it; SOFT_UPPER/HARD_UPPER predate
# this specific reasoning but were chosen for the same underlying fit.
SOFT_LOWER_LIMIT_PX = 250
OPTIMAL_RANGE_PX = (375, 625)
SOFT_UPPER_LIMIT_PX = 750
HARD_UPPER_LIMIT_PX = 3675


def _check_canvas_size_limits(parent: QWidget, width: int, height: int) -> bool:
    """Returns True if the caller should proceed, False if it should
    abort (hard limit exceeded, or the user declined at either soft
    limit) -- called BEFORE any project state changes in either
    caller, so declining leaves whatever existed before completely
    untouched, not a half-created project."""
    largest = max(width, height)
    smallest = min(width, height)
    # Named explicitly in every message below rather than just showing
    # "WxH" and a limit number side by side -- confirmed as worth being
    # unambiguous about, since the two dimensions can disagree about
    # which limit (if either) actually applies (e.g. a 1000x100 canvas
    # is a soft-lower-limit case on its height alone, even though its
    # width is nowhere near either limit).
    largest_dim = "width" if width >= height else "height"
    smallest_dim = "width" if width <= height else "height"
    if largest > HARD_UPPER_LIMIT_PX:
        QMessageBox.critical(
            parent, "Canvas too large",
            f"This canvas's {largest_dim} ({largest}px) exceeds PNGlyph's hard "
            f"limit of {HARD_UPPER_LIMIT_PX}px. PNGlyph is built for small, "
            f"bitmap-scale glyphs (and a simple from-scratch mode of similar "
            f"scale) -- for artwork this large, a full-featured vector editor is "
            f"a better fit.")
        return False
    if largest > SOFT_UPPER_LIMIT_PX:
        reply = QMessageBox.question(
            parent, "Unusually large canvas",
            f"This canvas's {largest_dim} ({largest}px) is larger than PNGlyph's "
            f"soft upper limit of {SOFT_UPPER_LIMIT_PX}px (hard limit "
            f"{HARD_UPPER_LIMIT_PX}px). It's built around small, DOS-era-scale "
            f"bitmap fonts and a deliberately simple from-scratch mode (straight "
            f"segments only), not general vector artwork -- the optimal range is "
            f"roughly {OPTIMAL_RANGE_PX[0]}-{OPTIMAL_RANGE_PX[1]}px. Continue anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return False
    if smallest < SOFT_LOWER_LIMIT_PX:
        reply = QMessageBox.question(
            parent, "Unusually small canvas",
            f"This canvas's {smallest_dim} ({smallest}px) is smaller than "
            f"PNGlyph's soft lower limit of {SOFT_LOWER_LIMIT_PX}px. "
            f"Low-resolution source images trace unreliably, and a from-scratch "
            f"project started this small will likely look blocky once exported "
            f"at a higher resolution -- the optimal range is roughly "
            f"{OPTIMAL_RANGE_PX[0]}-{OPTIMAL_RANGE_PX[1]}px. Continue anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return False
    return True


class MainWindow(QMainWindow):
    # One toolbar icon's own width (toolbar icons are 20px, set via
    # tb.setIconSize in _build_toolbar -- NOT the same _icon_size() used
    # for the glyph list's own, larger icons) plus its typical button
    # padding/margins -- neither of the two setMinimumWidth call sites
    # below ever accounted for the toolbar's total width at all, only
    # the glyph-list/right panes, so the toolbar could quietly outgrow
    # that floor as icons were added and push the last one (Help) off-
    # window. A fixed, generous margin here is simpler than trying to
    # measure the live toolbar's actual sizeHint() from a constructor
    # context where it may not be fully laid out yet.
    _TOOLBAR_MARGIN = 48

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PNGlyph")
        self.resize(1150, 700)
        # Placeholder floor until a project's glyphs actually exist --
        # replaced with a precise value once the left panel's own
        # fixed width can be measured (see _fix_left_pane_width, which
        # accounts for both fixed panes plus a floor for Weight/
        # Flatten staying at usable slider widths). Kept as a
        # reasonable starting point rather than 0 so the window still
        # behaves sensibly if shown before any project is loaded.
        # +_TOOLBAR_MARGIN: neither this nor _fix_left_pane_width's own
        # recomputation ever accounted for the TOOLBAR's own width --
        # confirmed as the reason the Help icon (last on the toolbar)
        # could get pushed off-window as more toolbar actions were
        # added over time.
        self.setMinimumWidth(900 + self._TOOLBAR_MARGIN)

        self.project: Project | None = None
        # Whether self.project was created fresh (via New Project, or a
        # from-scratch capture/File/Blank flow) in THIS still-running
        # session, as opposed to loaded from an existing .json (Open
        # project, Import folder, or crash recovery resuming a
        # previously-saved project) -- see _create_new_project_via_
        # dialog's own comment for the full reasoning behind what this
        # governs. None (not False) until a project actually exists,
        # so there's never an ambiguous "False, but why" state before
        # then.
        self._project_created_fresh: bool | None = None
        self.current_cp: int | None = None
        # Lazily created, kept alive rather than recreated each time --
        # see _open_help_dialog.
        self._help_dialog: HelpDialog | None = None

        # undo/redo: each entry is (codepoint, edits_snapshot_BEFORE_the_change)
        self._undo_stack: list[tuple[int, list, list]] = []
        self._redo_stack: list[tuple[int, list, list]] = []

        # cp -> already-traced-and-edited paths, so a bulk icon refresh
        # (e.g. on weight-slider release) doesn't need to re-trace every
        # glyph from its source file -- see _refresh_glyph_icon.
        self._paths_cache: dict[int, list] = {}

        # Dirty-tracking for the "unsaved changes?" prompt: a snapshot
        # of the project's serialized state as of the last save/load,
        # compared against the CURRENT serialized state on demand (see
        # _confirm_discard_if_dirty) rather than manually flagging every
        # individual mutation point throughout the app -- there are a
        # lot of them (every edit, every metrics/font change), and
        # missing even one would silently let real changes go
        # unprompted. None means "no project, or never saved/loaded".
        self._saved_snapshot: str | None = None
        # The project's OWN .pnglyph.json path, if it has ever been
        # saved/loaded as one -- distinct from _saved_snapshot (which
        # tracks dirty-vs-clean content, not a filesystem path). Used
        # only by capture/settings.py's crash-pointer, so a resumed
        # session can reload the exact same saved project rather than
        # only reconstructing one via a folder re-scan. None means
        # "never saved" -- perfectly valid; capture works against an
        # unsaved project too.
        self._saved_project_path: str | None = None
        # True only for a project created via the capture-from-scratch
        # flow with "set by first capture" chosen -- see
        # _create_new_project_via_dialog and _finalize_capture_canvas_size.
        self._capture_canvas_pending: bool = False
        # Whatever was in the New Project dialog's own width/height
        # fields at the moment it last closed successfully -- tracked
        # regardless of the "Set canvas size" checkbox's own state, for
        # _add_blank_glyph's own use: a BLANK first glyph (unlike File
        # or Capture) has no real content of its own to ever finalize a
        # still-deferred canvas from, so it falls back to this instead.
        self._last_new_project_field_size: QSize = QSize(300, 500)
        # Manual ALT+click override for the onion-skin reference glyph
        # (see _refresh_onion_skin) -- None means "keep using the
        # automatic nearest-glyph search", the original and still-
        # default behavior. In-memory only, not persisted across app
        # restarts -- reset via the Onion Skin toolbar toggle itself,
        # or whenever a different project loads, clears, or a new one
        # is created (see every self.project = ... assignment site).
        self._onion_skin_override_cp: int | None = None
        # True only while a project was created BY the current capture
        # session itself (start_or_resume's "no project yet" branch)
        # AND nothing has been committed into it yet -- lets a Discard
        # of that same session revert the project entirely rather than
        # leaving a placeholder/finalized canvas behind for a project
        # that only ever existed to serve this one attempt. Cleared the
        # moment anything real happens to it (a successful commit).
        self._project_created_for_capture_session: bool = False

        self._capture_session = CaptureSessionController(self)

        # Bulk-refreshing every glyph's list icon after a weight change
        # is real work (~270ms measured for ~185 glyphs) -- fine once,
        # but the old isSliderDown() check that gated it only knows
        # about an actual mouse-drag on the slider handle, not the +/-
        # buttons (which change the value via plain setValue() calls,
        # so isSliderDown() is always False for them). That meant EVERY
        # tick of a held +/- button re-ran the full refresh -- each one
        # far slower than the button's own repeat interval, so held
        # clicks queued up faster than they could drain and the whole
        # window appeared to lock up. A debounce timer sidesteps needing
        # to know which specific control triggered the change at all:
        # every value change restarts it, so the actual refresh only
        # ever fires once activity has been quiet for a moment,
        # regardless of whether that activity was a drag, a keyboard
        # arrow key, or a held button's auto-repeat.
        self._weight_icon_refresh_timer = QTimer(self)
        self._weight_icon_refresh_timer.setSingleShot(True)
        self._weight_icon_refresh_timer.timeout.connect(self._on_weight_settle)

        # Codepoints whose list icon no longer matches the current
        # weight/cap-style -- populated by _on_weight_settle for every
        # glyph NOT currently visible (see its own docstring for why
        # only the visible ones get refreshed immediately), drained by
        # _on_glyph_list_scroll_settle as previously-stale glyphs
        # scroll into view.
        self._stale_icon_cps: set[int] = set()
        self._scroll_icon_refresh_timer = QTimer(self)
        self._scroll_icon_refresh_timer.setSingleShot(True)
        self._scroll_icon_refresh_timer.timeout.connect(self._on_glyph_list_scroll_settle)

        # (path_indices, deep-copied paths-at-selection-time) captured
        # whenever the selection changes -- Flatten previews recompute
        # from THIS snapshot on every tick, not from whatever the canvas
        # currently shows, so dragging back toward 100% correctly
        # restores the original shape instead of compounding repeated
        # partial flattens on top of each other.
        self._flatten_snapshot: tuple[list[int], list] | None = None
        # The EditStep object (if any) that THIS selection session has
        # been repeatedly updating in place -- lets multiple commits
        # within one session collapse into a single undo step instead
        # of stacking a new one per click/release. Reset to None
        # whenever the selection changes to a genuinely different set
        # of paths (see _on_selection_changed).
        self._flatten_session_edit: EditStep | None = None
        # {'paths': list[list[tuple]], 'groups': list[list[int]]} or
        # None -- in-memory only, not the OS clipboard, since this
        # geometry format has no meaning outside PNGlyph and an
        # OS-level round trip wouldn't add anything.
        self._clipboard: dict | None = None

        self._build_ui()
        self._build_toolbar()

        select_all_action = QAction("Select all segments", self)
        select_all_action.setShortcut(QKeySequence.SelectAll)
        select_all_action.triggered.connect(self._select_all_segments)
        # Scoped to the canvas specifically (not the whole window) --
        # Ctrl+A also needs to mean "select all glyphs in the list"
        # when the glyph list has focus (see the matching action added
        # to self.glyph_list below); leaving this one window-wide would
        # make the two ambiguous and neither would fire reliably.
        select_all_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.canvas.addAction(select_all_action)

        select_all_glyphs_action = QAction("Select all glyphs", self)
        select_all_glyphs_action.setShortcut(QKeySequence.SelectAll)
        select_all_glyphs_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        select_all_glyphs_action.triggered.connect(self.glyph_list.selectAll)
        self.glyph_list.addAction(select_all_glyphs_action)

        # Fixes the glyph browser to its own 3-column width immediately,
        # even with no project loaded yet -- see the method's own
        # docstring for why this can't just wait for _refresh_glyph_list
        # to do it once real glyphs exist. Deferred to the next event-
        # loop iteration, not called directly here -- left_panel's own
        # width isn't meaningful yet this early in __init__, before the
        # window has ever actually been shown/laid out once, and
        # measuring against that would produce nonsense (confirmed: a
        # negative "chrome width").
        self._left_pane_width = 220   # placeholder until the deferred call below measures the real one
        # "Last known good" size for each pane -- see _clamp_pane_
        # widths's own docstring for why this exists (detecting and
        # reversing collateral-damage changes to whichever pane ISN'T
        # the one actually being dragged). setSizes() itself never
        # emits splitterMoved (confirmed directly), so every place
        # that calls it programmatically -- here, _restore_
        # collapsed_pane, _fix_left_pane_width -- updates these two
        # explicitly right afterward; _clamp_pane_widths does the same
        # at the end of every interactive-drag tick it handles.
        self._last_left_size = 220
        self._last_right_size = 240
        QTimer.singleShot(0, self._fix_left_pane_width)
        self.splitter.splitterMoved.connect(self._clamp_pane_widths)

        # Floor set to whatever the window's own natural minimum
        # actually is -- dominated in practice by the sidebar's full
        # stack of boxes (Family through Grid & snap), now that the
        # sidebar itself is a fixed width rather than something that
        # could squeeze narrower to make room. This is what keeps that
        # whole stack showing in full regardless of how short the
        # window gets, rather than reacting per-box to a shortage that
        # a single floor prevents from ever happening in the first
        # place. Computed once, here, after every piece of the UI
        # (including the toolbar) actually exists -- calling this any
        # earlier would under-count whatever hadn't been built yet.
        self.setMinimumHeight(self.minimumSizeHint().height())

        # Runs synchronously here (NOT deferred via QTimer.singleShot,
        # unlike _fix_left_pane_width above) so that any blocking
        # "already recovering elsewhere" message or the resume/discard/
        # later prompt appears BEFORE pnglyph.py's own win.show() call --
        # deferring it would show this window first and only then pop
        # the dialog on the next event-loop tick, a visible flash ahead
        # of what might turn out to be an immediate quit.
        self._check_capture_recovery()

    # -- UI construction ------------------------------------------------

    def _build_ui(self):
        splitter = QSplitter(Qt.Horizontal)
        # Wide enough to comfortably fit the grip icon (see style.py's
        # _ensure_grip_icon) with a little breathing room on each
        # side, and to give the collapse-by-drag gesture a slightly
        # more generous hit target than Qt's own quite thin default.
        splitter.setHandleWidth(10)

        # -- left: glyph browser --
        self.left_panel = left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        self.search_box = QLineEdit(placeholderText="Search glyphs")
        self.search_box.textChanged.connect(self._filter_glyph_list)
        self.glyph_list = QListWidget()
        self.glyph_list.setViewMode(QListWidget.IconMode)
        self.glyph_list.setResizeMode(QListWidget.Adjust)
        self.glyph_list.setGridSize(self.glyph_list.gridSize())
        # ExtendedSelection: Shift/Ctrl-click work like Windows Explorer
        # for multi-selecting glyphs to delete together (see
        # _on_glyph_list_key_event for Delete, and the Ctrl+A action
        # added further below, scoped to this widget specifically).
        self.glyph_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.glyph_list.currentItemChanged.connect(self._on_glyph_selected)
        self.glyph_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.glyph_list.customContextMenuRequested.connect(self._on_glyph_list_context_menu)
        self.glyph_list.installEventFilter(self)
        # Mouse events on a QAbstractItemView are delivered to its
        # VIEWPORT child widget, not the view widget itself -- the
        # install above (needed for keyboard events, which target
        # whichever widget has focus normally) doesn't catch clicks on
        # its own; ALT+click (see eventFilter's own handling) needs
        # this separate install to actually see them.
        self.glyph_list.viewport().installEventFilter(self)
        # Restarts on every scroll tick, same debounce pattern as the
        # weight slider (see _weight_icon_refresh_timer's own setup
        # comment) -- catches up any icons left stale by
        # _on_weight_settle as they scroll into view, without re-
        # rendering on every intermediate pixel of a fast scroll.
        self.glyph_list.verticalScrollBar().valueChanged.connect(
            lambda _v: self._scroll_icon_refresh_timer.start(120))
        left_layout.addWidget(self.search_box)
        left_layout.addWidget(self.glyph_list)
        self.coverage_label = QLabel("No project loaded")
        left_layout.addWidget(self.coverage_label)
        splitter.addWidget(left)

        # -- center: ruler + canvas, using a proper grid so the corner
        # above the vertical ruler is a real spacer, not something the
        # horizontal ruler's own coordinate space silently absorbed --
        # a QVBoxLayout with the horizontal ruler spanning the FULL
        # width, sitting above a row split into [vertical ruler | canvas],
        # meant the horizontal ruler's 0 never actually corresponded to
        # where the canvas starts (it starts 38px to the right, past the
        # vertical ruler) -- exactly why its ticks didn't line up.
        center = QWidget()
        center_layout = QGridLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)

        self.ruler = Ruler()
        self.vertical_ruler = VerticalRuler()
        corner_spacer = QWidget()
        corner_spacer.setFixedSize(self.vertical_ruler.width(), self.ruler.height())

        self.canvas = GlyphCanvas()
        self.canvas.guideChanged.connect(self._on_guide_changed)
        self.canvas.pointCommitted.connect(self._on_point_committed)
        self.canvas.selectionChanged.connect(self._on_selection_changed)
        self.canvas.cursorMoved.connect(self._on_cursor_moved)
        self.canvas.viewChanged.connect(self._on_view_changed)
        self.canvas.pathDrawn.connect(self._on_path_drawn)
        self.canvas.pastePlaced.connect(self._on_paste_placed)
        self.canvas.deleteRequested.connect(self._on_delete_requested)
        self.canvas.segmentContextMenuRequested.connect(self._on_canvas_context_menu_requested)
        self.canvas.offsetPreview.connect(self._on_offset_preview)
        self.canvas.offsetFinished.connect(self._on_offset_finished)
        self.canvas.snapToggleRequested.connect(self._on_snap_toggle_requested)

        self.guide_strip = GuideLabelStrip(self.canvas)
        self.advance_strip = AdvanceLabelStrip(self.canvas)
        corner_tr = QWidget()
        corner_tr.setFixedSize(self.guide_strip.width(), self.ruler.height())
        corner_bl = QWidget()
        corner_bl.setFixedSize(self.vertical_ruler.width(), self.advance_strip.height())
        corner_br = QWidget()
        corner_br.setFixedSize(self.guide_strip.width(), self.advance_strip.height())

        center_layout.addWidget(corner_spacer, 0, 0)
        center_layout.addWidget(self.ruler, 0, 1)
        center_layout.addWidget(corner_tr, 0, 2)
        center_layout.addWidget(self.vertical_ruler, 1, 0)
        center_layout.addWidget(self.canvas, 1, 1)
        center_layout.addWidget(self.guide_strip, 1, 2)
        center_layout.addWidget(corner_bl, 2, 0)
        center_layout.addWidget(self.advance_strip, 2, 1)
        center_layout.addWidget(corner_br, 2, 2)
        self.center_panel = CenterPanel(center, self.canvas)

        # Flatten bar sits directly below the canvas grid, not in the
        # far-away right panel -- it's driven by the SAME selection as
        # on-canvas dragging, so it needs to be where a user's eyes
        # already are while working the selection, not a reach away.
        # A separate row (not inside CenterPanel's own aspect-locked
        # grid) so it doesn't participate in that grid's resize math --
        # it's a fixed-height control strip, not part of the glyph view.
        #
        # Weight moved down here too, alongside Flatten rather than in
        # the right panel: the two are functionally related (a tight
        # clearance can sometimes be solved by EITHER reducing weight
        # or flattening a wobbly part), so keeping them adjacent surfaces
        # that relationship instead of leaving weight a reach away in a
        # separate panel while comparing the two options.
        #
        # The value itself is a compact "[-] value% [+]" group next to
        # each box's own label, rather than a separate text row -- and
        # rather than a spinbox: typing a value is barely faster than
        # dragging the slider (a slider IS already precise enough for
        # that), so +/- buttons for one-step fine-tuning cover the one
        # thing a bare slider can't (no keyboard entry needed either).
        weight_box = QGroupBox("Weight")
        weight_layout = QVBoxLayout(weight_box)
        weight_header = QHBoxLayout()
        self.skeleton_only_check = QCheckBox("Skeleton only")
        self.skeleton_only_check.setToolTip(
            "Show just the skeleton lines, skipping the thickened outline\n"
            "-- faster redraws for a glyph with a lot of points")
        self.skeleton_only_check.toggled.connect(self.canvas.set_skeleton_only)
        self.weight_minus_btn = QPushButton("−")
        self.weight_minus_btn.setObjectName("stepButton")
        self.weight_minus_btn.setFixedWidth(20)   # see Size panel's _step_button for why explicit
        self.weight_value_label = QLabel("10%")
        self.weight_value_label.setObjectName("valuePill")
        self.weight_value_label.setAlignment(Qt.AlignCenter)
        self.weight_value_label.setFixedWidth(44)
        self.weight_plus_btn = QPushButton("+")
        self.weight_plus_btn.setObjectName("stepButton")
        self.weight_plus_btn.setFixedWidth(20)
        # Skeleton only at the far left now that the box's own title
        # moved into its border (see QGroupBox("Weight") above) --
        # there's no longer a separate "Weight" label competing for
        # that same space on this row.
        weight_header.addWidget(self.skeleton_only_check)
        weight_header.addStretch()
        weight_header.addWidget(self.weight_minus_btn)
        weight_header.addWidget(self.weight_value_label)
        weight_header.addWidget(self.weight_plus_btn)
        weight_layout.addLayout(weight_header)

        self.weight_slider = QSlider(Qt.Horizontal)
        # 1% minimum, not 0 -- 0% is a real zero-width stroke, which
        # means no visible ink at all (confirmed: it doesn't crash
        # anymore, but there's nothing useful to see either).
        # 20% as a max wasn't derived from anything -- just a
        # reasonable-looking default when this slider was first built.
        # Widened to 30% for more headroom to experiment with heavier
        # weights; there's no technical reason it couldn't go higher
        # still, but well past this point most glyphs' counters start
        # visibly closing up (the live canvas and clearance warning
        # already make that obvious in practice, so it's self-limiting
        # rather than needing a hard cap).
        self.weight_slider.setRange(1, 30)
        self.weight_slider.setValue(10)
        self.weight_slider.valueChanged.connect(self._on_weight_value_changed)
        self.weight_slider.valueChanged.connect(lambda v: self.weight_value_label.setText(f"{v}%"))
        self.weight_minus_btn.clicked.connect(
            lambda: self.weight_slider.setValue(self.weight_slider.value() - 1))
        self.weight_plus_btn.clicked.connect(
            lambda: self.weight_slider.setValue(self.weight_slider.value() + 1))
        # Auto-repeat while held; the bulk icon refresh this used to
        # trigger via released/sliderReleased is now handled uniformly
        # by the debounce timer in _on_weight_value_changed instead (see
        # its own setup comment) -- that covers every trigger source
        # (drag, keyboard, held buttons) the same way, so no separate
        # release-specific wiring is needed here anymore.
        for btn in (self.weight_minus_btn, self.weight_plus_btn):
            btn.setAutoRepeat(True)
            btn.setAutoRepeatDelay(400)
            btn.setAutoRepeatInterval(60)
        self.weight_legend = SliderLegend(1, 30, [1, 10, 20, 30])
        weight_layout.addWidget(self.weight_slider)
        weight_layout.addWidget(self.weight_legend)

        self.flatten_box = flatten_box = QGroupBox("Size")
        flatten_layout = QVBoxLayout(flatten_box)

        # No wrapping frame -- see _set_flatten_link_visual for how the
        # "linked" state is shown instead (a background tint on the X/Y
        # letters, not a box-around-a-box). Still a QGridLayout, not
        # stacked QHBoxLayouts: the legend row's own checkbox+ticks need
        # to line up with the X/Y rows' slider column specifically, not
        # just approximately -- a grid's columns size consistently
        # across every row that uses them, which stacked independent
        # row layouts can't guarantee without manually matching widget
        # widths by hand.
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        # col 0=(axis letter+value)/checkbox, 1=minus, 2=slider/legend, 3=plus

        def _value_label() -> QLabel:
            lbl = QLabel("0")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            # Fixed width, not zero-padded -- "-100" and "50" both need
            # to occupy the same on-screen space so neither row's
            # slider shifts horizontally depending on how many digits
            # its current value happens to have, but the text itself
            # stays a plain, unpadded number (confirmed: no "-050").
            lbl.setFixedWidth(28)
            return lbl

        # Shared by the X/Y letter labels AND the link checkbox (see
        # both below) -- the one fixed size that has to match between
        # them for their centers to actually line up.
        _AXIS_LABEL_SIZE = QSize(18, 18)

        def _axis_cell(letter: str) -> tuple[QWidget, QLabel, QLabel]:
            # Letter and value share their OWN tight inner layout,
            # deliberately separate from the grid's own (wider) column
            # spacing -- the grid's uniform spacing is what every other
            # gap in this panel (minus-to-slider, slider-to-plus) uses,
            # and narrowing it globally to tighten just this one gap
            # would have narrowed those too.
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)
            axis_label = QLabel(letter)
            axis_label.setAlignment(Qt.AlignCenter)
            # Fixed size, not left to its own natural text size --
            # confirmed as the actual cause of a real, reported bug:
            # the "linked" stylesheet (see _set_flatten_link_visual)
            # adds its own padding, which grew the label's OWN
            # occupied space the instant it was applied, nudging the
            # minus button/slider/plus button that follow it in this
            # same row a few pixels to the right -- fixing the size up
            # front means the padding is drawn WITHIN that fixed box
            # instead of growing it, so toggling the tint never
            # changes anything else's position. Also what makes exact
            # alignment against the link checkbox possible at all (see
            # its own matching setFixedWidth below) -- two widgets
            # can't reliably share a center unless both have a known,
            # fixed size to center within.
            axis_label.setFixedSize(_AXIS_LABEL_SIZE)
            value_label = _value_label()
            cell_layout.addWidget(axis_label)
            cell_layout.addWidget(value_label)
            return cell, axis_label, value_label

        def _step_button(text: str) -> QPushButton:
            btn = QPushButton(text)
            btn.setObjectName("stepButton")
            # Explicit, not left to the "#stepButton" QSS rule alone --
            # confirmed that rule's min-width/max-width don't reliably
            # constrain a QPushButton's actual sizeHint in every
            # environment (measured 80px instead of the intended 20px
            # under this project's own offscreen test rendering, though
            # not under normal on-screen rendering). Costs nothing and
            # removes any dependency on that QSS property actually
            # working everywhere.
            btn.setFixedWidth(20)
            return btn

        x_cell, x_label, self.flatten_x_value_label = _axis_cell("X")
        self.flatten_x_minus_btn = _step_button("−")
        self.flatten_x_slider = QSlider(Qt.Horizontal)
        self.flatten_x_plus_btn = _step_button("+")
        grid.addWidget(x_cell, 0, 0)
        grid.addWidget(self.flatten_x_minus_btn, 0, 1)
        grid.addWidget(self.flatten_x_slider, 0, 2)
        grid.addWidget(self.flatten_x_plus_btn, 0, 3)

        self.flatten_link_check = QCheckBox()
        # Same fixed width as the X/Y letters (see _AXIS_LABEL_SIZE) and
        # left-aligned within column 0, not centered -- centering
        # within the FULL column (which x_cell/y_cell make wider than
        # just the letter, since the value label shares that same
        # column) put the checkbox under the midpoint of "X  0"
        # combined, not under "X" specifically. x_cell's own inner
        # layout packs from the left with no trailing stretch, so the
        # letter always sits flush against the column's own left edge
        # -- left-aligning the checkbox at the same fixed width lines
        # the two up exactly, not just approximately.
        self.flatten_link_check.setFixedWidth(_AXIS_LABEL_SIZE.width())
        self._flatten_link_tooltip = (
            "Link X and Y -- both sliders move together.\n"
            "Toggling this (either direction) commits the current shape as\n"
            "its own undo step and starts fresh from 0, so the full\n"
            "-100..100 range stays available regardless of where the\n"
            "sliders were.")
        self.flatten_link_check.setToolTip(self._flatten_link_tooltip)
        # Only the two end ticks and center, not +/-50 too -- confirmed
        # as the actual source of the reported cramped layout: fitting
        # five tick labels without overlap has a real minimum width
        # this panel didn't have to meet back when there was only one
        # slider row, and dropping to three removes that constraint
        # rather than needing more horizontal room to satisfy it.
        self.flatten_legend = SliderLegend(-100, 100, [-100, 0, 100], abs_labels=True)
        grid.addWidget(self.flatten_link_check, 1, 0, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(self.flatten_legend, 1, 2)

        y_cell, y_label, self.flatten_y_value_label = _axis_cell("Y")
        self.flatten_y_minus_btn = _step_button("−")
        self.flatten_y_slider = QSlider(Qt.Horizontal)
        self.flatten_y_plus_btn = _step_button("+")
        grid.addWidget(y_cell, 2, 0)
        grid.addWidget(self.flatten_y_minus_btn, 2, 1)
        grid.addWidget(self.flatten_y_slider, 2, 2)
        grid.addWidget(self.flatten_y_plus_btn, 2, 3)

        flatten_layout.addLayout(grid)
        # Kept for _set_flatten_link_visual (background tint) rather
        # than a wrapping frame's border -- box-around-a-box read as
        # visually odd, and the frame's own margins were the reason
        # this panel sat taller than Grid & snap in the first place.
        self._flatten_axis_labels = (x_label, y_label)

        # Center (0) is the original, untouched shape on both axes --
        # leftward (negative) flattens toward the anchor, rightward
        # (positive) stretches past it. X anchors to the leftmost point
        # (min_x), analogous to Y's own min_y (see geometry.flatten_xy).
        for slider, minus_btn, plus_btn, changed_slot in (
            (self.flatten_x_slider, self.flatten_x_minus_btn, self.flatten_x_plus_btn,
             self._on_flatten_x_changed),
            (self.flatten_y_slider, self.flatten_y_minus_btn, self.flatten_y_plus_btn,
             self._on_flatten_y_changed),
        ):
            slider.setRange(-100, 100)
            slider.setValue(0)
            slider.valueChanged.connect(changed_slot)
            slider.sliderReleased.connect(self._commit_flatten)
            minus_btn.clicked.connect(lambda _c=False, s=slider: s.setValue(s.value() - 1))
            plus_btn.clicked.connect(lambda _c=False, s=slider: s.setValue(s.value() + 1))
            for btn in (minus_btn, plus_btn):
                btn.setAutoRepeat(True)
                btn.setAutoRepeatDelay(400)
                btn.setAutoRepeatInterval(60)
                btn.released.connect(self._commit_flatten)
        self.flatten_link_check.toggled.connect(self._on_flatten_link_toggled)
        self._set_flatten_enabled(False)

        self.bottom_bar = bottom_bar = QWidget()
        bottom_bar_layout = QHBoxLayout(bottom_bar)
        bottom_bar_layout.setContentsMargins(8, 4, 8, 4)
        # Equal split -- reverted from an earlier 1:2 attempt to widen
        # Size's own share of this row. That was solving the wrong
        # problem: trimming the legend to three ticks (see its own
        # setup comment) removed the actual width requirement it was
        # compensating for, so the original even split is enough again.
        bottom_bar_layout.addWidget(weight_box, 1)
        bottom_bar_layout.addWidget(flatten_box, 1)

        canvas_container = QWidget()
        canvas_container_layout = QVBoxLayout(canvas_container)
        canvas_container_layout.setContentsMargins(0, 0, 0, 0)
        canvas_container_layout.setSpacing(0)
        canvas_container_layout.addWidget(self.center_panel, 1)
        canvas_container_layout.addWidget(bottom_bar, 0)
        splitter.addWidget(canvas_container)

        # -- right: properties --
        self.right_panel = right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 4, 4)

        # Family, back on the main screen -- moved out of Advanced
        # Settings (which is being phased out entirely as its
        # remaining contents relocate: EM already moved to Canvas
        # below, Metadata/Coverage/Spacing are moving into the export
        # flow). Committed live on editingFinished, matching how
        # EM/Ascent/Descent already work in the Canvas diagram, rather
        # than requiring a separate dialog's OK button.
        family_box = QGroupBox("Family")
        family_layout = QVBoxLayout(family_box)
        self.family_edit = QLineEdit()
        self.family_edit.editingFinished.connect(self._on_family_edited)
        family_layout.addWidget(self.family_edit)
        right_layout.addWidget(family_box)

        # Title becomes "Canvas (WxH)" once a project is loaded (see
        # _refresh_metrics_label). Starting as plain "Canvas" rather
        # than "Detected metrics" for the empty state too -- keeping
        # one consistent name throughout (just without dimensions yet)
        # reads as less confusing than switching to a completely
        # different label the moment real data shows up.
        metrics_box = QGroupBox("Canvas")
        self.metrics_box = metrics_box
        metrics_box_layout = QVBoxLayout(metrics_box)
        # MetricsDiagram is Fixed/Fixed sized (see its own
        # setSizePolicy comment), so its own internal proportions are
        # untouched by margin changes here; only the empty margin
        # AROUND it changes. Trimmed to hand space to Grid & snap
        # instead (see grid_hbox's own setContentsMargins) -- a first
        # attempt trimmed 4px top+bottom (8px total) for a reported
        # ~8px mismatch, which overshot once checked against a real
        # screenshot (Grid & snap landed 5px TALLER afterward, not
        # equal); this smaller trim matches the corrected estimate.
        # Still an estimate, not something this project's own offscreen
        # test environment can verify pixel-exact -- the same category
        # of on-screen-vs-offscreen rendering gap already confirmed
        # once before, for the step buttons' width.
        metrics_box_layout.setContentsMargins(6, 5, 6, 4)
        self.metrics_diagram = MetricsDiagram()
        metrics_box_layout.addWidget(self.metrics_diagram)
        # Expanding, not fixed -- this is what actually absorbs the
        # free space between it and Clearance check now, replacing the
        # old bare stretch that just left that space empty. A dimension
        # diagram genuinely benefits from more room (taller box, more
        # legible spacing) the same way the old flat text rows never
        # did, so growing INTO free space is a real improvement here,
        # not just filling a gap for its own sake.
        # Fixed (0), not Expanding (1) -- MetricsDiagram's own Fixed
        # size policy only pins ITS OWN size; this GroupBox wrapping it
        # was still being told to absorb extra vertical space in the
        # sidebar's layout, which is a separate thing and would have
        # kept happening regardless of what's fixed inside it. This is
        # the actual fix for "don't auto-size, keep at minimum" --
        # confirmed the earlier size-policy fix alone didn't address
        # this outer stretch factor.
        right_layout.addWidget(metrics_box, 0)

        # Stretch here, not at the very end -- keeps Clearance check,
        # Grid & snap, and Line endings packed together at the BOTTOM
        # of the sidebar (close to Flatten, in the bottom bar next to
        # the canvas) rather than sitting up near the top with the gap
        # trailing after them. Detected metrics stays fixed-size right
        # where it is; everything below this point stays pinned to the
        # bottom regardless of how tall the window gets.
        right_layout.addStretch(1)

        clearance_box = QGroupBox("Clearance check")
        self.clearance_label = QLabel("—")
        self.clearance_label.setWordWrap(True)
        self.clearance_label.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        # Fixed to fit two lines always, not sized to whatever the
        # CURRENT message happens to need -- previously this box
        # visibly grew/shrank between a one-line "OK" and a two-line
        # issue description, shifting Grid & snap and everything below
        # it up and down as a side effect of an unrelated status change.
        clearance_metrics = self.clearance_label.fontMetrics()
        self.clearance_label.setMinimumHeight(clearance_metrics.lineSpacing() * 2 + 4)
        clearance_layout = QVBoxLayout(clearance_box)
        clearance_layout.addWidget(self.clearance_label)
        # Placed directly against Grid & snap now (see below) -- no
        # stretch between them; the stretch above (between Detected
        # metrics and here) is what absorbs the panel's free space
        # instead.
        right_layout.addWidget(clearance_box)

        # Grid & snap and Line endings sit directly below Clearance now
        # -- no stretch here anymore. MetricsDiagram above (Expanding
        # size policy) is what absorbs the panel's free space instead,
        # which is what was actually asked for: growing the diagram
        # into that space rather than leaving it as a bare gap.

        # Controls from here down, not info -- Grid & snap and Line
        # endings are things actively adjusted while working, unlike
        # the read-only boxes above. Line endings specifically goes
        # LAST, closest to the bottom of the panel: it's the same kind
        # of setting as Weight (both define stroke rendering -- cap
        # style and thickness), so ending the panel here reads as a
        # continuation into the bottom bar's controls, not an unrelated
        # jump back to a metrics-style box.
        self.caps_box = caps_box = QGroupBox("Line endings")
        caps_layout = QHBoxLayout(caps_box)
        self.round_btn = QPushButton("Round")
        self.flat_btn = QPushButton("Flat")
        self.round_btn.setCheckable(True)
        self.flat_btn.setCheckable(True)
        self.round_btn.setChecked(True)
        self.round_btn.clicked.connect(lambda: self._on_cap_style_changed("round"))
        self.flat_btn.clicked.connect(lambda: self._on_cap_style_changed("flat"))
        caps_layout.addWidget(self.round_btn)
        caps_layout.addWidget(self.flat_btn)
        right_layout.addWidget(caps_box)

        # Grid & snap goes LAST, below Line endings -- an ordinary
        # widget in this same layout, nothing more. An earlier attempt
        # made this float outside the sidebar entirely, positioned by
        # hand from resizeEvent, specifically so it would stay visible
        # if the sidebar were ever short on room -- but reordering two
        # boxes within the same layout never changes the sidebar's
        # OWN total content height at all, so that whole mechanism was
        # solving a problem that reordering alone doesn't create. Any
        # actual shortage of vertical room is instead handled once, up
        # front, by fixing MainWindow's own minimum height to whatever
        # the full stack of sidebar boxes needs (see __init__'s tail),
        # rather than reacting to it per-box here.
        # Blank outer wrapper -- "Snap" and "Grid" are each a REAL,
        # separate QGroupBox now (see snap_box/spacing_box below), not
        # a single shared box with two plain-label fake titles. That
        # matters for height, not just looks: confirmed as a real,
        # reported problem with the plain-label version otherwise --
        # a QGroupBox's own title renders IN its border strip, costing
        # it almost nothing, while an ordinary QLabel styled to look
        # like a title is just regular content and adds a genuine
        # extra row's worth of height. The plain-label attempt measured
        # ~14px taller than Weight/Size; two real titled boxes measure
        # back at this box's own original ~8px gap instead -- the same
        # mismatch that already existed before any of this, not a new
        # one. self.grid_box stays a plain QWidget wrapper (not a
        # QGroupBox itself) purely so _float_grid_box's own reparenting
        # (see that method) still has exactly one widget to move
        # around as a unit, unchanged.
        self.grid_box = grid_box = QWidget()
        grid_hbox = QHBoxLayout(grid_box)
        grid_hbox.setContentsMargins(0, 0, 0, 0)

        self.snap_box = snap_box = QGroupBox("Snap - Hold CTRL to bypass")
        checks_col = QVBoxLayout(snap_box)
        # Margins unchanged from this box's own earlier single-box form
        # -- see this file's git history for the original height-
        # matching story against Weight/Size.
        checks_col.setContentsMargins(11, 12, 11, 13)
        self.snap_grid_check = QCheckBox("Grid (G)")
        self.snap_grid_check.setToolTip("Press G to toggle this on/off.")
        self.snap_grid_check.toggled.connect(self._on_grid_settings_changed)
        checks_col.addWidget(self.snap_grid_check)
        self.snap_points_check = QCheckBox("Segments (S)-Hold E to skip endpoints")
        self.snap_points_check.setToolTip(
            "Point first, then line, whichever is nearer -- an existing\n"
            "endpoint always wins over a nearby line when both are close\n"
            "enough to qualify.\n"
            "Also applies while drawing a new stroke, against that same\n"
            "stroke's own earlier points/segments, not just\n"
            "already-committed glyph geometry.")
        self.snap_points_check.toggled.connect(self._on_grid_settings_changed)
        checks_col.addWidget(self.snap_points_check)
        self.snap_xheight_baseline_check = QCheckBox("X-height / baseline (H)")
        self.snap_xheight_baseline_check.setToolTip(
            "Y only -- locks a drawn point's height to whichever of\n"
            "x-height or baseline is closer.\n"
            "Takes precedence over grid/points for Y when more than one\n"
            "of these is enabled at once.\n"
            "Press H to toggle this on/off.")
        self.snap_xheight_baseline_check.toggled.connect(self._on_grid_settings_changed)
        checks_col.addWidget(self.snap_xheight_baseline_check)
        # Stretch=1 -- this box gets whatever width spacing_box (below)
        # doesn't need, which is what makes the "Segments (S)-Hold
        # E to skip endpoints" line fit at all.
        grid_hbox.addWidget(snap_box, 1)

        self.spacing_box = spacing_box = QGroupBox("Grid")
        spacing_col = QVBoxLayout(spacing_box)
        spacing_col.setContentsMargins(11, 12, 11, 13)
        # Bare "Spacing" is safe now that it's sitting inside its own
        # "Grid"-titled box -- the earlier "Grid spacing" wording
        # existed specifically to disambiguate against a single shared
        # box title covering both this and Snap at once, which no
        # longer exists.
        spacing_label = QLabel("Spacing (px)")
        spacing_col.addWidget(spacing_label)
        self.grid_spacing_spin = QDoubleSpinBox()
        self.grid_spacing_spin.setRange(0, 500)
        self.grid_spacing_spin.setValue(0)
        self.grid_spacing_spin.setSpecialValueText("off")
        # Capped/floored to fit "500,00" (the widest value this range
        # can ever show, locale decimal separator included) plus the
        # native spin-arrow buttons -- confirmed as a real, reported
        # issue otherwise: left to its own default sizing, this
        # stretched to fill the whole column, far wider than the
        # 3-character value it actually ever needs to display. A
        # setMaximumWidth alone (an earlier version of this fix) turned
        # out to do NOTHING useful once combined with the AlignRight
        # below -- Qt sizes an aligned widget by its own sizeHint(),
        # not by its maximum, so the cap was never actually the
        # constraint in the first place. setMinimumWidth is what
        # actually forces real width regardless of alignment. The +60
        # padding here (not a tighter value closer to this project's
        # own measured text width) is deliberate margin, not a precise
        # fit -- a tighter cap was confirmed, on a real screen, to clip
        # the trailing digit of values like 499,00, despite this
        # project's own offscreen test measurements showing comfortable
        # room to spare (Qt's own computed size hint for this exact
        # spinbox measured at only 70px here) -- the same category of
        # offscreen-vs-on-screen rendering gap already hit once before,
        # for these same controls' width, so this errs generous rather
        # than trying to re-derive an exact number this test
        # environment already can't be trusted to confirm. Right-
        # aligned within its column so growing/shrinking it moves its
        # LEFT edge rather than leaving it stranded away from the
        # column's own right border. Padding trimmed from +60 to +45
        # (was 102px rendered, now 87px) to give the Segments checkbox
        # room for its longer "Hold E to skip endpoints" wording --
        # confirmed there was real slack to give back: the +60 version
        # measured 17px of comfortable margin above Qt's own bare
        # natural need here (70px) even after this trim, not down to
        # that bare minimum.
        spin_width = QFontMetrics(self.grid_spacing_spin.font()).horizontalAdvance("500,00") + 45
        self.grid_spacing_spin.setMinimumWidth(spin_width)
        self.grid_spacing_spin.setMaximumWidth(spin_width)
        self.grid_spacing_spin.valueChanged.connect(self._on_grid_settings_changed)
        spacing_col.addWidget(self.grid_spacing_spin, 0, Qt.AlignRight)
        spacing_col.addStretch(1)
        grid_hbox.addWidget(spacing_box, 0)
        right_layout.addWidget(grid_box)


        # Ideal width tracked for reactive clamping, not a hard
        # setFixedWidth constraint on the widget itself -- confirmed
        # as a real, reported source of bugs: a splitter forced to
        # collapse a genuinely FIXED-width child doesn't cleanly
        # resize it down to zero the way it does an ordinary,
        # unconstrained one -- it moves the whole widget off-screen
        # instead (position goes deeply negative, size unchanged),
        # which is a working-but-fragile special case that produced
        # multiple real, reported symptoms downstream (a 2-phase
        # visual collapse, and a restore that didn't bring back the
        # widget's own internal state correctly) once other widgets
        # with their own internal layout (like a scrollbar) were
        # involved. Clamped in _clamp_pane_widths instead, reacting to
        # splitterMoved -- the widget itself carries no special size
        # constraint at all, so the splitter's own normal, unmodified
        # resize/collapse behavior handles it exactly like any other
        # pane (confirmed clean before this fixed-width attempt was
        # ever added). This still prevents drag-WIDENING past the
        # ideal width (there's no benefit to a wider sidebar than its
        # content needs), while collapsing all the way to zero to hide
        # it remains a perfectly ordinary splitter collapse.
        self._right_pane_width = right.sizeHint().width()
        splitter.addWidget(right)

        # Explicit stretch factors -- previously unset, meaning a wider
        # window had no DEFINED place for the extra space to go, and it
        # was landing on the sidebar (see MetricsDiagram's own size
        # policy fix for the reported symptom this caused). The canvas
        # (index 1) is what should actually grow with the window; the
        # glyph browser and metrics sidebar stay at their own natural
        # widths regardless of how wide the window gets.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)

        splitter.setSizes([220, 640, 240])
        self.splitter = splitter
        # Hints that these can be dragged all the way to collapse a
        # pane -- not obvious from the handle's own appearance alone
        # (confirmed: collapsing was only found by accident). Ordinary
        # setToolTip() calls on the handle objects, not a
        # QSplitterHandle subclass -- see CollapsedPaneTab for why
        # that distinction matters here.
        splitter.handle(1).setToolTip("Drag to resize \u2014 drag to the edge to collapse")
        splitter.handle(2).setToolTip("Drag to resize \u2014 drag to the edge to collapse")
        self.setCentralWidget(splitter)

        # Floating tabs for collapsed panes -- see CollapsedPaneTab for
        # why this isn't a QSplitterHandle subclass. Parented to
        # MainWindow itself, NOT the splitter -- QSplitter treats any
        # direct child widget as an additional pane of its own layout,
        # confirmed as a real bug: parenting these to the splitter
        # silently turned it into a 5-pane splitter (3 real + 2 tabs),
        # visible as sizes() suddenly returning 5 elements instead of
        # 3. Positioned in MainWindow's own coordinate space instead
        # (see _update_collapsed_tabs, which maps the splitter's
        # position into that space).
        self._left_collapsed_tab = CollapsedPaneTab("Glyphs", self)
        self._left_collapsed_tab.clicked_signal.connect(lambda: self._restore_collapsed_pane(0, self._left_pane_width))
        self._right_collapsed_tab = CollapsedPaneTab("Metrics", self)
        self._right_collapsed_tab.clicked_signal.connect(lambda: self._restore_collapsed_pane(2, self._right_pane_width))
        # One grip indicator per handle -- shown only while that
        # handle's own pane is expanded (see _update_collapsed_tabs,
        # which hides it in favor of the collapsed-pane tab once the
        # pane reaches zero width, the same swap CollapsedPaneTab
        # itself does in the other direction).
        self._left_grip = SplitterGripIndicator(self)
        self._right_grip = SplitterGripIndicator(self)
        # Tracks whether Grid & snap is currently pulled out of the
        # right sidebar's own layout and floating over MainWindow
        # instead -- see _update_collapsed_tabs, which is what flips
        # this. Grid & snap needs to keep working (and stay reachable)
        # even with the sidebar itself fully collapsed, but living
        # there PERMANENTLY (an earlier attempt) caused real,
        # reported problems of its own once mixed with the sidebar's
        # normal, expanded-state layout -- confining the floating
        # behavior to only the brief collapsed window itself avoids
        # that: in every other state it's simply an ordinary widget in
        # right_layout, with no special-case logic touching it at all.
        self._grid_box_floating = False
        self._collapsed_tab_timer = QTimer(self)
        self._collapsed_tab_timer.timeout.connect(self._update_collapsed_tabs)
        self._collapsed_tab_timer.start(200)

        self.setStatusBar(QStatusBar())
        # Two dedicated, permanent-style labels replace QStatusBar's own
        # showMessage()/temporary-message mechanism entirely -- added
        # via addWidget() (the "normal" left-hand region), not
        # addPermanentWidget(): permanent widgets are always packed to
        # the RIGHT of that region, with no way to place one before it,
        # so they can't sit at the true far left the way glyph info
        # needs to. showMessage() itself would also visually obscure
        # both of these the same way it obscured everything before --
        # every previous showMessage() call site elsewhere in this file
        # now sets self.status_message_label directly instead, via
        # _set_status_message.
        #
        # glyph_info_label: current glyph's own info (U+XXXX, contour
        # count) -- set ONLY by _load_glyph_into_canvas, and only
        # cleared there too, on the next glyph switch. Confirmed as the
        # wanted behavior over any auto-clearing timer: stays exactly
        # as long as its own glyph stays on screen, nothing else.
        self.glyph_info_label = QLabel("")
        self.statusBar().addWidget(self.glyph_info_label)
        # status_message_label: everything else (save/load/delete
        # confirmations, capture-commit summaries, etc.) -- separate
        # from glyph_info_label so neither ever covers the other, each
        # always visible at its own fixed position. Cleared whenever a
        # different glyph is loaded (see _load_glyph_into_canvas's own
        # comment) rather than on any timer -- stays until either that
        # happens or another message replaces it.
        self.status_message_label = QLabel("")
        self.statusBar().addWidget(self.status_message_label)
        # project_name_label: this project's own name (whatever it was
        # saved/will be saved as) and its folder -- the persistent
        # replacement for the old "Saved <full path>" transient
        # message, which vanished the moment any OTHER status message
        # replaced it and required remembering what had briefly
        # flashed by earlier. A permanent widget, not addWidget like
        # the two above -- deliberately placed immediately to the LEFT
        # of coord_label (permanent widgets pack left-to-right in the
        # order added, all in the right-hand region), matching where
        # this was asked to sit relative to the x/y display.
        #
        # Given a fixed maximum width (see _refresh_project_name_label)
        # so a long folder path can never crowd out the coordinate/zoom
        # display beside it -- elided rather than wrapped or left to
        # overflow, with the full, un-elided text always available on
        # hover via its own tooltip.
        self.project_name_label = QLabel("")
        self.project_name_label.setMaximumWidth(320)
        self.statusBar().addPermanentWidget(self.project_name_label)
        self.coord_label = QLabel("")
        # Monospace specifically for this label -- its own text is
        # deliberately fixed-width-padded (see _on_cursor_moved and
        # _on_offset_preview's own f-string formatting) so the
        # coordinate values visually align as they change, the same
        # reasoning and fix as the calibration-warnings dialog's own
        # padded columns: a proportional font (QLabel's default) makes
        # the padding characters present in the string but visually
        # inconsistent width per digit, so the numbers don't actually
        # line up on screen even though the string itself is padded
        # correctly.
        self.coord_label.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.statusBar().addPermanentWidget(self.coord_label)
        self.zoom_label = QLabel("")
        # Same font as coord_label, and the value itself padded the
        # same way (see _update_zoom_label) -- confirmed as the wanted
        # look, matching the x/y values immediately to its left rather
        # than reading as a visually unrelated, differently-styled
        # number next to them.
        self.zoom_label.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.statusBar().addPermanentWidget(self.zoom_label)

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)
        style_ = self.style()
        icons = get_icons()

        # -- Project ------------------------------------------------------
        new_action = QAction(style_.standardIcon(style_.StandardPixmap.SP_FileDialogNewFolder),
                              "New Project…", self)
        new_action.setToolTip("Start a project from scratch, no source PNGs")
        new_action.triggered.connect(self._new_blank_project)
        tb.addAction(new_action)

        open_action = QAction(icons["import"],
                               "Import PNG folder…", self)
        open_action.setToolTip("Import a folder of u<hex>.png source glyphs")
        open_action.triggered.connect(self._import_folder)
        tb.addAction(open_action)

        save_action = QAction(style_.standardIcon(style_.StandardPixmap.SP_DialogSaveButton),
                               "Save project…", self)
        save_action.setToolTip("Save project")
        save_action.triggered.connect(self._save_project)
        tb.addAction(save_action)

        load_action = QAction(style_.standardIcon(style_.StandardPixmap.SP_DialogOpenButton),
                               "Open project…", self)
        load_action.setToolTip("Open project")
        load_action.triggered.connect(self._load_project)
        tb.addAction(load_action)

        clear_project_action = QAction(icons["clear"], "Clear Project", self)
        clear_project_action.setToolTip(
            "Reset the workspace -- removes the current project from\n"
            "memory, optionally also resets persistent settings")
        clear_project_action.triggered.connect(self._clear_project)
        tb.addAction(clear_project_action)

        tb.addSeparator()

        # -- Undo/Redo ------------------------------------------------------
        self.undo_action = QAction(icons["undo"], "Undo", self)
        self.undo_action.setToolTip("Undo (Ctrl+Z)")
        self.undo_action.setShortcut(QKeySequence.Undo)
        self.undo_action.triggered.connect(self._undo)
        self.undo_action.setEnabled(False)
        tb.addAction(self.undo_action)

        self.redo_action = QAction(icons["redo"], "Redo", self)
        self.redo_action.setToolTip("Redo (Ctrl+Y)")
        self.redo_action.setShortcut(QKeySequence.Redo)
        self.redo_action.triggered.connect(self._redo)
        self.redo_action.setEnabled(False)
        tb.addAction(self.redo_action)

        tb.addSeparator()

        # -- Glyph management -------------------------------------------------
        add_glyph_action = QAction(style_.standardIcon(style_.StandardPixmap.SP_FileIcon),
                                    "Add Glyph…", self)
        add_glyph_action.setToolTip(
            "Add a new glyph -- blank to draw from scratch, or from one\n"
            "or more source PNGs")
        add_glyph_action.triggered.connect(self._add_blank_glyph)
        tb.addAction(add_glyph_action)

        capture_glyphs_action = QAction(icons["capture"], "Capture Glyphs…", self)
        capture_glyphs_action.setToolTip(
            "Capture rectangles from another on-screen application and\n"
            "add them as new glyphs")
        capture_glyphs_action.triggered.connect(self._start_capture_session)
        tb.addAction(capture_glyphs_action)

        edit_metrics_action = QAction(style_.standardIcon(style_.StandardPixmap.SP_FileDialogDetailedView),
                                       "Edit Metrics", self)
        edit_metrics_action.setToolTip("Show guides without a glyph selected")
        edit_metrics_action.triggered.connect(self._show_metrics_view)
        tb.addAction(edit_metrics_action)

        tb.addSeparator()

        # -- View ------------------------------------------------------------
        fit_window_action = QAction(icons["fit_window"], "Fit to window", self)
        fit_window_action.setToolTip("Fit to window")
        fit_window_action.triggered.connect(self.canvas.fit_to_window)
        tb.addAction(fit_window_action)

        tb.addSeparator()

        # -- Edit geometry -----------------------------------------------------
        self.cut_action = QAction(icons["cut"], "Cut", self)
        self.cut_action.setToolTip("Cut the selection (Ctrl+X) -- same whole/piece distinction as Delete")
        self.cut_action.setShortcut(QKeySequence.Cut)
        self.cut_action.triggered.connect(self._on_cut_clicked)
        self.cut_action.setEnabled(False)
        tb.addAction(self.cut_action)

        self.copy_action = QAction(icons["copy"], "Copy", self)
        self.copy_action.setToolTip("Copy the selection (Ctrl+C) -- same whole/piece distinction as Delete")
        self.copy_action.setShortcut(QKeySequence.Copy)
        self.copy_action.triggered.connect(self._on_copy_clicked)
        self.copy_action.setEnabled(False)
        tb.addAction(self.copy_action)

        self.paste_action = QAction(icons["paste"], "Paste", self)
        self.paste_action.setToolTip(
            "Paste (Ctrl+V) -- attaches to the cursor, click to place.\n"
            "Replaces a current selection, same as Delete.")
        self.paste_action.setShortcut(QKeySequence.Paste)
        self.paste_action.triggered.connect(self._on_paste_clicked)
        self.paste_action.setEnabled(False)
        tb.addAction(self.paste_action)

        self.break_action = QAction(icons["break"], "Break", self)
        self.break_action.setToolTip("Break the selected segment(s) off so they can move independently")
        self.break_action.triggered.connect(self._on_break_clicked)
        self.break_action.setEnabled(False)
        tb.addAction(self.break_action)

        self.join_action = QAction(icons["join"], "Join", self)
        self.join_action.setToolTip("Join the selected segments (from different paths) back into one")
        self.join_action.triggered.connect(self._on_join_clicked)
        self.join_action.setEnabled(False)
        tb.addAction(self.join_action)

        self.angle_snap_action = QAction(icons["angle_snap"], "Angle snap", self)
        self.angle_snap_action.setToolTip(
            "Constrain dragged points and new strokes to 22.5° steps\n"
            "from the segment's other end, or the previous point drawn")
        self.angle_snap_action.setCheckable(True)
        self.angle_snap_action.toggled.connect(self.canvas.set_angle_snap)
        tb.addAction(self.angle_snap_action)

        tb.addSeparator()

        # -- Draw --------------------------------------------------------------
        self.select_all_toolbar_action = QAction(icons["select_all"], "Select all", self)
        self.select_all_toolbar_action.setToolTip("Select all segments (Ctrl+A)")
        self.select_all_toolbar_action.triggered.connect(self._select_all_segments)
        tb.addAction(self.select_all_toolbar_action)

        self.draw_action = QAction(icons["pen"], "Draw", self)
        self.draw_action.setToolTip(
            "Draw straight-segment paths by clicking -- double-click,\n"
            "Enter, or Escape to finish/cancel the current path.\n"
            "Delete removes the selected segment's path.")
        self.draw_action.setCheckable(True)
        self.draw_action.toggled.connect(self._on_draw_mode_toggled)
        tb.addAction(self.draw_action)

        self.onion_skin_action = QAction(style_.standardIcon(style_.StandardPixmap.SP_FileDialogContentsView),
                                          "Onion Skin", self)
        self.onion_skin_action.setToolTip(
            "Show a faint outline of the nearest already-drawn glyph,\n"
            "as a weight/proportion reference")
        self.onion_skin_action.setCheckable(True)
        self.onion_skin_action.setChecked(True)
        self.onion_skin_action.toggled.connect(self._on_onion_skin_toggled)
        tb.addAction(self.onion_skin_action)

        tb.addSeparator()

        # -- Output --------------------------------------------------------
        # Single toolbar action now -- see ExportDialog's own docstring
        # for why the old separate "Advanced Settings" button (and its
        # direct-to-file-picker "Export TTF" sibling) folded into one.
        export_action = QAction(icons["export"], "Export TTF", self)
        export_action.setToolTip("Export TTF (spacing, metadata, multi-weight, coverage)")
        export_action.triggered.connect(self._open_export_dialog)
        tb.addAction(export_action)

        tb.addSeparator()

        # Standard Qt icon, not a custom one from icons.py -- Qt already
        # ships a clear, immediately-recognizable "help" glyph, unlike
        # Break/Join/Select-all/etc., which is the actual reason
        # everything in icons.py exists (see that module's own
        # docstring): custom icons there are for things with no good
        # standard equivalent, not a blanket rule against ever using one.
        help_action = QAction(style_.standardIcon(style_.StandardPixmap.SP_TitleBarContextHelpButton),
                               "Help", self)
        help_action.setToolTip("Help (F1)")
        help_action.setShortcut(QKeySequence.HelpContents)
        help_action.triggered.connect(self._open_help_dialog)
        tb.addAction(help_action)

    def _open_help_dialog(self):
        docs_root = Path(__file__).resolve().parent.parent / "docs"
        if self._help_dialog is None:
            self._help_dialog = HelpDialog(docs_root, self)
        self._help_dialog.show()
        self._help_dialog.raise_()
        self._help_dialog.activateWindow()

    def _open_export_dialog(self):
        if not self.project:
            QMessageBox.warning(self, "No project", "Import a glyph set first.")
            return
        dialog = ExportDialog(self.project, self)
        dialog.exec()
        # Settings (spacing, metadata) are only actually written to
        # self.project.font when Export… is clicked (see
        # ExportDialog._apply_settings), same as the old dialog's own
        # OK/Cancel split -- but that happens BEFORE the file/folder
        # picker within that flow, so canceling THAT picker still
        # leaves the dialog open with settings already applied, not
        # necessarily reflected by dialog.exec()'s own return value.
        # Refreshing unconditionally covers that case too, not just a
        # full, completed export.
        if self.current_cp is not None:
            self._load_glyph_into_canvas(self.current_cp, preserve_view=True)

    # -- project / import -------------------------------------------------

    def _new_blank_project(self):
        """A project with no source PNGs at all -- metrics start at
        sensible proportions (see Project.new_blank) rather than
        needing calibration, so there's something to drag into place
        immediately instead of an empty form to guess numbers into."""
        if not self._create_new_project_via_dialog(size_label="Canvas"):
            return
        self._set_status_message(
            "New project created -- drag cap/x-height/baseline/descender/advance into place, "
            "then add a glyph to start drawing.")
        # Immediately prompting for the first glyph rather than leaving
        # it as a separate, easy-to-forget step -- a from-scratch
        # project has nothing to draw INTO yet, and the natural
        # instinct is to reach for the Draw tool right away.
        self._add_blank_glyph()

    def _create_new_project_via_dialog(self, size_label: str = "Canvas", show_size_fields: bool = True,
                                        defer_canvas_draw: bool = False) -> bool:
        """Shared by _new_blank_project (the normal toolbar action),
        CaptureSessionController's "start a capture session with no
        project open yet" path, and _add_blank_glyph's own "no project
        yet" path -- same underlying Project.new_blank call every time,
        since whatever ends up defining the canvas size (a typed width/
        height, a capture's own size, or a picked file's own size) is
        just a different SOURCE for the same number. size_label only
        changes the dialog's own wording ("Canvas width/height" vs
        "Capture width/height"); show_size_fields is independent of
        that -- decoupled from size_label entirely (an earlier version
        tied it to size_label == "Capture" specifically, which had no
        way to also apply it from _add_blank_glyph's own entry point,
        where the same "there's no way to know the right size yet"
        reasoning applies just as well to a File... pick as to a
        capture).

        show_size_fields=False (used by both capture-driven entry
        points) omits the width/height fields from NewProjectDialog
        entirely -- not just a checkbox gating them, the fields
        themselves, since neither entry point's user could ever
        meaningfully know the right size at this point anyway. See
        NewProjectDialog's own __init__ comment.

        defer_canvas_draw: when True, skips showing the canvas here
        UNCONDITIONALLY -- _add_blank_glyph's own flow needs this:
        File... and Capture there can each still override even a
        typed/default size with their own real content's dimensions
        (confirmed as the actual, wanted behavior, not a bug -- the New
        Project fields, when present at all, are really just the
        FALLBACK for a blank first glyph, not a hard commitment). Since
        there's no way to know here whether the caller's own next step
        will end up overriding the size again, drawing anything at all
        this early risks showing something that's about to change.

        Returns True with self.project set on success, False if
        cancelled anywhere along the way (name/size dialog, folder
        picker) -- callers must check this before proceeding, since
        unlike _new_blank_project's own tail (status message + prompting
        for a first glyph), other callers have their own, different
        follow-ups that must not run on a cancelled attempt."""
        if not self._confirm_discard_if_dirty():
            return False
        # Folder location is now part of the SAME dialog (see
        # NewProjectDialog's own docstring on the merge) -- never ask
        # to confirm creating a NEW folder, since naming a new project
        # already IS the intent to create one. Only ever confirm the
        # opposite case: the resulting folder already exists, where
        # silently reusing it could mean overwriting something.
        # Declining that confirmation loops back to a FRESH dialog (not
        # pre-filled with the previous entries -- matches this flow's
        # existing behavior before the merge, where declining also
        # reopened a fresh name/size dialog), not out of the whole flow.
        while True:
            dialog = NewProjectDialog(self, size_label=size_label, show_size_fields=show_size_fields)
            if dialog.exec() != QDialog.Accepted:
                return False
            name, width, height = dialog.name, dialog.chosen_width, dialog.chosen_height
            folder = dialog.chosen_folder
            # dialog.chosen_width/height, not dialog.width_spin.value()
            # -- the fields themselves don't exist at all when
            # show_size_fields is False (see NewProjectDialog's own
            # __init__), but chosen_width/height are always set either
            # way (falling back to the __init__ defaults when there
            # were no fields to read from). _add_blank_glyph's own "no
            # project yet" path needs this specifically for a BLANK
            # first glyph, which (unlike File or Capture) has no real
            # content of its own to ever finalize a still-deferred
            # canvas from. See _add_blank_glyph's own use of this.
            self._last_new_project_field_size = QSize(dialog.chosen_width, dialog.chosen_height)
            # Remembers the PARENT the user actually chose (what's in
            # parent_edit), not the combined project folder itself --
            # confirmed as a real, reported bug otherwise:
            # remember_last_browsed_dir's own "remember the containing
            # directory" logic (correct for a FILE path) instead kept
            # the newly-created/reused PROJECT folder itself once it
            # existed on disk (os.path.isdir(folder) is true by then),
            # so the NEXT New Project dialog pre-filled "Parent folder"
            # with the PREVIOUS project's own folder -- creating a new
            # project there nested it inside the previous one instead
            # of alongside it.
            app_settings.remember_last_browsed_dir(dialog.parent_edit.text().strip())

            if os.path.isdir(folder):
                reply = QMessageBox.question(
                    self, "Folder already exists",
                    f'"{folder}" already exists. Use it anyway?',
                    QMessageBox.Yes | QMessageBox.No)
                if reply != QMessageBox.Yes:
                    continue   # back to a fresh dialog
                break
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(self, "Couldn't create folder", str(e))
                return False
            break

        # width == height == 0 is NewProjectDialog's own sentinel for
        # "set by first capture" (only offered when size_label ==
        # "Capture") -- there's no real size yet to build real metrics
        # from, so a small, clearly-provisional placeholder stands in
        # until CaptureSessionController._on_captured finalizes the
        # real canvas size and metrics from the session's first
        # captured rectangle (see _finalize_capture_canvas_size below).
        self._capture_canvas_pending = (width == 0 and height == 0)
        if self._capture_canvas_pending:
            width, height = 300, 500   # arbitrary but harmless -- overwritten on first capture

        self.project = Project.new_blank(folder, width, height)
        # Every capture/File-add for a codepoint not yet in this
        # project writes its source PNG straight into source_dir --
        # for a genuinely from-scratch project, that first write IS
        # what establishes the glyph's own original PNG, so there's
        # nothing yet to protect (see _import_single_png_for_glyph's
        # own routing decision). Flips to False the moment this
        # project is loaded from disk instead (Open project, crash
        # recovery's own saved_path branch) or Import folder replaces
        # it -- confirmed as scoped to the LIVE session a project was
        # created fresh in, not a permanent property of the project
        # file itself: once saved and reopened, it's indistinguishable
        # from any other existing project.
        self._project_created_fresh = True
        self._saved_project_path = None
        self._refresh_project_name_label()
        self._remember_last_project()
        self._onion_skin_override_cp = None
        self.project.font.family_name = os.path.basename(folder.rstrip("/\\")) or "New Font"
        self._saved_snapshot = None   # brand new, never saved -- dirty until the first save
        # _paths_cache is keyed by codepoint alone, not (project, codepoint)
        # -- without clearing it here, a codepoint reused across two
        # different projects (near-guaranteed for common ASCII letters)
        # would show the OLD project's cached shape as its list icon,
        # even though clicking it correctly re-traces from the new
        # project's own source and shows the right thing on canvas.
        self._paths_cache.clear()
        self._refresh_glyph_list()
        self._refresh_metrics_label()
        # Skipped while deferred, OR whenever the caller passed
        # defer_canvas_draw (see this method's own docstring for why
        # _add_blank_glyph always needs the latter, even for an
        # explicit size) -- confirmed as a real, reported source of
        # confusion otherwise: this would draw the small 300x500
        # placeholder (or an explicit size that a subsequent File/
        # Capture might still override) immediately, only for it to
        # visibly change again moments later once the real outcome
        # became known. Callers that defer are responsible for showing
        # the canvas themselves once that's actually known -- see
        # _finalize_capture_canvas_size (already does this) and
        # _add_blank_glyph's own fallback for a fully-abandoned attempt.
        if not self._capture_canvas_pending and not defer_canvas_draw:
            self._show_metrics_view()
        return True

    # Below this in EITHER dimension, a capture/file is treated as
    # likely-accidental rather than a deliberately tiny glyph --
    # proportionally larger than tracing.py's own MIN_KEEP_COMPONENT
    # (10px) / MIN_PATH_LENGTH (15px), since a whole CANVAS needs real
    # room for multiple strokes and margins, not just one short stroke.
    _MIN_CANVAS_DIMENSION = 40

    def _confirm_tiny_capture_size(self, width: int, height: int) -> str:
        """Returns "accept" (proceed -- also the immediate result when
        the size isn't actually tiny, so callers can use this
        unconditionally), "restart" (try again), or "discard" (abandon
        this attempt) -- called before finalizing a "set from the first
        glyph/capture" project's canvas size (see
        _create_new_project_via_dialog) whenever the candidate size is
        suspiciously small. A tiny accidental selection here would
        otherwise silently lock in an unusable canvas for the rest of
        the project, with no warning at all until every subsequently
        added glyph looked wrong. "Try again" is the default button,
        not "Use it anyway" -- this is specifically warning about a
        likely MISTAKE, so an errant Enter/Escape shouldn't be the one
        response that locks it in permanently; "accept" still stays
        available and un-buried for the real, valid case of a
        deliberately small glyph (a single symbol, say)."""
        if width >= self._MIN_CANVAS_DIMENSION and height >= self._MIN_CANVAS_DIMENSION:
            return "accept"
        box = QMessageBox(
            QMessageBox.Warning, "Very small capture",
            f"This capture is only {width} x {height}px -- likely too small "
            f"for a usable canvas, since every guide and margin on the whole "
            f"project would be sized from it. What would you like to do?",
            parent=self)
        restart_btn = box.addButton("Try again", QMessageBox.ActionRole)
        box.addButton("Use it anyway", QMessageBox.AcceptRole)
        discard_btn = box.addButton("Discard", QMessageBox.DestructiveRole)
        box.setDefaultButton(restart_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is discard_btn:
            return "discard"
        if clicked is restart_btn:
            return "restart"
        return "accept"

    def _prompt_canvas_size(self, message: str, default_width: int, default_height: int) -> QSize | None:
        """Shows CanvasSizePromptDialog, returns the confirmed QSize,
        or None if cancelled."""
        dlg = CanvasSizePromptDialog(self, message, default_width, default_height)
        if dlg.exec() != QDialog.Accepted:
            return None
        return QSize(dlg.width_spin.value(), dlg.height_spin.value())

    def _finalize_capture_canvas_size(self, width: int, height: int) -> None:
        """Called once, by CaptureSessionController._on_commit, when
        committing a "set by first capture" project's (see
        _create_new_project_via_dialog) captures for the first time --
        replaces the small provisional placeholder canvas with the
        actual LARGEST capture's size across the whole session (not the
        first one captured -- see _on_commit's own reasoning: locking
        in from whichever capture happened first risked a too-small
        canvas forcing bigger, later captures through the resize-to-fit
        pipeline unnecessarily). Reuses Project.new_blank's own metrics-
        proportion math (rather than only overwriting canvas_size)
        since cap height/x-height/baseline/descender are all fractions
        OF the canvas size -- leaving them computed against the old
        placeholder while only canvas_size changed would misplace every
        guide relative to the new, real box.

        Callers are expected to have already checked
        _confirm_tiny_capture_size themselves (and honored "restart"/
        "discard" before ever reaching here) -- this method only
        applies the size, it doesn't gate on tininess itself, since
        "accept" from that confirmation is exactly the signal to
        proceed unconditionally."""
        if not self.project or not self._capture_canvas_pending:
            return
        fresh = Project.new_blank(self.project.source_dir, width, height)
        self.project.metrics = fresh.metrics
        self._capture_canvas_pending = False
        self._refresh_metrics_label()
        self._show_metrics_view()
        self._set_status_message(
            f"Canvas size set from largest capture: {width} x {height}px")

    def _show_metrics_view(self):
        """Show the box + all five guides with no glyph loaded -- lets
        cap/x-height/baseline/descender/advance be positioned before
        any glyph exists to edit, which a from-scratch project needs
        (there's nothing in the glyph list yet to select). Also just a
        generally useful "adjust metrics without picking a glyph" view
        for existing projects."""
        if not self.project:
            return
        self.current_cp = None
        self.glyph_list.clearSelection()
        # Also clears the CURRENT item specifically, not just the
        # visible selection highlight -- confirmed as a real, reported
        # bug otherwise: clearSelection() alone doesn't touch
        # currentItem(), and currentItemChanged (what glyph selection
        # is wired to -- see _on_glyph_selected's own comment on why,
        # switched from itemClicked specifically so keyboard navigation
        # would work) only fires on an ACTUAL change of the current
        # item. Clicking the SAME glyph that was already current before
        # this ran -- the exact case of "I want the glyph I was just
        # looking at back" -- produced no signal at all, since Qt saw
        # no change to react to. Clicking any OTHER glyph worked by
        # accident, since that's a genuine change either way.
        self.glyph_list.setCurrentItem(None)
        m = self.project.metrics
        box = None
        if m.advance_px and m.cap_top_px is not None and m.descender_bottom_px is not None:
            box = (0, m.cap_top_px, m.advance_px, m.descender_bottom_px)
        guides = self._build_guides_dict(m)
        half_width = self.project.font.weight_ratio * ((m.baseline_px - m.cap_top_px) / 2) \
            if m.cap_top_px is not None and m.baseline_px is not None else 10.0
        max_half_width = self.weight_slider.maximum() / 100 * ((m.baseline_px - m.cap_top_px) / 2) \
            if m.cap_top_px is not None and m.baseline_px is not None else 40.0
        self.canvas.load_glyph([], half_width, self.project.font.cap_style, box, guides,
                                max_half_width=max_half_width,
                                source_bounds=tuple(m.canvas_size) if m.canvas_size else None)
        # Resize the whole ruler+strip+canvas grid to match the
        # content's own aspect ratio BEFORE reading back the view range
        # -- otherwise the canvas widget keeps whatever size the
        # splitter last gave it, which is what caused fitInView to
        # letterbox internally in the first place.
        self.center_panel.reflow()
        # Use the SAME exact mapToScene(viewport()) computation
        # _on_view_changed already uses for zoom/pan updates, rather
        # than a box+margin approximation -- fitInView's KeepAspectRatio
        # letterboxes whichever axis doesn't match the viewport's own
        # aspect ratio, so the approximation could differ substantially
        # from what's actually visible (worse the more the window's
        # proportions diverge from the box's, e.g. in full-screen).
        self._on_view_changed()
        self._set_status_message("Editing metrics -- drag the guide lines into place.")

    def eventFilter(self, obj, event):
        # Delete key on the glyph list -- QListWidget has no built-in
        # Delete handling, and this needs to apply to the WHOLE current
        # selection (see ExtendedSelection mode set on glyph_list),
        # matching how Explorer-style multi-select delete works.
        if obj is self.glyph_list and event.type() == QEvent.KeyPress \
                and event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            cps = [item.data(Qt.UserRole) for item in self.glyph_list.selectedItems()]
            cps = [cp for cp in cps if cp is not None]
            if cps:
                self._delete_glyphs(cps)
            return True
        # ALT+click on the glyph list -- picks a manual onion-skin
        # reference glyph (see _refresh_onion_skin) instead of loading
        # the clicked glyph for editing. Installed on the VIEWPORT (see
        # its own installEventFilter call for why), caught at
        # MouseButtonPress and consumed (return True) specifically so
        # Qt's own selection handling never sees this click at all --
        # without consuming it, currentItemChanged would still fire
        # right after this and load the clicked glyph into the canvas
        # for EDITING too, which defeats the whole point: ALT+click is
        # meant to compare against another glyph without disturbing
        # which one is actually being worked on.
        if obj is self.glyph_list.viewport() and event.type() == QEvent.MouseButtonPress \
                and event.button() == Qt.LeftButton and event.modifiers() & Qt.AltModifier:
            item = self.glyph_list.itemAt(event.pos())
            if item is not None:
                cp = item.data(Qt.UserRole)
                if cp is not None:
                    self._onion_skin_override_cp = cp
                    self._refresh_onion_skin()
            return True
        return super().eventFilter(obj, event)

    def _on_canvas_context_menu_requested(self, global_pos):
        # Reuses the SAME QActions the toolbar buttons/keyboard
        # shortcuts already use, rather than duplicating their enabled-
        # state logic here -- cut_action/copy_action are already kept
        # correctly enabled/disabled based on whether there's a
        # selection (see _on_selection_changed), and paste_action based
        # on whether the clipboard has content (see _on_copy_clicked/
        # _on_cut_clicked) -- exactly the same rules this menu should
        # follow, so there's nothing to recompute. Same reasoning
        # extends to break_action/join_action/select_all_toolbar_action
        # below: _on_selection_changed already manages their enabled
        # state too, and more precisely than a plain "is anything
        # selected" check would -- can_break specifically requires a
        # multi-point or grouped path, can_join requires at least two
        # distinct paths, not just any selection at all.
        menu = QMenu(self)
        menu.addAction(self.cut_action)
        menu.addAction(self.copy_action)
        menu.addAction(self.paste_action)
        delete_action = menu.addAction("Delete")
        delete_action.setEnabled(bool(self.canvas.selected_segments))
        delete_action.triggered.connect(self._on_delete_requested)
        menu.addSeparator()
        menu.addAction(self.break_action)
        menu.addAction(self.join_action)
        menu.addAction(self.select_all_toolbar_action)
        menu.exec(global_pos)

    def _on_glyph_list_context_menu(self, pos):
        item = self.glyph_list.itemAt(pos)
        if item is None:
            return
        # Right-clicking an item that's already part of the current
        # multi-selection acts on the WHOLE selection (Explorer-style);
        # right-clicking one that ISN'T replaces the selection with just
        # that item first, same as Explorer does.
        if item not in self.glyph_list.selectedItems():
            self.glyph_list.clearSelection()
            item.setSelected(True)
        selected_cps = [i.data(Qt.UserRole) for i in self.glyph_list.selectedItems()]
        selected_cps = [cp for cp in selected_cps if cp is not None]
        if not selected_cps:
            return

        menu = QMenu(self)
        exclude_action = include_action = None
        if len(selected_cps) == 1:
            # A genuine toggle for exactly one glyph -- its own current
            # state has an unambiguous single label to flip.
            currently_excluded = self.project.glyphs[selected_cps[0]].excluded
            if currently_excluded:
                include_action = menu.addAction("Include (restore to export)")
            else:
                exclude_action = menu.addAction("Exclude (skip from export)")
        else:
            # A mixed selection has no single current state to toggle
            # FROM, so both directions are offered as their own,
            # always-present actions instead -- each unconditionally
            # sets every selected glyph to that one state, overriding
            # whatever each one's own prior state happened to be,
            # rather than trying to compute one toggle label out of a
            # selection that may already be a mix of both.
            exclude_action = menu.addAction(f"Exclude {len(selected_cps)} glyphs (skip from export)")
            include_action = menu.addAction(f"Include {len(selected_cps)} glyphs (restore to export)")
        menu.addSeparator()
        if len(selected_cps) == 1:
            remove_action = menu.addAction(f"Remove U+{selected_cps[0]:04X} from project")
        else:
            remove_action = menu.addAction(f"Remove {len(selected_cps)} glyphs from project")

        chosen = menu.exec(self.glyph_list.mapToGlobal(pos))
        if chosen is not None and chosen == exclude_action:
            self._set_glyphs_excluded(selected_cps, True)
        elif chosen is not None and chosen == include_action:
            self._set_glyphs_excluded(selected_cps, False)
        elif chosen == remove_action:
            self._delete_glyphs(selected_cps)

    def _set_glyphs_excluded(self, cps: list[int], excluded: bool) -> None:
        """Sets recipe.excluded on every one of cps unconditionally to
        the SAME value -- overriding whatever each glyph's own prior
        state happened to be, rather than trying to preserve a mix.
        Confirmed as the wanted behavior for a multi-selection: there's
        no single sensible "toggle" for a set of glyphs that may
        already disagree with each other, so Exclude/Include are two
        explicit, always-available actions instead of one toggle (see
        _on_glyph_list_context_menu's own comment).

        Not itself undoable via Ctrl+Z -- matches Delete/Remove from
        project's own, already-established convention (a structural,
        whole-glyph change, not a segment-level content edit) rather
        than trying to force it onto the same per-glyph edit stack that
        segment-level undo/redo already owns."""
        if not self.project:
            return
        changed_any = False
        for cp in cps:
            recipe = self.project.glyphs.get(cp)
            if recipe is not None and recipe.excluded != excluded:
                recipe.excluded = excluded
                changed_any = True
                self._refresh_glyph_icon(cp)
        if not changed_any:
            return
        if self.current_cp in cps:
            # The currently-viewed glyph's own editability needs to
            # reflect its new state immediately, not just its list icon
            # -- _load_glyph_into_canvas already calls
            # _refresh_editing_locked_state internally, so this alone
            # is enough to lock/unlock the canvas and toolbar actions
            # to match.
            self._load_glyph_into_canvas(self.current_cp, preserve_view=True)
        if len(cps) == 1:
            self._set_status_message(
                f"U+{cps[0]:04X} excluded from export." if excluded
                else f"U+{cps[0]:04X} included in export again.")
        else:
            self._set_status_message(
                f"{len(cps)} glyphs excluded from export." if excluded
                else f"{len(cps)} glyphs included in export again.")

    def _remove_glyphs_from_project(self, cps: list[int]) -> None:
        """The actual removal itself -- no confirmation dialog, no
        status message, just the project/canvas state changes. Shared
        between _delete_glyphs (which wraps this with its own
        confirmation and "Deleted..." message) and _undo's own "this
        glyph didn't exist before this point" branch (which needs the
        identical removal, but silently and without asking, since
        undo is never itself a confirmable action).

        A soft delete -- only the recipe entry is removed from the
        project, not the underlying source PNG on disk (if any). That
        file staying put is harmless (nothing still references it),
        and NOT touching disk keeps this reversible in the one way
        that matters most: re-adding the same codepoint later, from
        the same file, still works."""
        cps = [cp for cp in cps if cp in self.project.glyphs]
        if not cps:
            return
        was_current = self.current_cp in cps
        for cp in cps:
            del self.project.glyphs[cp]
            self._paths_cache.pop(cp, None)
        if was_current:
            self.current_cp = None   # see _import_folder for why this must happen BEFORE the list rebuild
        self._refresh_glyph_list()
        if was_current:
            remaining = sorted(self.project.glyphs.keys())
            if remaining:
                self._load_glyph_into_canvas(remaining[0])
            else:
                self._blank_canvas()

    def _delete_glyphs(self, cps: list[int]):
        """Handles both the single- and multi-selection case with one
        shared confirmation dialog -- singular wording for exactly one
        codepoint (matching the previous single-glyph message users
        already saw), a count for more than one."""
        if not self.project:
            return
        cps = [cp for cp in cps if cp in self.project.glyphs]
        if not cps:
            return
        if len(cps) == 1:
            question = f"Delete U+{cps[0]:04X} from this project? This can't be undone."
        else:
            question = f"Delete {len(cps)} glyphs from this project? This can't be undone."
        reply = QMessageBox.question(
            self, "Delete glyph" if len(cps) == 1 else "Delete glyphs",
            question, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self._remove_glyphs_from_project(cps)
        if len(cps) == 1:
            self._set_status_message(f"Deleted U+{cps[0]:04X}.")
        else:
            self._set_status_message(f"Deleted {len(cps)} glyphs.")

    def _finalize_blank_glyph_canvas_if_pending(self) -> str:
        """Shows the same review-and-edit canvas-size prompt every
        other from-scratch finalization now uses (see
        CanvasSizePromptDialog), pre-filled with whatever was sitting
        in the New Project dialog's own width/height fields (the
        retained size, or the 300x500 fallback) -- confirmed as a
        real, reported gap otherwise: this was the only from-scratch
        path left that still finalized silently, after every other one
        (File, Capture, Commit, Close keep pending) already got the
        same review step.

        Returns "accept" (finalized, or nothing was pending to begin
        with) or "restart" (the user cancelled the prompt -- back to a
        fresh AddGlyphDialog, matching every other from-scratch
        cancellation in this same flow)."""
        if not self._capture_canvas_pending:
            return "accept"
        size = self._last_new_project_field_size
        chosen = self._prompt_canvas_size(
            "No canvas exists yet for this project. This is the current default "
            "\u2014 edit it if you'd like, or accept it as is.",
            size.width(), size.height())
        if chosen is None:
            return "restart"
        self._finalize_capture_canvas_size(chosen.width(), chosen.height())
        return "accept"

    def _abandon_add_glyph_attempt(self) -> None:
        """Called at every point _add_blank_glyph's own flow ends
        without ever committing a real glyph -- a plain Cancel, or
        Discard from the tiny-size warning (a cancelled capture now
        loops back instead -- see _add_blank_glyph's own comment).
        When this was a from-scratch attempt (_capture_canvas_pending
        still true -- see _add_blank_glyph's own forced-True comment),
        fully reverts the project that was created purely to attempt
        this, exactly as if it never happened -- see
        _revert_uncommitted_capture_project's own docstring, shared
        with Capture Glyphs' own Discard for the identical situation.
        When working within an already-existing project instead,
        there's nothing pending to begin with and nothing to do here
        at all."""
        if self._capture_canvas_pending:
            self._revert_uncommitted_capture_project()

    def _try_immediate_multi_file_import(self, file_paths: list[str]) -> dict[int, str] | None:
        """Returns a resolved {codepoint: file_path} mapping, skipping
        MultiFileAssignmentDialog entirely, ONLY when every condition
        below holds -- any ambiguity or conflict at all returns None,
        meaning "fall back to the full assignment window instead",
        which already handles every one of these cases correctly with
        per-file review and confirmation. Confirmed as the intended
        scope: the assignment window only ever counted as overkill (the
        reason this shortcut exists) when there was genuinely nothing
        left to decide -- every file's own name already,
        unambiguously said which codepoint it's for, none collided with
        each other, and none collided with a codepoint the project
        already has.

        - Every file's own name must follow the u<hex>.png convention
          (see _guess_codepoint_from_filename) -- a single ambiguously-
          named file anywhere in the batch means falling back to the
          window instead, not guessing or leaving it out silently.
        - No two files may resolve to the same codepoint.
        - None of the resolved codepoints may already exist in this
          project -- an existing-glyph overwrite is exactly the kind of
          decision the assignment window's own per-row confirmation
          exists for, not something to skip past without asking."""
        guessed = [_guess_codepoint_from_filename(os.path.basename(p)) for p in file_paths]
        if any(cp is None for cp in guessed):
            return None
        if len(set(guessed)) != len(guessed):
            return None
        if set(guessed) & set(self.project.glyphs.keys()):
            return None
        return dict(zip(guessed, file_paths))

    def _add_blank_glyph(self):
        if not self.project:
            # No width/height fields at all here -- not just a
            # checkbox gating them, the fields themselves are entirely
            # absent (see NewProjectDialog's own __init__ comment).
            # Confirmed as a deliberate removal, not a bug: there's no
            # way to know the right size at this point anyway --
            # File... still overrides whatever ends up here with its
            # own real content's dimensions (see below), and
            # Blank/Cancel always fall back to the same hardcoded
            # default regardless. Multi-capture's own use of this SAME
            # dialog got the identical treatment, for the identical
            # reason -- see start_or_resume's own comment.
            if not self._create_new_project_via_dialog(size_label="Canvas", show_size_fields=False,
                                                         defer_canvas_draw=True):
                return
            # Forced True regardless of the dialog -- keeps
            # _finalize_pending_canvas_from_file's own override logic
            # active for this flow (a real size is only ever settled by
            # what File/Blank actually resolves to below, never by this
            # dialog alone).
            self._capture_canvas_pending = True
        # Loops back to the SAME dialog on "don't overwrite", rather
        # than exiting the whole flow -- declining to overwrite means
        # "let me pick a different character", not "cancel adding a
        # glyph entirely".
        while True:
            dialog = AddGlyphDialog(self)
            result = dialog.exec()
            if result != QDialog.Accepted:
                if dialog.multi_file_paths is not None:
                    # AddGlyphDialog._browse_file always routes here now,
                    # one file or several alike (see its own docstring
                    # for why: the codepoint is asked for AFTER the pick,
                    # with a real preview, rather than committed to
                    # beforehand). _try_immediate_multi_file_import
                    # handles the genuinely unambiguous case (every
                    # file's own name already says which codepoint it's
                    # for, none colliding with each other or the
                    # project) without ever showing the assignment
                    # window at all -- any ambiguity or conflict falls
                    # back to that window, which already handles every
                    # one of those cases correctly with per-file review
                    # and confirmation.
                    assignments = self._try_immediate_multi_file_import(dialog.multi_file_paths)
                    if assignments is None:
                        assignment_dialog = MultiFileAssignmentDialog(
                            dialog.multi_file_paths,
                            existing_codepoints_cb=lambda: set(self.project.glyphs.keys()),
                            parent=self)
                        if assignment_dialog.exec() != QDialog.Accepted:
                            continue   # cancelled the whole batch -- back to a fresh AddGlyphDialog
                        assignments = assignment_dialog.get_assignments()
                        if not assignments:
                            continue   # every row ended up deleted/unassigned -- back to a fresh AddGlyphDialog
                    # Canvas-size finalization only ever matters for the
                    # very FIRST file added to a still-pending, from-
                    # scratch project (see _finalize_pending_canvas_
                    # from_file's own early-return once
                    # _capture_canvas_pending is already cleared) --
                    # resolved once, up front, against whichever file
                    # happens to be first in this dict, rather than
                    # repeated (harmlessly, but needlessly) inside a
                    # per-file loop.
                    first_file = next(iter(assignments.values()))
                    decision = self._finalize_pending_canvas_from_file(first_file)
                    if decision == "restart":
                        continue   # back to a fresh AddGlyphDialog, matching File...'s own single-file "restart" behavior
                    for cp in assignments:
                        self._paths_cache.pop(cp, None)
                    # _commit_new_glyphs_batch, not a per-file loop
                    # calling _commit_new_glyph -- see its own docstring
                    # for the confirmed, measured cost of rebuilding the
                    # entire glyph list after every single file in a
                    # batch that could be dozens long.
                    self._commit_new_glyphs_batch(assignments)
                    return
                # Genuinely abandoned here -- a plain Cancel. Nothing
                # was committed.
                self._abandon_add_glyph_attempt()
                return
            cp = dialog.codepoint
            if cp in self.project.glyphs:
                reply = QMessageBox.question(
                    self, "Already exists",
                    f"U+{cp:04X} is already in this project. Overwrite it?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if reply != QMessageBox.Yes:
                    continue
                self._paths_cache.pop(cp, None)
            if self._capture_canvas_pending:
                # Blank -- same review-and-edit prompt every other
                # from-scratch path uses now, pre-filled with the New
                # Project dialog's own width/height fields, since blank
                # has no file content of its own to derive or override
                # a size from otherwise. This is the only path that
                # still reaches here at all now (see this while loop's
                # own "not Accepted" branch above for File..., which
                # never falls through to this point) -- dialog.file_path
                # is therefore always None by the time this runs.
                if self._finalize_blank_glyph_canvas_if_pending() == "restart":
                    continue   # back to a fresh AddGlyphDialog
            break

        self._commit_new_glyph(cp, None, "")

    def _finalize_pending_canvas_from_file(self, file_path: str) -> str:
        """If this project's canvas size is still pending finalization
        (created via _add_blank_glyph's own "no project yet" branch,
        with no real size established yet), asks the user to confirm
        or adjust a size now, pre-filled with THIS file's own
        dimensions -- replaces the earlier silent finalization (with
        only a too-small warning) entirely, per the confirmed redesign:
        every from-scratch canvas finalization now goes through this
        same review step instead (see CanvasSizePromptDialog and
        _confirm_tiny_capture_size, the latter no longer used by any
        caller).

        Returns "accept" (finalized, or nothing was pending to begin
        with -- either way the caller should proceed normally with
        committing the glyph) or "restart" (the user cancelled the
        prompt -- back to a fresh AddGlyphDialog, per the confirmed
        design)."""
        if not self._capture_canvas_pending:
            return "accept"
        try:
            from PIL import Image
            with Image.open(file_path) as im:
                w, h = im.size
        except Exception:
            return "accept"   # can't read it -- leave the placeholder canvas size; _import_single_png_for_glyph will still report the real error
        chosen = self._prompt_canvas_size(
            "No canvas exists yet for this project. This is the size of what "
            "you just provided \u2014 edit it if you'd like, or accept it as is.",
            w, h)
        if chosen is None:
            return "restart"
        self._finalize_capture_canvas_size(chosen.width(), chosen.height())
        return "accept"

    def _add_glyph_recipe(self, cp: int, file_path: str | None) -> str | None:
        """Core, per-glyph "bring a new glyph's recipe into existence"
        step -- imports the source file if there is one, creates the
        recipe, and records the resize needed to fit an oversized
        source to the canvas as import_scale (see GlyphRecipe.
        import_scale's own docstring for why this is kept separate
        from edits, not appended to it as a scale_uniform EditStep the
        way it used to be). Shared by _commit_new_glyph (one glyph) and
        _commit_capture_session (one call per queued item) -- confirmed
        as a real, reported bug when this was two separately-maintained
        copies instead: _commit_capture_session's own copy never got
        the later setCurrentItem fix _commit_new_glyph did, since
        nothing forced the two to actually stay in sync with each
        other.

        Neither adding a new glyph nor replacing an existing one's
        source file is undoable via Ctrl+Z -- confirmed, deliberately,
        as the same conclusion already reached for Reassign before it
        was removed entirely: a structural, whole-glyph change
        (codepoint identity and existence for an addition; source_file/
        import_scale for a replacement) sharing one stack with pure,
        segment-level content edits (draw/cut/join/paste) is exactly
        the mismatch that caused Reassign's own crash and erratic
        multi-click undo behavior. "Undo" an addition by selecting the
        glyph and using Remove from project instead (already fully
        supported, already a soft delete); "undo" a replacement by
        capturing or loading the file again -- source PNGs are never
        touched or lost, so this is always possible. The one
        acknowledged cost: any segment-level edits already made to a
        glyph before it gets replaced are lost along with the old
        source_file/import_scale, with no way back via undo -- an
        accepted, minor trade-off for a mechanism that no longer
        creates the reported problems.

        Returns the resolved source_file name (empty string for a
        blank glyph) on success, or None if reading/copying the file
        failed (already reported to the user) -- callers should skip/
        abort for this cp on None, not assume the recipe exists."""
        source_file = ""
        resize_factor = None
        if file_path:
            result = self._import_single_png_for_glyph(file_path, cp)
            if result is None:
                return None
            source_file, resize_factor = result

        recipe = self.project.add_glyph(cp, source_file)
        if resize_factor is not None:
            recipe.import_scale = resize_factor
        return source_file

    def _commit_new_glyph(self, cp: int, file_path: str | None, source_description: str) -> bool:
        """Shared tail for every way a single new glyph can be added --
        File..., blank, and Capture all end up here once they've each
        independently resolved where the source file (if any) comes
        from. Returns True on success, False if reading/copying the
        file failed (already reported to the user)."""
        source_file = self._add_glyph_recipe(cp, file_path)
        if source_file is None:
            return False
        self._refresh_glyph_list()
        self._load_glyph_into_canvas(cp)
        # Confirmed as a real, reported gap otherwise: this loaded the
        # new glyph into the CANVAS correctly, but never told the list
        # widget to select/highlight the corresponding item -- the
        # same pattern _import_folder's own tail already gets right.
        self._select_glyph_in_list(cp)
        if source_file:
            self._set_status_message(f"Added U+{cp:04X} from {source_description}.")
        else:
            self._set_status_message(f"Added U+{cp:04X} -- empty, ready to draw.")
        return True

    def _commit_new_glyphs_batch(self, assignments: dict[int, str]) -> int:
        """Same underlying work as calling _commit_new_glyph once per
        (codepoint, file_path) pair, but refreshes the glyph list and
        loads the final glyph into the canvas ONCE at the end, not
        after every single one -- see AddGlyphDialog's own
        multi-selection branch for the caller.

        Confirmed as a real, measured cost otherwise, not just
        theoretical: _commit_new_glyph's own _refresh_glyph_list call
        rebuilds the ENTIRE list (clear, then re-trace and re-render
        every glyph's own icon) from scratch every time it runs. Called
        once per file in a loop, a batch of N files rebuilt the whole,
        already-growing list N times in a row -- quadratic in the
        number of files, not linear, and a plausible source of the
        rendering artifacts reported on a real desktop (rapid, repeated
        clear()-then-repopulate cycles racing against Qt's own
        in-flight paint events) that a single, clean rebuild at the end
        avoids entirely.

        Returns the number of glyphs actually added; individual file
        failures are reported the same way _add_glyph_recipe's own
        caller already handles them, and don't stop the rest of the
        batch from proceeding."""
        last_cp = None
        added = 0
        for cp, file_path in assignments.items():
            source_file = self._add_glyph_recipe(cp, file_path)
            if source_file is None:
                continue   # already reported to the user by _import_single_png_for_glyph itself
            last_cp = cp
            added += 1
        if added == 0:
            return 0
        self._refresh_glyph_list()
        self._load_glyph_into_canvas(last_cp)
        self._select_glyph_in_list(last_cp)
        self._set_status_message(
            f"Added U+{last_cp:04X}." if added == 1 else f"Added {added} glyphs.")
        return added

    def _import_single_png_for_glyph(self, file_path: str, cp: int) -> tuple[str, float | None] | None:
        """Copies an arbitrary, individually-chosen PNG into the
        project's source_dir (as u<hex>.png, matching the bulk-import
        naming convention everything else expects) for exactly one
        glyph -- distinct from scanning a whole folder of them.

        If the image exceeds the project's own canvas size in either
        dimension, a resize-to-fit factor is returned for the caller to
        record as a scale_uniform EditStep -- scaling the ALREADY-
        TRACED skeleton afterward (see geometry.scale_uniform), not
        downscaling the source bitmap before tracing, which would cost
        real skeleton precision for no benefit. Returns
        (source_file_name, resize_factor_or_None), or None if reading/
        copying the file failed (already reported to the user)."""
        try:
            from PIL import Image
            with Image.open(file_path) as im:
                src_w, src_h = im.size
        except Exception as e:
            QMessageBox.critical(self, "Couldn't read image", str(e))
            return None

        dest_name = f"u{cp:04x}.png"
        # Direct-to-source_dir ONLY when this codepoint is genuinely
        # new to a project created fresh in THIS session -- that first
        # write IS what establishes the glyph's own original PNG,
        # nothing yet exists there to protect. Every OTHER case (an
        # opened/imported project, regardless of codepoint, OR a
        # SECOND write for an already-existing codepoint even in a
        # fresh project) goes to staging instead, leaving whatever's
        # already in source_dir completely untouched -- see
        # Project.resolve_source_path/stage_source_file's own
        # docstrings for the full reasoning behind this split.
        writes_direct = bool(self._project_created_fresh) and cp not in self.project.glyphs
        try:
            if writes_direct:
                shutil.copyfile(file_path, os.path.join(self.project.source_dir, dest_name))
            else:
                self.project.stage_source_file(dest_name, file_path)
        except OSError as e:
            QMessageBox.critical(self, "Couldn't copy file", str(e))
            return None

        resize_factor = None
        canvas_size = self.project.metrics.canvas_size
        if canvas_size:
            cw, ch = canvas_size
            if src_w > cw or src_h > ch:
                resize_factor = min(cw / src_w, ch / src_h)
        return dest_name, resize_factor

    # -- Capture Glyphs (screen-capture-to-glyph session) -----------------

    def _blank_canvas(self):
        """Shows a genuinely empty canvas -- no paths, no box, no
        guides. _show_metrics_view refuses to run at all when
        self.project is None (there's nothing to show metrics OF), so
        without this, clearing or reverting a project left whatever the
        canvas last displayed just sitting there instead of reflecting
        the now-empty state.

        Also resets the ruler/guide-label strips -- confirmed as a
        real, reported gap otherwise: those live as separate widgets
        (GuideLabelStrip/AdvanceLabelStrip -- see GuideLine's own
        docstring on why they're not just fixed children of the guide
        lines themselves), untouched by canvas.load_glyph(), so Reset
        Workspace cleared the canvas itself but left the previous
        project's guide labels/ruler values still showing."""
        self.current_cp = None
        self.canvas.load_glyph([], 10.0, "round", None, {}, max_half_width=40.0, source_bounds=None)
        self.ruler.set_values(None, None, None, None, average_advance=self._average_advance())
        self.vertical_ruler.set_values(None, None)
        self.guide_strip.set_view(None, None)
        self.advance_strip.set_view(None, None)

    def _clear_project(self):
        """Memory-only reset for the project itself -- nothing on disk
        is touched, matching the same "soft" convention as single-glyph
        deletion elsewhere in this file. Two separate confirmations on
        purpose when a project IS loaded: the existing dirty-check (in
        case there ARE unsaved changes worth keeping/saving first) and
        then a distinct confirmation for the clear itself, since even a
        project with nothing unsaved is still something the user might
        not have meant to lose access to mid-session.

        No longer requires a project to be loaded at all -- confirmed
        as a real, reported gap otherwise: the persistent-settings
        checkbox below (browsed folders, the last capture size, the
        crash-recovery pointer, etc. -- see app_settings.
        clear_all_persistent_settings's own docstring for the full
        list) is just as meaningful with nothing currently open as
        with something open, so this dialog needs to stay reachable
        either way rather than being gated on self.project."""
        if not self._confirm_discard_if_dirty():
            return

        if self.project is not None:
            message = ("Remove the current project from memory? Nothing on disk is deleted -- "
                       "you can reopen it the same way you opened it this time.")
        else:
            message = "No project is currently open. Reset the workspace anyway?"
        box = QMessageBox(QMessageBox.Question, "Clear project", message,
                           QMessageBox.Yes | QMessageBox.No, self)
        box.setDefaultButton(QMessageBox.No)
        reset_settings_check = QCheckBox("Also reset all persistent settings (browsed folders, "
                                          "last capture size, crash-recovery pointer, font preferences)")
        box.setCheckBox(reset_settings_check)
        reply = box.exec()
        if reply != QMessageBox.Yes:
            return

        if self.project is not None:
            self.project = None
            self._saved_snapshot = None
            self._saved_project_path = None
            self._refresh_project_name_label()
            self._capture_canvas_pending = False
            self._project_created_for_capture_session = False
            # Reset here too, not just when a different project is
            # created/loaded -- clearing counts as "leaving" this
            # project just as much as switching to another one does.
            self._onion_skin_override_cp = None
            self.current_cp = None
            self._paths_cache.clear()
            self._refresh_glyph_list()
            self._refresh_metrics_label()
            self._blank_canvas()

        if reset_settings_check.isChecked():
            app_settings.clear_all_persistent_settings()
            self._set_status_message(
                "Workspace and all persistent settings cleared." if self.project is None
                else "Project cleared and all persistent settings reset.")
        else:
            self._set_status_message("Project cleared -- create a new one or open an existing one.")

    def _revert_uncommitted_capture_project(self):
        """Called whenever a project that was created purely for THIS
        attempt at adding a first glyph -- Capture Glyphs' own
        CaptureSessionController._on_discard, or _add_blank_glyph's own
        abandonment points (Cancel, a cancelled capture, Discard from
        the tiny-size warning) -- ends without anything ever having
        been committed into it. Silently reverts to no project at all,
        since abandoning should mean "as if this never happened", not
        "keep an otherwise-empty project with a placeholder or
        finalized canvas" -- and forgets it for crash-recovery purposes
        too (see forget_last_project's own docstring for why this is a
        legitimate place to call it): there's nothing left to ever
        recover if the app crashed right after this ran. No
        confirmation here -- whichever action led here already
        confirmed the user's intent on its own."""
        self.project = None
        self._project_created_fresh = None   # nothing to be fresh or not fresh about -- see __init__'s own comment
        self._saved_snapshot = None
        self._saved_project_path = None
        self._capture_canvas_pending = False
        self._project_created_for_capture_session = False
        self._onion_skin_override_cp = None
        self.current_cp = None
        self._paths_cache.clear()
        capture_settings.forget_last_project()
        self._refresh_glyph_list()
        self._refresh_metrics_label()
        self._blank_canvas()

    def _read_capture_item_dimensions(self, store) -> list[tuple[int, int, int]]:
        """Returns (seq, width, height) for every item currently in the
        given CaptureSessionStore, read directly from each saved PNG.
        Used at commit time to find the largest capture across the
        WHOLE session (see CaptureSessionController._on_commit) -- kept
        here rather than in the capture package since it's a thin,
        Qt-only helper with no session-state of its own."""
        from PySide6.QtGui import QImage
        sizes = []
        for item in store.items:
            img = QImage(store.item_path(item))
            if img.isNull():
                continue
            sizes.append((item.seq, img.width(), img.height()))
        return sizes

    def _start_capture_session(self):
        self._capture_session.start_or_resume()

    def _commit_capture_session(self, assigned: list[tuple[int, int, str]]) -> list[int]:
        """assigned: (seq, codepoint, source_png_path) for every queued
        capture that currently has a codepoint typed in. Returns the
        seqs actually committed. Reuses _import_single_png_for_glyph
        exactly as-is: it already does precisely what's needed here
        (copy into source_dir as u<hex>.png, compute a resize-to-fit
        factor against the project's canvas size), just called once per
        queued item instead of once per AddGlyphDialog invocation.

        No overwrite-conflict prompt here anymore -- that used to run
        per colliding item at commit time, meaning a batch of several
        conflicts produced a burst of separate popups all at once. It's
        now confirmed once, immediately, the moment each codepoint is
        actually typed into the assignment window (see
        CaptureWindow._on_row_commit) -- by the time a
        conflicting item reaches this method it's already been
        approved, so re-asking here would just be the same redundant
        burst this was meant to remove."""
        if not self.project:
            return []
        committed_seqs: list[int] = []
        any_added = False
        for seq, cp, source_png_path in assigned:
            if cp in self.project.glyphs:
                self._paths_cache.pop(cp, None)

            if self._add_glyph_recipe(cp, source_png_path) is None:
                continue   # read/copy failure already reported -- leave it queued
            committed_seqs.append(seq)
            any_added = True

        if any_added:
            self._refresh_glyph_list()
            first_committed_cp = next(
                (cp for _seq, cp, _path in assigned if _seq in committed_seqs), None)
            if first_committed_cp is not None:
                self._load_glyph_into_canvas(first_committed_cp)
                # Same fix as _commit_new_glyph's own tail -- confirmed
                # as a real, reported gap: this was a separately-
                # maintained copy of that same commit logic, and never
                # got the setCurrentItem fix the other one did.
                self._select_glyph_in_list(first_committed_cp)
            self._set_status_message(f"Committed {len(committed_seqs)} captured glyph(s).")
        return committed_seqs

    def _remember_last_project(self):
        """See capture/settings.py -- tracks the current project so a
        crashed run can be traced back to its capture session, if any,
        on the next startup. Cleared only on a confirmed-normal exit
        (closeEvent)."""
        if not self.project:
            return
        capture_settings.remember_last_project(
            self.project.source_dir, self._saved_project_path)

    def _load_project_for_recovery(self, source_dir: str, saved_path: str | None) -> bool:
        """Shared by both "Resume Now" and "Later" in
        _check_capture_recovery -- loads a live Project against
        source_dir, preferring the exact saved file if one was tracked,
        falling back to a folder re-scan (same reconstruction
        _import_folder already does) for a project that had never been
        saved before the abnormal exit. Returns True on success, with
        self.project/self._saved_snapshot/self._saved_project_path all
        set; False if it failed (already reported to the user)."""
        try:
            if saved_path and os.path.exists(saved_path):
                self.project = Project.load(saved_path)
                self._project_created_fresh = False
                self._saved_snapshot = self.project.to_json()
                self._saved_project_path = saved_path
            else:
                self.project = Project(source_dir=source_dir,
                                        font=FontSettings(family_name=os.path.basename(source_dir.rstrip("/")) or "New Font"))
                self.project.scan_source_dir()
                # Recovering a project that crashed before it was EVER
                # saved -- it was created fresh originally (nothing to
                # load from disk otherwise), and this is simply
                # continuing that same, still-unsaved session, not
                # opening an existing one. See _create_new_project_via_
                # dialog's own comment for the full reasoning behind
                # this flag.
                self._project_created_fresh = True
                self._saved_snapshot = None
                self._saved_project_path = None
            self._refresh_project_name_label()
            return True
        except Exception as e:
            QMessageBox.critical(self, "Couldn't reload project",
                                  f"Found a pending capture session, but couldn't reload its "
                                  f"project to resume into:\n{e}\n\n"
                                  "The capture session itself has been left in place.")
            return False

    def _detect_pending_canvas_after_recovery_load(self) -> None:
        """Shared by both "Resume Now" and "Later" in
        _check_capture_recovery -- sets _capture_canvas_pending/
        _project_created_for_capture_session correctly for a project
        just loaded via _load_project_for_recovery. A resumed project
        can genuinely have no real canvas size yet -- the interrupted
        session may never have reached a commit/finalize step before
        the crash that necessitated this recovery in the first place.
        Confirmed as a real, reported crash otherwise:
        _enter_interact_state's own "not pending" branch unconditionally
        reads project.metrics.canvas_size[0], which is None by default
        (see Metrics' own definition) -- nothing else ever sets
        _capture_canvas_pending for a project loaded this way, unlike
        start_or_resume's own "create a new project" branch, which
        always does."""
        self._capture_canvas_pending = self.project.metrics.canvas_size is None
        if self._capture_canvas_pending and not self.project.glyphs:
            # Only when there's truly nothing real in it yet either --
            # matches _project_created_for_capture_session's own
            # meaning precisely (see _revert_uncommitted_capture_
            # project's own docstring): a resumed project that already
            # has real committed glyphs must never be treated as
            # revert-eligible just because its canvas size specifically
            # never got finalized.
            self._project_created_for_capture_session = True

    def _check_capture_recovery(self):
        """Called once, near the end of __init__, before this window is
        ever shown -- see the docstring at the call site for why this
        runs synchronously here rather than deferred through
        QTimer.singleShot (avoids a visible flash of the main window
        before a blocking "another instance is recovering" message)."""
        if capture_instance_lock.is_locked_by_live_process():
            QMessageBox.warning(
                self, "PNGlyph is already recovering a session",
                "Another PNGlyph window is currently recovering an interrupted "
                "capture session. Finish, discard, or defer that recovery there "
                "before opening a new window.")
            import sys
            sys.exit(0)

        pending = capture_settings.get_last_project()
        if pending is None:
            return   # last run ended normally -- nothing to check
        source_dir, saved_path = pending

        if not os.path.isdir(source_dir) or not CaptureSessionStore.has_pending_session(source_dir):
            capture_settings.forget_last_project()
            return

        capture_instance_lock.acquire()
        msg_box = QMessageBox(
            QMessageBox.Question, "Interrupted capture session found",
            f"PNGlyph didn't close normally last time, and there's an unfinished "
            f"capture session in:\n{source_dir}",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, self)
        # Custom text on the standard Yes/No/Cancel buttons -- the
        # button objects themselves (and QMessageBox.question's usual
        # Yes/No/Cancel return values) stay the same underneath, only
        # the labels change, so the reply-handling below needs no
        # changes for this.
        msg_box.button(QMessageBox.Yes).setText("Resume")
        msg_box.button(QMessageBox.No).setText("Discard")
        msg_box.button(QMessageBox.Cancel).setText("Later")
        msg_box.setDefaultButton(QMessageBox.Yes)
        reply = msg_box.exec()
        # Yes = Resume Now, No = Discard, Cancel = Later (defer)

        if reply == QMessageBox.No:
            capture_settings.forget_last_project()   # nothing left to recover -- the pointer's job ends here
            CaptureSessionStore(source_dir).discard()
            capture_instance_lock.release()
            return
        if reply == QMessageBox.Cancel:
            # Later -- deliberately does NOT forget the remembered
            # project here. Confirmed as a real, reported bug
            # otherwise: forget_last_project() used to run
            # unconditionally above, before reply was even checked, so
            # "Later" ALSO cleared the very pointer the next startup
            # needs to rediscover this session and offer the same
            # choice again -- its whole documented purpose ("manifest
            # left intact, reachable via Capture Glyphs on that
            # project") only half held: the on-disk manifest genuinely
            # did survive, but nothing would ever automatically point
            # back to it again, making this indistinguishable from
            # Discard except for what happened to be left on disk.
            # Leaving the pointer alone means the NEXT startup asks
            # again, exactly as "Later" implies it should.
            #
            # Still loads the project and shows a real canvas, though
            # -- confirmed as a real, reported gap otherwise: returning
            # to a blank main screen gave no indication at all that
            # there was still an unassigned-captures session sitting
            # there, unlike "Close (keep pending)"'s own identical
            # situation, which already shows the canvas at the largest
            # pending capture's size. Doesn't open the assignment
            # dialog or touch the capture session itself in any way --
            # "Later" specifically means deferring that decision, not
            # making it here.
            if self._load_project_for_recovery(source_dir, saved_path):
                self._paths_cache.clear()
                self.current_cp = None
                self._onion_skin_override_cp = None
                self._detect_pending_canvas_after_recovery_load()
                self._refresh_glyph_list()
                if self._capture_canvas_pending:
                    store = CaptureSessionStore(source_dir)
                    # Confirmed as a real bug otherwise: this is a
                    # brand-new CaptureSessionStore, not the session
                    # controller's own already-loaded one (which isn't
                    # even constructed yet at this point in startup) --
                    # items stays empty, silently, unless the on-disk
                    # manifest is explicitly loaded first.
                    store.load_existing()
                    sizes = self._read_capture_item_dimensions(store)
                    if sizes:
                        areas = [(w * h, w, h) for _seq, w, h in sizes]
                        _max_area, default_w, default_h = max(areas)
                        message = ("No canvas exists yet for this project. The canvas will be "
                                   "set to the largest capture so far \u2014 edit it if you'd "
                                   "like, or accept it as is.")
                    else:
                        default_size = self._last_new_project_field_size
                        default_w, default_h = default_size.width(), default_size.height()
                        message = ("No canvas exists yet for this project, and nothing has "
                                   "been captured yet either. This is the current default "
                                   "\u2014 edit it if you'd like, or accept it as is.")
                    chosen = self._prompt_canvas_size(message, default_w, default_h)
                    if chosen is not None:
                        self._finalize_capture_canvas_size(chosen.width(), chosen.height())
                        # _finalize_capture_canvas_size already shows
                        # the canvas -- nothing further needed here.
                    else:
                        self._show_metrics_view()
                else:
                    first_cp = self._first_glyph_to_show()
                    if first_cp is not None:
                        self._load_glyph_into_canvas(first_cp)
                    else:
                        self._show_metrics_view()
            capture_instance_lock.release()
            return

        # Resume Now: the pointer's job also ends here -- this run is
        # already actively handling the session itself, so there's
        # nothing left for a FUTURE startup's abnormal-exit check to
        # detect or offer again.
        capture_settings.forget_last_project()

        if not self._load_project_for_recovery(source_dir, saved_path):
            capture_instance_lock.release()
            return

        self._paths_cache.clear()
        self.current_cp = None
        self._onion_skin_override_cp = None
        self._detect_pending_canvas_after_recovery_load()
        self._refresh_glyph_list()
        first_cp = self._first_glyph_to_show()
        if first_cp is not None:
            self._load_glyph_into_canvas(first_cp)
        else:
            self._show_metrics_view()

        self._capture_session.resume_from_recovery(source_dir)
        capture_instance_lock.release()

    def _import_folder(self):
        if not self._confirm_discard_if_dirty():
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Select folder of u<hex>.png files", app_settings.get_last_browsed_dir())
        if not folder:
            return
        folder = app_settings.normalize_path(folder)
        # Remembers the PARENT of the imported folder, not the folder
        # itself -- same reasoning as _create_new_project_via_dialog's
        # own fix: the imported folder IS a project-like directory, so
        # remembering it directly would pre-fill a LATER New Project
        # dialog's "Parent folder" with this one, nesting a new project
        # inside it instead of alongside it.
        app_settings.remember_last_browsed_dir(os.path.dirname(folder) or folder)
        cal = calibration.calibrate(folder)
        if cal.canvas_size is not None:
            w, h = cal.canvas_size
            if not _check_canvas_size_limits(self, w, h):
                return
            # Per-metric fallback, not all-or-nothing -- whichever of
            # cap/x-height/baseline/descender calibrate() DID
            # confidently measure stays exactly as measured; only the
            # specific one(s) it couldn't (None, with its own warning
            # explaining why -- see cal.warnings below) fall back to
            # default_metrics_for_canvas's own sensible proportions.
            # Confirmed as a real, reported bug otherwise: a
            # calibration warning telling the user to manually
            # position these guides, with no guide for any None metric
            # ever appearing on screen at all to actually do that with
            # -- _build_guides_dict skips building a guide entirely for
            # a still-None metric rather than falling back to
            # something with an actual position to drag from.
            fallback = Project.default_metrics_for_canvas(w, h)
            cal.cap_top_px = cal.cap_top_px if cal.cap_top_px is not None else fallback.cap_top_px
            cal.xheight_top_px = cal.xheight_top_px if cal.xheight_top_px is not None else fallback.xheight_top_px
            cal.baseline_px = cal.baseline_px if cal.baseline_px is not None else fallback.baseline_px
            cal.descender_bottom_px = cal.descender_bottom_px if cal.descender_bottom_px is not None else fallback.descender_bottom_px
            cal.advance_px = cal.advance_px if cal.advance_px is not None else fallback.advance_px
        self.project = Project(
            source_dir=folder,
            font=FontSettings(family_name=os.path.basename(folder.rstrip("/")) or "New Font"),
            metrics=Metrics(
                cap_top_px=cal.cap_top_px, baseline_px=cal.baseline_px,
                xheight_top_px=cal.xheight_top_px,
                descender_bottom_px=cal.descender_bottom_px,
                advance_px=cal.advance_px,
                canvas_size=list(cal.canvas_size) if cal.canvas_size else None,
            ),
        )
        # Explicitly False, not left to whatever the flag happened to
        # be before this ran -- confirmed as an explicit, deliberate
        # rule, not an incidental consequence of using the same
        # Project(...) constructor _create_new_project_via_dialog also
        # uses: Import folder is importing EXISTING content, not
        # creating an empty project from nothing, even though the two
        # are mechanically similar here. Every capture/File-add against
        # this project, new codepoint or not, goes to staging from here
        # on (see _import_single_png_for_glyph's own routing decision).
        self._project_created_fresh = False
        self._saved_snapshot = None   # brand new, never saved -- dirty until the first save
        self._saved_project_path = None
        self._refresh_project_name_label()
        self._remember_last_project()
        self._onion_skin_override_cp = None
        added = self.project.scan_source_dir()
        self._paths_cache.clear()   # see _new_blank_project for why this matters
        # current_cp must be cleared BEFORE _refresh_glyph_list runs, not
        # after -- _refresh_glyph_icon's fast path checks cp==current_cp
        # and, if true, reuses self.canvas.paths directly (skipping the
        # cache entirely). Clearing the cache alone wasn't sufficient:
        # current_cp was still pointing at the OLD project's selection
        # while the list rebuilt, so any codepoint shared between both
        # projects (e.g. 'A') hit that fast path and got the stale,
        # pre-import canvas contents baked right back into its icon.
        self.current_cp = None
        self._refresh_glyph_list(show_progress=True)
        self._refresh_metrics_label()
        # Loading the first real glyph (or falling back to the metrics
        # view if the folder somehow had none) guarantees the CANVAS
        # also reflects the current project immediately, not whatever
        # was on screen before it.
        first_cp = self._first_glyph_to_show()
        if first_cp is not None:
            self._load_glyph_into_canvas(first_cp)
            self._select_glyph_in_list(first_cp)
        else:
            self._show_metrics_view()
        msg = f"Imported {len(added)} glyphs."
        if cal.warnings:
            msg += "\n\n" + "\n".join(cal.warnings)
            box = QMessageBox(QMessageBox.Warning, "Calibration warnings", msg,
                               QMessageBox.Ok, self)
            # Monospace specifically for this dialog -- the mixed-size
            # warning's own padded, right-aligned width/height columns
            # (see calibration.py) only actually line up on screen in a
            # fixed-width font; QMessageBox's default proportional font
            # would make the padding spaces present but visually
            # meaningless, each row landing at a different pixel offset.
            box.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
            box.exec()
        self._set_status_message(msg.splitlines()[0])

    def _confirm_discard_if_dirty(self) -> bool:
        """Prompts to save unsaved changes before a destructive action
        (closing the app, opening a different project, starting a new
        one). Returns True if it's safe to proceed -- nothing was
        dirty, or the user chose to save or explicitly discard -- False
        if the user cancelled, in which case the caller must not
        continue with whatever it was about to do."""
        if self.project is None:
            return True
        if self.project.to_json() == self._saved_snapshot:
            return True
        reply = QMessageBox.question(
            self, "Unsaved changes",
            "This project has unsaved changes. Save before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if reply == QMessageBox.Cancel:
            return False
        if reply == QMessageBox.Save:
            self._save_project()
            # The save dialog itself may have been cancelled -- if so,
            # the project is still dirty, and this whole confirmation
            # should read as cancelled too, not as a green light.
            return self.project.to_json() == self._saved_snapshot
        return True   # Discard

    def closeEvent(self, event):
        if self._confirm_discard_if_dirty():
            # A NORMAL exit -- this is the only place the crash pointer
            # is cleared (see capture/settings.py). Its continued
            # presence at the next startup is exactly what signals an
            # abnormal previous exit, so this must run here and nowhere
            # else that isn't a genuine, confirmed shutdown.
            capture_settings.forget_last_project()
            event.accept()
            # Explicit, since quitOnLastWindowClose is now disabled
            # (see pnglyph.py) -- closing the main window must still
            # end the whole application; closing the capture window/
            # overlay on their own must NOT.
            QApplication.instance().quit()
        else:
            event.ignore()

    def _save_project(self):
        if not self.project:
            return
        if self._saved_project_path is None:
            # First save for this project (New Project or Import
            # folder -- neither has an established filename yet) --
            # only a NAME is asked for, never a full path, since the
            # .json is now required to live in source_dir itself (see
            # ProjectNameDialog's own docstring). Pre-filled with the
            # enclosing folder's own name, the closest thing to an
            # intended project name that already exists at this point.
            default_name = os.path.basename(self.project.source_dir.rstrip("/\\")) or "project"
            name = ProjectNameDialog.get_name(self, self.project.source_dir, default_name)
            if name is None:
                return
            path = os.path.join(self.project.source_dir, f"{name}.pnglyph.json")
        else:
            # Every subsequent save silently reuses the SAME path --
            # no dialog, no re-asking for a name each time, matching
            # ordinary "Save" behavior everywhere else.
            path = self._saved_project_path
        self.project.save(path)
        self._saved_snapshot = self.project.to_json()
        self._saved_project_path = path
        self._refresh_project_name_label()
        self._remember_last_project()
        app_settings.remember_last_browsed_dir(path)
        # Just "Saved" now, not the full path -- that's shown
        # persistently in project_name_label instead (see its own
        # setup comment), so repeating it here in a message that
        # vanishes the moment anything else is done would be redundant
        # with something already visible at a glance.
        self._set_status_message("Saved")

    def _load_project(self):
        if not self._confirm_discard_if_dirty():
            return
        default_dir = self.project.source_dir if self.project else app_settings.get_last_browsed_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", default_dir, filter="PNGlyph project (*.pnglyph.json)")
        if not path:
            return
        path = app_settings.normalize_path(path)
        # Remembers the PARENT of the project folder, not the project
        # folder itself -- same reasoning as _create_new_project_via_
        # dialog's and _import_folder's own identical fixes: the
        # opened project IS a project-like directory (the .json file
        # lives directly inside it), so remembering it directly would
        # pre-fill a LATER New Project dialog's "Parent folder" with
        # this one, nesting a new project inside it instead of
        # alongside it. remember_last_browsed_dir's own "remember the
        # containing directory" logic is correct as far as it goes
        # (the containing directory of the .json FILE is indeed this
        # project's own folder) -- it's simply one level too shallow
        # for this specific use, so the extra dirname() is applied
        # explicitly here rather than changing that shared function's
        # own, otherwise-correct behavior for every other caller.
        project_folder = os.path.dirname(path)
        app_settings.remember_last_browsed_dir(os.path.dirname(project_folder) or project_folder)
        self.project = Project.load(path)
        self._project_created_fresh = False   # loaded from disk -- see _create_new_project_via_dialog's own comment
        self._saved_snapshot = self.project.to_json()   # freshly loaded -- in sync with disk by definition; captured BEFORE any missing-glyph removal below, so removing any correctly leaves the project marked dirty
        self._saved_project_path = path
        self._refresh_project_name_label()
        # Reset here, before the missing-glyph check below -- avoids
        # _remove_glyphs_from_project's own was_current logic
        # incorrectly matching a codepoint left over from whatever
        # project happened to be open before this one, which could
        # otherwise trigger its canvas-loading side effects before the
        # rest of this method's own setup (weight_slider, paths cache)
        # has even run.
        self.current_cp = None
        missing = self.project.find_missing_glyphs()
        if missing:
            # Checked once, right after load, so every affected glyph
            # is surfaced at once -- not discovered piecemeal, one
            # confusing crash at a time, the first time each is
            # individually clicked on (see the defensive try/except
            # around tracing itself for that separate, ongoing
            # safety net -- this is the up-front, comprehensive check).
            codepoint_list = "\n".join(f"  U+{cp:04X}" for cp in missing)
            plural = len(missing) != 1
            reply = QMessageBox.question(
                self, "Missing source images",
                f"The following glyph{'s' if plural else ''} "
                f"reference{'' if plural else 's'} a source image that "
                f"couldn't be found:\n\n{codepoint_list}\n\n"
                "This can happen if a PNG was deleted or renamed "
                "outside PNGlyph, separately from this project file.\n\n"
                "Continue without them? Choosing No leaves them in the "
                "project exactly as they are (shown grayed out in the "
                "list) so the missing files can be restored first, if "
                "that's preferred instead.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                # Not re-saved immediately -- left as an ordinary
                # unsaved change, same as any other edit, so the user
                # decides when (or whether) to actually persist this
                # removal, rather than it happening silently as a side
                # effect of just opening the file.
                self._remove_glyphs_from_project(missing)
        self._remember_last_project()
        self.weight_slider.setValue(round(self.project.font.weight_ratio * 100))
        self._paths_cache.clear()   # see _new_blank_project for why this matters
        self.current_cp = None   # see _import_folder for why this must happen BEFORE the list rebuild
        self._onion_skin_override_cp = None
        self._refresh_glyph_list(show_progress=True)
        self._refresh_metrics_label()
        first_cp = self._first_glyph_to_show()
        if first_cp is not None:
            self._load_glyph_into_canvas(first_cp)
            self._select_glyph_in_list(first_cp)
        else:
            self._show_metrics_view()
        self._set_status_message(f"Loaded {path}")

    # -- glyph browser -----------------------------------------------------

    def _refresh_glyph_list(self, show_progress: bool = False):
        self.glyph_list.clear()
        if not self.project:
            return
        self.glyph_list.setIconSize(self._icon_size())
        cps = sorted(self.project.glyphs)
        progress = None
        if show_progress and len(cps) > 20:   # not worth a dialog for a handful of glyphs
            progress = QProgressDialog("Reading glyphs…", None, 0, len(cps), self)
            progress.setWindowModality(Qt.WindowModal)
            progress.setMinimumDuration(300)
            progress.setCancelButton(None)   # tracing isn't safely interruptible mid-glyph
            # Label text set ONCE here, never touched again in the loop
            # below (which only calls setValue) -- measured directly:
            # calling setLabelText every iteration was the dominant
            # cost of this whole progress-update mechanism, more than
            # setValue or processEvents combined (~13% of total load
            # time with a live "U+XXXX (i/n)" label vs. ~5% with the
            # label left untouched and just the bar's own fill
            # animating) -- apparently from Qt's own internal layout
            # remeasurement a text CHANGE triggers, largely independent
            # of the string's own length or content. The percentage
            # bar itself still updates live via setValue below; only
            # the per-glyph text churn is gone.
            #
            # Confirmed as a real, reported crash otherwise: this
            # QProgressDialog's own WindowModal modality only actually
            # blocks input once the dialog has become VISIBLE, which
            # setMinimumDuration deliberately delays -- for a rebuild
            # that finishes quickly, or during whatever time remains
            # before that threshold even for a slower one, the main
            # window stays fully interactive while processEvents()
            # below keeps pumping the event loop. That let a SECOND
            # project-switching action (a different button click,
            # itself reentering this exact method) run to completion
            # in the middle of THIS call's own for loop below --
            # reassigning self.project and rebuilding the list against
            # the NEW project, only for this call's own loop to resume
            # afterward and keep appending items from ITS OWN, now-
            # stale cps (captured from the OLD project, before any of
            # this happened) on top of it. Selecting one of those
            # leftover stale items later crashed with a KeyError, since
            # its codepoint was never in the project current by then.
            # Explicitly disabling the whole window -- not relying on
            # the progress dialog's own, timing-dependent modality --
            # closes that window regardless of how long the rebuild
            # itself takes.
            self.setEnabled(False)
        try:
            for i, cp in enumerate(cps):
                item = QListWidgetItem()
                item.setData(Qt.UserRole, cp)
                item.setToolTip(f"U+{cp:04X}")
                self.glyph_list.addItem(item)
                self._refresh_glyph_icon(cp)
                if progress is not None:
                    progress.setValue(i + 1)
                    QApplication.processEvents()
        finally:
            if progress is not None:
                self.setEnabled(True)
                progress.close()
        self.coverage_label.setText(f"{len(self.project.glyphs)} glyphs")
        self._fix_left_pane_width()

    def _fix_left_pane_width(self):
        """Fixed to fit exactly 3 columns of glyph icons, no more --
        same reasoning as the metrics sidebar's own fixed width (see
        its own comment): there's no benefit to a wider glyph browser
        once it comfortably shows a few columns, so drag-widening it
        further just steals room the canvas could otherwise use.

        Runs even with no project loaded yet -- confirmed as a real,
        reported gap otherwise: with no real glyphs to measure, the
        pane was left completely unconstrained (freely resizable)
        until a project's glyphs first populated the list, rather than
        already being fixed-width from the moment the window opens.
        Two temporary items, styled identically to real glyph entries,
        stand in for real ones when the list is empty, purely to
        measure the actual rendered cell pitch -- then get removed
        immediately after.

        Cell pitch (the gap between two adjacent items' own left
        edges, not a single item's own width) is measured from real,
        rendered items rather than computed from icon size alone --
        Qt's own item-delegate padding around each icon isn't a
        documented, predictable constant. The scrollbar's contribution
        is measured the same way, as the difference between the
        panel's own outer width and the list's current viewport width,
        rather than added as a separately-guessed constant -- but only
        after forcing the scrollbar's policy to Always On first:
        confirmed as a real, reported bug otherwise (3 columns fit at
        measurement time, but only 2 once the fixed width was applied)
        -- if the scrollbar isn't showing yet at measurement time (e.g.
        because the list's current, still-unconstrained width fits
        enough columns per row to need fewer rows than would require
        scrolling), the measured chrome width silently omits it, and
        the fixed width computed from that comes up short once the
        narrower, final layout actually needs to scroll.

        A small buffer beyond the exact product of cell_width * 3 is
        added on top of that -- confirmed necessary, not just
        defensive: an exactly-3-columns-wide viewport reliably fit
        only 2, meaning Qt's own column-fitting needs genuinely MORE
        room than the exact product, not merely at least that much."""
        if not self.isVisible():
            # Confirmed as a real, reported bug otherwise: this is
            # deferred via QTimer.singleShot(0, ...) from __init__ so
            # it runs once real widget geometry exists to measure --
            # but a queued singleShot(0, ...) fires at the NEXT
            # available event-loop processing opportunity, whichever
            # comes first, not necessarily the main application loop
            # after this window is actually shown. _check_capture_
            # recovery's own blocking QMessageBox.exec() (which can run
            # later in this SAME __init__, before .show() is ever
            # called) runs its own nested event loop, which processes
            # already-queued callbacks just as readily -- firing this
            # one against an unshown, unlaid-out window. Every
            # measurement below (visualItemRect, viewport().width(),
            # left_panel.width()) comes back zero or bogus in that
            # state, permanently corrupting _left_pane_width to a tiny
            # value -- which then also poisons the collapsed-pane
            # restore tab, since THAT restores to this same stored
            # value. Re-deferring instead of measuring anything yet
            # makes this self-correcting regardless of which event loop
            # happens to process it first.
            QTimer.singleShot(0, self._fix_left_pane_width)
            return
        temp_items_added = 0
        if self.glyph_list.count() < 2:
            # A blank QPixmap, not just QListWidgetItem() left with no
            # icon at all -- confirmed as a real, reported bug
            # otherwise: an item with no actual icon pixmap set
            # doesn't reserve the full grid cell space that setIconSize
            # implies, regardless of that call, and shrinks to fit its
            # own (empty) content instead -- producing a measurement
            # far too narrow to be useful.
            blank = QPixmap(self._icon_size())
            blank.fill(Qt.transparent)
            for _ in range(2 - self.glyph_list.count()):
                item = QListWidgetItem()
                item.setIcon(QIcon(blank))
                self.glyph_list.addItem(item)
                temp_items_added += 1
            self.glyph_list.setIconSize(self._icon_size())

        sb_policy = self.glyph_list.verticalScrollBarPolicy()
        self.glyph_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        QApplication.processEvents()

        r0 = self.glyph_list.visualItemRect(self.glyph_list.item(0))
        r1 = self.glyph_list.visualItemRect(self.glyph_list.item(1))
        cell_width = r1.x() - r0.x()
        chrome_width = self.left_panel.width() - self.glyph_list.viewport().width()
        left_width = cell_width * 3 + chrome_width + 4   # +4: see docstring

        self.glyph_list.setVerticalScrollBarPolicy(sb_policy)
        if temp_items_added:
            for _ in range(temp_items_added):
                self.glyph_list.takeItem(self.glyph_list.count() - 1)

        # Tracked for reactive clamping, not applied as a hard
        # setFixedWidth constraint -- see _right_pane_width's own
        # comment (same reasoning, same confirmed bug: a splitter
        # forced to collapse a genuinely fixed-width child doesn't
        # cleanly resize it, and gets worse once the child has its own
        # internal layout with a scrollbar, exactly what this pane is).
        # If the splitter's current width for this pane already
        # exceeds the newly measured ideal, correct it immediately
        # rather than waiting for the next drag to trigger
        # _clamp_pane_widths.
        self._left_pane_width = left_width
        sizes = self.splitter.sizes()
        if len(sizes) == 3 and sizes[0] > left_width:
            sizes[1] += sizes[0] - left_width
            sizes[0] = left_width
            self.splitter.setSizes(sizes)
        if len(sizes) == 3:
            self._last_left_size = sizes[0]
            self._last_right_size = sizes[2]
        # Window's own minimum width recomputed here too, not just at
        # startup -- left_panel's fixed width isn't actually known
        # until a project's glyphs exist to measure (see this method's
        # own docstring), so __init__'s original flat 900px guess
        # can't account for it. Center floor is a fixed, separate
        # estimate for Weight/Size staying at genuinely usable slider
        # widths, not derived from either fixed pane. Was temporarily
        # raised to 970 while Size's legend needed room for five tick
        # labels -- reverted back to 420 once the legend was trimmed to
        # three (-100/0/100, see the legend's own setup comment), which
        # removed that requirement rather than needing more width to
        # satisfy it -- but 420 alone still let the Size box collapse
        # to where its own sliders effectively disappeared, confirmed
        # as a real, reported problem. Raised by exactly one Weight
        # box's own share of that 420 total: Weight and Size split the
        # bottom bar evenly (1:1 stretch, see bottom_bar_layout's own
        # setup), so each currently gets roughly half of center_floor
        # -- adding that same half again (420 -> 630) hands Size a
        # second full Weight-box's worth of room without guessing at
        # an arbitrary new number. This is a structural estimate, not
        # a pixel-measured one -- this project's own offscreen test
        # environment has already been confirmed unreliable for exact
        # widget-width measurements more than once, so this number is
        # worth checking against a real screen rather than trusted
        # outright.
        center_floor = 630
        self.setMinimumWidth(left_width + self._right_pane_width + center_floor + self._TOOLBAR_MARGIN)

    def _icon_size(self):
        return QSize(40, 40)

    def _first_glyph_to_show(self) -> int | None:
        """The most useful glyph to land on right after a project loads
        -- the lowest non-space codepoint, since space (0x20) is both
        the numerically-lowest ASCII codepoint in virtually every font
        project and always empty, making it a poor first impression to
        auto-select ("did the import actually work?"). Falls back to
        space itself only if it's genuinely the sole glyph present."""
        if not self.project or not self.project.glyphs:
            return None
        non_space = [cp for cp in self.project.glyphs if cp != 0x20]
        return min(non_space) if non_space else min(self.project.glyphs)

    def _find_item(self, cp: int) -> QListWidgetItem | None:
        for i in range(self.glyph_list.count()):
            item = self.glyph_list.item(i)
            if item.data(Qt.UserRole) == cp:
                return item
        return None

    def _select_glyph_in_list(self, cp: int) -> None:
        """Highlights cp's item in the glyph list WITHOUT also
        reloading the canvas a second time -- confirmed as a real bug
        otherwise, present at every one of this method's six call
        sites (not just newly-introduced ones): setCurrentItem() fires
        currentItemChanged, which is wired to _on_glyph_selected, which
        itself calls _load_glyph_into_canvas -- so a caller that
        already loaded the canvas directly, then called setCurrentItem
        to fix the list highlight, was silently triggering a SECOND,
        redundant full reload every time, discovered only once a test
        actually counted calls rather than just checking the end
        state. Blocking signals for the SINGLE setCurrentItem call
        avoids this without needing every caller to know why."""
        item = self._find_item(cp)
        if item is None:
            return
        self.glyph_list.blockSignals(True)
        self.glyph_list.setCurrentItem(item)
        self.glyph_list.blockSignals(False)

    def _refresh_glyph_icon(self, cp: int, paths=None):
        """Render the glyph's actual traced (and edited) outline for its
        list icon, rather than relying on however the system UI font
        happens to draw that character -- the two can differ noticeably
        for symbols, box-drawing characters, or anything hand-edited.

        Pass paths explicitly (or rely on the cp==current_cp fast path,
        or the paths cache below) whenever possible: re-tracing from the
        source PNG is a real cost (skeletonize + graph build + simplify,
        ~20ms+ per call, worse on slower machines) and weight/cap-style
        changes don't touch the underlying skeleton at all -- only
        re-render the SAME paths at a different thickness. Calling this
        on every tick of a dragged slider without a fast path was
        blocking the UI thread for a visible, sometimes multi-second
        stretch.

        Every call here also updates self._paths_cache[cp] -- a full
        refresh of every glyph's icon (see _refresh_all_glyph_icons)
        would otherwise mean re-tracing all ~185 source files from disk
        on every weight-slider release, which is exactly the kind of
        cost that caused the freezes this was built to avoid. Since
        _refresh_glyph_list() already traces every glyph once at import
        time, that same pass populates the cache as a side effect, so a
        later bulk refresh only needs a cheap re-thicken per glyph."""
        if not self.project:
            return
        item = self._find_item(cp)
        if item is None:
            return
        recipe = self.project.glyphs[cp]
        m = self.project.metrics
        try:
            if paths is not None:
                pass
            elif cp == self.current_cp and self.canvas.paths:
                paths = self.canvas.paths
            elif cp in self._paths_cache:
                paths = self._paths_cache[cp]
            elif not recipe.source_file:
                # drawn-from-scratch, nothing drawn yet -- no source
                # PNG to trace, so start from an empty path list rather
                # than attempting to trace and hitting the broad
                # except below (which would show a generic gray
                # "something went wrong" icon for what's actually just
                # a normal, empty, not-yet-drawn glyph)
                paths = _apply_import_scale_and_edits([], recipe)
            else:
                src_path = self.project.resolve_source_path(recipe)
                paths, _shape = tracing.trace_glyph(src_path)
                paths = _apply_import_scale_and_edits(paths, recipe)
            self._paths_cache[cp] = paths
            half_width = (self.project.font.weight_ratio * (m.baseline_px - m.cap_top_px) / 2
                          if m.cap_top_px is not None and m.baseline_px is not None else 10.0)
            box = None
            if m.advance_px and m.cap_top_px is not None and m.descender_bottom_px is not None:
                box = (0, m.cap_top_px, m.advance_px, m.descender_bottom_px)
            bg = QColor(*style.EDITED_MARKER_COLOR) if recipe.edits else None
            pixmap = render_glyph_pixmap(paths, half_width, self.project.font.cap_style,
                                          size=40, box=box, bg=bg)
        except Exception:
            pixmap = QPixmap(40, 40)
            pixmap.fill(QColor("#eeeeee"))
        if recipe.excluded:
            # Desaturated and dimmed, not just a plain gray placeholder
            # -- the glyph's own traced shape should still be
            # recognizable at a glance (which glyph this is), just
            # visibly "dimmed out" to make excluded-from-export status
            # obvious without opening it.
            img = pixmap.toImage().convertToFormat(QImage.Format_Grayscale8)
            faded = QPixmap(img.size())
            faded.fill(Qt.transparent)
            painter = QPainter(faded)
            painter.setOpacity(0.35)
            painter.drawImage(0, 0, img)
            painter.end()
            pixmap = faded
        item.setIcon(pixmap)
        # recipe.groups counts too, not just recipe.edits -- confirmed
        # as a real gap: grouping alone (no geometric change at all,
        # since forming a group only tags existing paths together)
        # left a glyph showing as "unedited" even though its stored
        # state had genuinely changed from the auto-traced original.
        is_modified = bool(recipe.edits or recipe.groups)
        font = QFont(self.font().family(), 9, QFont.Bold if is_modified else QFont.Normal)
        item.setFont(font)
        # Set on BOTH branches, not just when edits exist -- confirmed
        # as a real, reported bug otherwise: the icon pixmap and font
        # weight both correctly reverted after an undo that emptied
        # recipe.edits back out, but the tooltip stayed stuck reading
        # "— edited" forever, since nothing ever reset it back to the
        # plain form.
        item.setToolTip(f"U+{cp:04X} \u2014 edited" if is_modified else f"U+{cp:04X}")
        if recipe.excluded:
            # Overrides the tooltip set just above unconditionally --
            # excluded status is the more important thing to surface at
            # a glance than whether it also happens to be edited.
            item.setToolTip(f"U+{cp:04X} \u2014 excluded from export")
        # Explicit foreground either way, not left at whatever the
        # stylesheet's own default happens to be for the non-excluded
        # case -- QListWidgetItem doesn't reliably fall back to a
        # stylesheet color once setForeground has been called on it at
        # all, even from a PREVIOUS refresh (e.g. toggling a glyph
        # excluded, then back to included again), so both states are
        # set explicitly here rather than only setting one and hoping
        # the other reverts on its own.
        item.setForeground(QColor("#a0a0a0") if recipe.excluded else QColor("#222222"))

    def _filter_glyph_list(self, text: str):
        for i in range(self.glyph_list.count()):
            item = self.glyph_list.item(i)
            cp = item.data(Qt.UserRole)
            label = chr(cp) if cp >= 0x20 else f"U+{cp:04X}"
            item.setHidden(bool(text) and text.lower() not in label.lower()
                            and text.lower() not in f"u+{cp:04x}")

    def _on_glyph_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None = None):
        # currentItemChanged, not itemClicked -- confirmed as a real,
        # reported gap otherwise: itemClicked only ever fires for an
        # actual mouse click, so keyboard navigation (arrow keys, Page
        # Up/Down) changed which item LOOKED selected in the list
        # without ever loading it into the canvas, making keyboard
        # navigation of the glyph list effectively useless. This signal
        # fires for both, plus programmatic selection changes (see
        # setCurrentItem's own call sites) -- one handler now covers
        # every way the current item can change, not just a click.
        #
        # current can be None here (e.g. while the list is being
        # cleared/rebuilt in _refresh_glyph_list) -- itemClicked could
        # never fire with no item, so this guard is new.
        if current is None:
            return
        cp = current.data(Qt.UserRole)
        self._load_glyph_into_canvas(cp)

    def _build_guides_dict(self, m) -> dict[str, float]:
        guides = {}
        if m.cap_top_px is not None:
            guides["cap"] = m.cap_top_px
        if m.xheight_top_px is not None:
            guides["x-height"] = m.xheight_top_px
        if m.baseline_px is not None:
            guides["baseline"] = m.baseline_px
        if m.descender_bottom_px is not None:
            guides["descender"] = m.descender_bottom_px
        if m.advance_px is not None:
            guides["advance"] = m.advance_px
        return guides

    def _load_glyph_into_canvas(self, cp: int, preserve_view: bool = False, restore_groups: bool = True,
                                 precomputed_paths: list | None = None):
        """precomputed_paths: when the caller already has the exact,
        correct paths for this glyph in hand (Break and Join both do --
        each computes its own result directly via incremental
        geometry.break_path/join_paths calls against what's already on
        screen, with no coordinate computation involved at all, just a
        repartition of points that already exist), this skips the
        retrace-from-source-plus-replay-the-whole-edit-history below
        entirely and uses it as-is. Every OTHER caller (switching
        glyphs, Undo/Redo, capture commits, Flatten...) has
        no such in-hand result and genuinely needs the full retrace --
        it's the only way those can work at all. Break/Join used to go
        through that same full retrace purely out of convenience
        (reusing this one already-correct function rather than writing
        a second rebuild path), which was harmless right up until it
        wasn't: replaying the ENTIRE edit history on every Break/Join
        meant either of them could silently resurface a corrupted
        EARLIER edit that had nothing to do with the operation just
        performed (confirmed as a real, reported bug -- see
        _on_point_committed's own comment on the actual corruption this
        exposed, which this parameter doesn't fix on its own, only
        avoids needlessly re-triggering here)."""
        if not self.project:
            return
        if cp not in self.project.glyphs:
            # Defensive guard, not a fix in itself -- the actual, known
            # cause (a reentrant project switch mid-rebuild leaving
            # stale, now-invalid items selectable in the glyph list)
            # is fixed at _refresh_glyph_list's own source. Kept here
            # too, cheaply, as a safety net against any OTHER,
            # unforeseen way a stale codepoint could reach this method
            # -- a KeyError crash here is never acceptable regardless
            # of how it happened.
            return
        self.current_cp = cp
        # Recomputed here, not just from _push_undo/_undo/_redo
        # themselves -- the CURRENT glyph changing (a plain glyph-list
        # switch, with neither stack touched at all) can just as easily
        # flip whether there's history for IT specifically. This is the
        # one shared point every path through this method already
        # passes, so it's the natural place to keep this correct
        # regardless of why the current glyph changed.
        self._refresh_undo_redo_enabled_state()
        # Same reasoning -- the current glyph's own excluded state
        # (locking/unlocking editing) needs to be re-checked every time
        # the glyph itself changes, not just when that state is
        # explicitly toggled (see _set_glyphs_excluded's own call to
        # this same method for that other case).
        self._refresh_editing_locked_state()
        recipe = self.project.glyphs[cp]
        trace_error_message: str | None = None
        if precomputed_paths is not None:
            paths = precomputed_paths
        else:
            if recipe.source_file:
                src_path = self.project.resolve_source_path(recipe)
                try:
                    paths, shape = tracing.trace_glyph(src_path)
                except Exception as e:
                    # A source PNG referenced by this glyph's own
                    # recipe couldn't be traced -- most likely deleted
                    # or renamed outside PNGlyph after this project was
                    # last loaded (find_missing_glyphs already catches
                    # this proactively at load time; this is the
                    # defensive fallback for a file that goes missing
                    # or becomes unreadable mid-session instead, after
                    # that one-time check already ran). Falls back to
                    # an empty base rather than letting this propagate
                    # uncaught and crash the app the moment this glyph
                    # is simply clicked on to view it -- any of its own
                    # edits still get applied below, on top of that
                    # empty base, rather than losing them from view too.
                    # Message set later, not here -- see this method's
                    # own unconditional status_message_label clear
                    # further down, which would otherwise immediately
                    # wipe out anything set at this point in the call.
                    paths = []
                    trace_error_message = (
                        f"U+{cp:04X}: couldn't read its source image "
                        f"({os.path.basename(src_path)}) -- {e}")
            else:
                # drawn from scratch -- no source PNG to trace, start empty
                paths = []
            paths = _apply_import_scale_and_edits(paths, recipe)

        m = self.project.metrics
        half_width = self.project.font.weight_ratio * ((m.baseline_px - m.cap_top_px) / 2) \
            if m.cap_top_px is not None and m.baseline_px is not None else 10.0

        # Sized to the slider's MAXIMUM reachable weight, not the current
        # one -- see load_glyph()'s docstring on why: a margin that only
        # matches the current weight clips ink at the canvas edge the
        # moment a heavier weight is picked, and resizes visibly (a
        # jarring "canvas jump") every time either the weight or the
        # selected glyph changes.
        max_half_width = self.weight_slider.maximum() / 100 * ((m.baseline_px - m.cap_top_px) / 2) \
            if m.cap_top_px is not None and m.baseline_px is not None else 40.0

        box = None
        if m.advance_px and m.cap_top_px is not None and m.descender_bottom_px is not None:
            box = (0, m.cap_top_px, m.advance_px, m.descender_bottom_px)

        guides = self._build_guides_dict(m)

        self.canvas.load_glyph(paths, half_width, self.project.font.cap_style, box, guides,
                                max_half_width=max_half_width,
                                source_bounds=tuple(m.canvas_size) if m.canvas_size else None,
                                preserve_view=preserve_view,
                                groups=recipe.groups)
        self._refresh_clearance_label(paths, half_width)
        if box:
            # Resize the whole grid to match the content's aspect ratio
            # first -- see _show_metrics_view for the full reasoning.
            self.center_panel.reflow()
            # Same reasoning as _show_metrics_view: use the exact
            # mapToScene(viewport()) computation rather than a
            # box+margin approximation, since fitInView's letterboxing
            # can make that approximation diverge substantially from
            # what's actually visible.
            self._on_view_changed()
        else:
            self.ruler.set_values(None, None, None, None, average_advance=self._average_advance())
            self.vertical_ruler.set_values(None, None)
            self.guide_strip.set_view(None, None)
            self.advance_strip.set_view(None, None)
        # Set (not showMessage) -- this is glyph_info_label now, not
        # the temporary-message area. status_message_label cleared
        # here too, not on any timer -- confirmed as the wanted
        # behavior: switching to a different glyph should drop any
        # stale extra message (e.g. a capture-commit summary) that no
        # longer relates to what's now on screen. Commit flows that
        # call this method and then set their OWN message right
        # afterward (see _commit_new_glyph and the multi-capture commit
        # method) simply overwrite this clear a moment later -- no
        # special-casing needed here for that.
        self.glyph_info_label.setText(f"U+{cp:04X} {chr(cp) if cp>=0x20 else ''}  ·  {len(paths)} contours")
        self.status_message_label.setText("")
        if trace_error_message:
            # Set AFTER the unconditional clear just above, not instead
            # of it -- matches this method's own established pattern
            # (see its comment just above) of clearing first, with
            # whatever should actually survive set right after, in the
            # same spirit as a commit flow's own message overwriting
            # this same clear a moment later. Setting it any EARLIER
            # (e.g. inside the except block itself, back where the
            # trace actually failed) would have this same clear wipe it
            # out a few lines later in this same call, before it was
            # ever visible at all.
            self._set_status_message(trace_error_message)
        # Weight (and cap style) are font-wide settings, not per-glyph --
        # the slider correctly stays wherever it was left, applying to
        # every glyph uniformly. But the list only re-renders an icon
        # when THAT glyph is the one being actively edited, so a glyph
        # you hadn't touched since the last weight change would still
        # show its OLD icon the moment you clicked it, even though the
        # canvas (computed fresh right here) already reflects the
        # current weight correctly. Refreshing here -- reusing the
        # paths just traced above, no extra re-trace cost -- keeps
        # whichever glyph is selected always showing an icon that
        # matches what's actually on the canvas.
        self._refresh_glyph_icon(cp, paths=paths)
        # Unconditional, not just from _on_path_drawn/_on_delete_requested
        # -- plain glyph-list selection reaches this method directly
        # too, and without this, switching glyphs while Draw mode is
        # active would leave a stale onion-skin overlay from whichever
        # glyph was previously selected.
        if self.draw_action.isChecked():
            self.canvas.set_draw_mode(True)
        self._refresh_onion_skin()
        # Restore any persistent groups (see GlyphRecipe.groups) into
        # the SHIFT layer (see GlyphCanvas._shift_selected) -- a group
        # is a whole-unit selection, the same kind SHIFT-click itself
        # produces, so this is what makes a group PERSISTENT rather
        # than just surviving the one reload right after it was
        # created: this method runs on every load (switching glyphs
        # and back, reopening the project, undo/redo), so the same
        # set stays selected together every time, not just for as
        # long as the current selection happens to survive. Restoring
        # into the shift layer specifically (not a raw Qt selection)
        # also means a later CTRL-click can correctly carve one member
        # back out on its own, Explorer-style, without disturbing the
        # rest -- confirmed as the fix for a real, reported bug where
        # the OLD single-flag selection model made that same CTRL-
        # click instead toggle everything BUT the clicked item.
        # Indices that are no longer in range (e.g. a LATER, unrelated
        # edit changed the path count) are silently dropped rather
        # than applied wrong or raising -- a group created by Join is
        # only ever meant to describe the state at the moment it was
        # made.
        #
        # restore_groups=False skips actually SELECTING what's
        # restored (the data cleanup below still always runs) --
        # confirmed as a real, reported issue otherwise: Join/Break's
        # own internal reload (still this same method) would sweep in
        # and highlight EVERY OTHER, completely unrelated persisted
        # group along with whatever the user actually just did,
        # because this restoration has no way to know "just this one"
        # was the intent versus "show me everything that's saved" (the
        # correct behavior for switching glyphs, undo/redo, or
        # reopening the project). _on_join_clicked/_on_break_clicked
        # pass False here and set the canvas selection to reflect
        # their own specific outcome themselves, afterward.
        stale = False
        restored: set[int] = set()
        for group in recipe.groups:
            valid = [i for i in group if 0 <= i < len(paths)]
            if len(valid) != len(group):
                stale = True
            restored |= set(valid)
        if restored and restore_groups:
            # Group members are always single-segment (atomic) paths
            # by the time they're grouped -- joining a group
            # decomposes any polyline into individual segments first
            # (see _on_join_clicked) -- so seg_index 0 always applies.
            self.canvas._shift_selected |= {(pi, 0) for pi in restored}
            self.canvas._last_action_was_shift = True
            self.canvas._last_action_was_plain_click = False
            self.canvas._refresh_selection_visuals()
        if stale:
            recipe.groups = [[i for i in g if 0 <= i < len(paths)] for g in recipe.groups]
            recipe.groups = [g for g in recipe.groups if len(g) >= 2]
        if recipe.groups:
            self._on_selection_changed()

    def _average_advance(self) -> float | None:
        if not self.project or not self.project.metrics.advance_px:
            return None
        return self.project.metrics.advance_px

    def _refresh_clearance_label(self, paths, half_width):
        if len(paths) < 2:
            self.clearance_label.setText("Single contour \u2014\nnothing to check.")
            return
        d = geometry.clearance(paths, half_width=half_width)
        if d == float("inf"):
            # every pair of paths was within the junction tolerance --
            # i.e. everything that's close is a legitimate connection,
            # not a stray mark -- so there's nothing to flag, but "inf"
            # is meaningless to show as a distance.
            self.clearance_label.setText("OK \u2014 no separate strokes to compare.")
            return
        threshold = 2.3 * half_width
        # Fixed-width numeric fields (with the monospace font set on
        # this label above) keep the decimal point landing in the same
        # column, and stop the rest of the sentence from visibly
        # reflowing sideways as a value's digit count changes.
        #
        # Every message here is explicitly split across both of the
        # box's two fixed lines, not just left to wrap naturally --
        # word-wrap alone left short messages sitting on a single line
        # with an empty, unbalanced-looking second line beneath them,
        # now that the box is a fixed two-line height regardless of
        # message length.
        if d < threshold:
            self.clearance_label.setText(
                f"\u26a0 tight clearance: {d:6.1f}px\n(need \u2265{threshold:6.1f}px at this weight)")
        else:
            self.clearance_label.setText(f"OK \u2014 min clearance: {d:6.1f}px")

    # -- property panel callbacks -------------------------------------------

    def _flatten_bounds(self) -> tuple[float, float, float, float] | None:
        """The same working-area bounds GlyphCanvas.clamp_to_source_bounds
        uses for ordinary point-dragging, computed here once for
        Stretch/Flatten's own use -- see geometry.flatten's own bounds
        parameter for why this needs to be the SAME bounds both at
        preview time and when stored in the committed EditStep's args."""
        if self.canvas.box_rect is not None:
            return self.canvas.box_rect
        if self.canvas.source_bounds is not None:
            w, h = self.canvas.source_bounds
            return (0.0, 0.0, w, h)
        return None

    def _on_flatten_x_changed(self, value: int):
        self.flatten_x_value_label.setText(f"{value}")
        if self.flatten_link_check.isChecked() and self.flatten_y_slider.value() != value:
            self.flatten_y_slider.setValue(value)   # its own handler updates its own label/preview
            return
        self._update_flatten_preview()

    def _on_flatten_y_changed(self, value: int):
        self.flatten_y_value_label.setText(f"{value}")
        if self.flatten_link_check.isChecked() and self.flatten_x_slider.value() != value:
            self.flatten_x_slider.setValue(value)
            return
        self._update_flatten_preview()

    def _update_flatten_preview(self):
        # value is -100..100 per axis, centered on 0 = the original,
        # untouched shape on that axis (see the sliders' own setup
        # comment). Scale is a single, continuous 0..2 mapping across
        # the whole range per axis: -100 -> 0 (fully flat), 0 -> 1
        # (unchanged), +100 -> 2 (fully stretched) -- geometry.flatten
        # itself is untouched, it already accepted any scale, including
        # above 1, before these sliders had any way to reach one.
        if not self._flatten_snapshot or not self.project or self.current_cp is None:
            return
        indices, snapshot_paths = self._flatten_snapshot
        x_value = self.flatten_x_slider.value()
        y_value = self.flatten_y_slider.value()
        new_paths = geometry.flatten_xy(snapshot_paths, indices,
                                         x_scale=1 + x_value / 100, y_scale=1 + y_value / 100,
                                         # Fixed anchors, not computed per-glyph from the
                                         # selection's own X/Y extent -- tried first and
                                         # confirmed wrong: a tilde is wide and short (it
                                         # varies MORE in X than Y), so an extent-based
                                         # heuristic picked "compress X" for exactly the
                                         # case that needs Y compressed instead. Worse, that
                                         # made the choice state-dependent in a way that
                                         # looked like a bug on its own -- flattening changes
                                         # the shape's own extent, so the SAME heuristic could
                                         # flip its answer on the very next attempt at the
                                         # same glyph.
                                         x_anchor="min_x", y_anchor="min_y",
                                         bounds=self._flatten_bounds())
        self._preview_flatten(new_paths, indices)

    def _preview_flatten(self, new_paths: list, indices: list[int]):
        """Live-updates the canvas to a candidate flatten result without
        committing it. Uses the canvas's in-place preview path, not a
        full load_glyph() rebuild -- see apply_paths_preview's own
        docstring for why that matters here specifically."""
        self.canvas.apply_paths_preview(indices, new_paths)

    def _commit_flatten(self):
        """Bakes the current X/Y slider values into the recipe as ONE
        combined EditStep (op="flatten_xy") -- WITHOUT touching the
        canvas, selection, or snapshot. Earlier versions reloaded the
        canvas and re-selected here, which re-fired selectionChanged
        and reset the snapshot to the just-committed shape; that meant
        every commit silently re-baselined "0" to mean "whatever was
        last committed" instead of "the original selection", so the
        percentage appeared to snap back right after every adjustment
        and there was no way to slide back to the true original short
        of Undo. The snapshot now stays fixed at the ORIGINAL shape for
        the whole selection session -- 0/0 (center on both axes -- see
        the sliders' own setup comment) always means that original, for
        as long as the same paths stay selected -- and repeated commits
        within one session update the SAME EditStep in place rather
        than stacking a new one, so the whole session collapses to one
        undo step, not one per click."""
        if not self._flatten_snapshot or not self.project or self.current_cp is None:
            return
        x_value = self.flatten_x_slider.value()
        y_value = self.flatten_y_slider.value()
        indices, snapshot_paths = self._flatten_snapshot
        recipe = self.project.glyphs[self.current_cp]

        updating_own_step = (self._flatten_session_edit is not None
                              and recipe.edits and recipe.edits[-1] is self._flatten_session_edit)

        if x_value == 0 and y_value == 0:
            if updating_own_step:
                recipe.edits.pop()
                self._flatten_session_edit = None
                # The canvas already shows the restored-to-original shape
                # (the sliders reaching 0 already re-previewed it) -- but
                # the list icon was never told anything changed, so
                # without this it would keep showing the flattened/
                # stretched version even though the recipe no longer has
                # that step.
                self._refresh_glyph_icon(self.current_cp)
            return

        args = {"indices": indices, "x_scale": 1 + x_value / 100, "y_scale": 1 + y_value / 100,
                "x_anchor": "min_x", "y_anchor": "min_y", "bounds": self._flatten_bounds()}
        if updating_own_step:
            recipe.edits[-1].args = args
        else:
            self._push_undo(self.current_cp, recipe.edits)
            step = EditStep(op="flatten_xy", args=args)
            recipe.edits.append(step)
            self._flatten_session_edit = step
        # Same reasoning as the 0/0 case above: the canvas is already
        # correct (apply_paths_preview kept it in sync live), but the
        # list icon needs telling explicitly. Cheap -- this hits
        # _refresh_glyph_icon's cp==current_cp fast path, reusing the
        # canvas's own current paths rather than re-tracing anything.
        self._refresh_glyph_icon(self.current_cp)

    def _set_flatten_link_visual(self, linked: bool):
        """Tints the X and Y letter labels themselves (not the values
        next to them) with the checkbox's own blue when linked, plain
        otherwise -- replaces an earlier border-around-the-whole-panel
        design, dropped as visually odd (a box drawn around a box) and
        because its own margins were the reason this panel sat taller
        than Grid & snap. A plain background-color tint on just the two
        letters needs no extra layout space at all, which is also what
        let the panel's height come back down to match without having
        to inflate Grid & snap's own height instead."""
        style = "background-color: #3a6ff0; color: white; border-radius: 3px; padding: 1px 4px;" \
            if linked else ""
        for lbl in self._flatten_axis_labels:
            lbl.setStyleSheet(style)

    def _on_flatten_link_toggled(self, checked: bool):
        """Toggling the link (either direction) commits whatever's
        currently previewed as its OWN, separate, final undo step, then
        starts a genuinely fresh session from that resulting shape --
        NOT a matter of simply composing the old scale values with a
        new multiplier. Composing would silently narrow the achievable
        range on the very next drag: whichever axis started further
        from 1.0 would hit the 2x ceiling (or the 0 floor) before the
        other one finished, and unlinking has exactly the same risk in
        reverse. A genuinely fresh reference point -- a new snapshot, a
        new EditStep, both sliders back at 0 -- is the only way to keep
        the full -100..100 range available regardless of where the
        sliders happened to be the moment this was toggled. The glyph
        itself doesn't visibly move: at 0/0 against the brand new
        snapshot, the shape is exactly what was already on screen."""
        self._set_flatten_link_visual(checked)
        self._commit_flatten()
        if not self._flatten_snapshot or self.current_cp is None:
            return
        indices, _old_snapshot_paths = self._flatten_snapshot
        self._flatten_snapshot = (indices, copy.deepcopy(self.canvas.paths))
        self._flatten_session_edit = None
        for slider, label in ((self.flatten_x_slider, self.flatten_x_value_label),
                               (self.flatten_y_slider, self.flatten_y_value_label)):
            slider.blockSignals(True)
            slider.setValue(0)
            slider.blockSignals(False)
            label.setText("0")

    def _set_flatten_enabled(self, enabled: bool):
        """Keeps every X/Y control (both sliders, both +/- pairs, both
        value labels, and the link checkbox) in sync as one unit --
        QWidget has no built-in enabledChanged signal to hook into, so
        each control needs setting explicitly whenever the underlying
        selection state changes."""
        controls = (self.flatten_x_slider, self.flatten_x_minus_btn, self.flatten_x_plus_btn,
                    self.flatten_x_value_label, self.flatten_y_slider, self.flatten_y_minus_btn,
                    self.flatten_y_plus_btn, self.flatten_y_value_label)
        tooltip = "" if enabled else "Select a path to enable"
        for w in controls:
            w.setEnabled(enabled)
            w.setToolTip(tooltip)
        self.flatten_link_check.setEnabled(enabled)
        self.flatten_link_check.setToolTip(tooltip or self._flatten_link_tooltip)

    def _on_weight_value_changed(self, value: int):
        """Runs on every value change, including every intermediate tick
        of an active drag. The canvas re-thicken is cheap now (~1ms,
        confirmed by measurement) thanks to the fast-path fix that
        avoids re-tracing from the source file, so it's safe to keep
        this live all the way through the drag -- there's real value in
        seeing the stroke weight change as you move the slider, not
        just where it ends up."""
        ratio = value / 100
        if self.project:
            self.project.font.weight_ratio = ratio
        if self.canvas.paths and self.project and self.project.metrics.cap_top_px is not None:
            cap_px = self.project.metrics.baseline_px - self.project.metrics.cap_top_px
            self.canvas.set_weight(ratio * cap_px / 2)
            self._refresh_clearance_label(self.canvas.paths, ratio * cap_px / 2)
        # Debounced (see the timer's own setup comment) rather than
        # gated on isSliderDown() -- restarting on every tick and only
        # actually firing once things go quiet naturally covers drags,
        # keyboard arrow keys, and held +/- buttons alike, without
        # needing to special-case each trigger source individually.
        self._weight_icon_refresh_timer.start(150)

    def _on_weight_settle(self):
        self._refresh_visible_glyph_icons()
        # The onion-skin overlay (see _refresh_onion_skin) is a
        # DIFFERENT glyph's outline, deliberately rendered at the
        # CURRENT glyph's own weight for a fair proportion comparison
        # -- so a weight change needs to refresh it too, same as it
        # already needs to refresh every glyph's list icon. Debounced
        # here rather than called live from every slider tick like the
        # canvas's own re-thicken: measured at ~15ms per call (it can
        # re-trace a different glyph's source PNG from scratch), not
        # the ~1ms the live canvas update costs, so doing it on every
        # tick during an active drag risked visibly stuttering the
        # slider itself. Confirmed as a real, reported bug otherwise:
        # the onion skin only ever updated on the NEXT glyph switch or
        # undo/redo, silently showing a stale, previous-weight
        # rendering for as long as you stayed on the same glyph.
        self._refresh_onion_skin()

    def _visible_glyph_items(self) -> list[QListWidgetItem]:
        """Every glyph-list row whose icon actually intersects the
        current viewport -- geometry checks only, no re-rendering, so
        even iterating every row this way stays cheap regardless of
        total glyph count; only the actual icon refresh (see
        _refresh_glyph_icon, ~1.4ms/glyph measured) is the part that
        doesn't scale, which is exactly what limiting to this list is
        for."""
        viewport_rect = self.glyph_list.viewport().rect()
        return [
            self.glyph_list.item(i) for i in range(self.glyph_list.count())
            if viewport_rect.intersects(self.glyph_list.visualItemRect(self.glyph_list.item(i)))
        ]

    def _refresh_visible_glyph_icons(self):
        """The weight-settle half of the visible/stale split (see
        _stale_icon_cps' own setup comment) -- confirmed necessary, not
        premature optimization, by direct measurement: refreshing every
        glyph's icon on a project this size already took ~260ms per
        settle, scaling LINEARLY with glyph count (no per-glyph fixed
        cost to amortize away), so a full-coverage set of several
        thousand would turn every settle into a multi-second stall.
        Refreshing only what's actually on screen bounds the cost to
        viewport size instead of total glyph count -- everything else
        keeps showing its previous, still-valid (just weight-stale)
        icon rather than nothing at all, and is caught up the moment it
        actually scrolls into view (see _on_glyph_list_scroll_settle)."""
        if not self.project:
            return
        visible_cps = {item.data(Qt.UserRole) for item in self._visible_glyph_items()}
        for cp in self.project.glyphs:
            if cp in visible_cps:
                self._refresh_glyph_icon(cp)
                self._stale_icon_cps.discard(cp)
            else:
                self._stale_icon_cps.add(cp)

    def _on_glyph_list_scroll_settle(self):
        """Catches up any icon left stale by a weight change once it
        actually scrolls into view -- debounced against scroll
        SETTLING (see the timer's own connection comment), not fired on
        every intermediate scroll pixel, for the same reason the weight
        slider itself is debounced: refreshing is real, per-glyph work,
        not free geometry."""
        if not self.project or not self._stale_icon_cps:
            return
        for item in self._visible_glyph_items():
            cp = item.data(Qt.UserRole)
            if cp in self._stale_icon_cps:
                self._refresh_glyph_icon(cp)
                self._stale_icon_cps.discard(cp)

    def _refresh_all_glyph_icons(self):
        if not self.project:
            return
        for cp in self.project.glyphs:
            self._refresh_glyph_icon(cp)
        self._stale_icon_cps.clear()

    def _on_cap_style_changed(self, style: str):
        self.round_btn.setChecked(style == "round")
        self.flat_btn.setChecked(style == "flat")
        if self.project:
            self.project.font.cap_style = style
        self.canvas.set_cap_style(style)
        # Same gap as the weight slider had (see _on_weight_settle's
        # own comment): a round vs. flat cap can visibly change how
        # large a glyph reads at a glance (flat caps read smaller,
        # since round caps bulge past the stroke's own endpoints), so
        # every list icon and the onion-skin overlay both need to
        # reflect the new style, not just the canvas. Called directly
        # rather than through the weight slider's debounce timer -- a
        # style button is a single, discrete click, not a value that
        # can fire many times a second like a dragged slider, so
        # there's no stutter risk to guard against here. Still uses
        # the same visible-only + stale-marking split as the weight
        # path (see _refresh_visible_glyph_icons), rather than a full,
        # every-glyph refresh, for the same reason: even a single
        # synchronous pass over a very large glyph set would freeze
        # the UI for the length of that one click.
        self._refresh_visible_glyph_icons()
        self._refresh_onion_skin()

    def _on_offset_preview(self, dx: float, dy: float):
        # Reuses the same status-bar label the cursor-position readout
        # uses -- the raw cursor coordinate isn't the useful number to
        # show mid-drag anyway; how far the group has moved from where
        # it started is. Reverts to normal cursor coordinates on the
        # next plain mouse move once the drag ends.
        self.coord_label.setText(f"Offset: {dx:+6.1f}, {dy:+6.1f}")

    def _refresh_flatten_snapshot_if_stale(self, affected_indices):
        """Called after ANY external edit commits (an ordinary point
        drag, a whole-segment/group drag) that might touch the paths a
        Size session is currently tracking -- selectionChanged alone
        doesn't cover this: Qt only fires it when the SET of selected
        items changes, never when an already-selected item's own
        geometry moves via a drag, confirmed directly. Without this,
        the snapshot stayed silently stale at whatever the shape looked
        like when the session began, so touching a Size slider after
        dragging the very thing it was tracking would recompute from
        that stale reference and overwrite the drag -- exactly the
        reported "moving an object then touching a Size slider makes it
        jump back" bug. Same "fresh reference point, sliders back to
        0" resolution as the link-toggle case (see
        _on_flatten_link_toggled) -- the glyph doesn't visibly move,
        since 0/0 against a snapshot taken from the CURRENT canvas
        state reproduces exactly what's already on screen."""
        if not self._flatten_snapshot:
            return
        snap_indices, _old_snapshot_paths = self._flatten_snapshot
        if not (set(snap_indices) & set(affected_indices)):
            return
        self._flatten_snapshot = (snap_indices, copy.deepcopy(self.canvas.paths))
        self._flatten_session_edit = None
        for slider, label in ((self.flatten_x_slider, self.flatten_x_value_label),
                               (self.flatten_y_slider, self.flatten_y_value_label)):
            slider.blockSignals(True)
            slider.setValue(0)
            slider.blockSignals(False)
            label.setText("0")

    def _on_offset_finished(self, indices: list[int], dx: float, dy: float):
        """The canvas has already fully applied and shown the final
        offset (via apply_paths_preview during the drag) -- this only
        needs to bake it into the recipe. The list icon still needs an
        explicit nudge though, same as Flatten's commit -- see its own
        comment on this exact point."""
        if not self.project or self.current_cp is None:
            return
        recipe = self.project.glyphs[self.current_cp]
        self._push_undo(self.current_cp, recipe.edits)
        recipe.edits.append(EditStep(op="offset_part", args={
            "indices": indices, "dx": dx, "dy": dy,
        }))
        self._refresh_glyph_icon(self.current_cp)
        self._refresh_flatten_snapshot_if_stale(indices)

    def _on_snap_toggle_requested(self, which: str):
        """G/S/H tap-toggle -- flips the matching checkbox directly.
        Nothing else to do here: the checkbox's own existing `toggled`
        connection to _on_grid_settings_changed already syncs the
        canvas's real snap_to_grid/snap_to_points/snap_to_xheight_
        baseline state from it, the same path a manual click on the
        checkbox itself already goes through."""
        checkbox = {
            "grid": self.snap_grid_check,
            "points": self.snap_points_check,
            "xheight_baseline": self.snap_xheight_baseline_check,
        }.get(which)
        if checkbox is not None:
            checkbox.setChecked(not checkbox.isChecked())

    def _on_cursor_moved(self, x: float, y: float):
        # Match the ruler's convention (origin at the box's own
        # top-left corner), not raw scene coordinates -- the two were
        # inconsistent: the ruler already shows "0" at the box corner,
        # but this was showing the raw underlying value (here, 1.0,
        # since cap_top_px happens to be 1.0 rather than exactly 0),
        # so the same point read as "0" on the ruler and "1" in the
        # status bar.
        if self.canvas.box_rect is not None:
            ox, oy = self.canvas.box_rect[0], self.canvas.box_rect[1]
        else:
            ox, oy = 0.0, 0.0
        self.coord_label.setText(f"x: {x - ox:6.1f}  y: {y - oy:6.1f}")

    def _on_view_changed(self):
        """Zoom or pan changed which part of the canvas is actually
        visible -- refresh just the rulers' VIEW range to match (the
        box values, which anchor the 0 origin and the round-number
        ticks, don't change; only how much of the margin around the
        box is currently in view does).

        Also re-run the grid's own reflow here, since this fires on
        every zoom step too -- CenterPanel.reflow() only actually
        resizes anything when its target size genuinely changed (e.g.
        crossing the fit/zoomed-in boundary), so this doesn't loop:
        a resize it causes re-fires this same handler once, computes
        the identical target the second time, and settles."""
        if self.canvas.box_rect is None:
            return
        self.center_panel.reflow()
        self._refresh_rulers_and_strips()

    def _refresh_rulers_and_strips(self):
        """The ruler/strip half of _on_view_changed, WITHOUT reflow() --
        split out so a guide move can refresh these live (see
        _on_guide_changed) without also re-running reflow(). reflow()
        reads box_rect via _content_aspect(), and box_rect now updates
        live the instant a guide moves (see GlyphCanvas.on_guide_moved)
        -- calling reflow() on every intermediate position DURING a
        drag, not just at the end, meant the canvas widget's own size
        could change mid-gesture, which shifts the viewport's mapped
        scene range, which shifts the strip's own coordinate mapping
        while the user is still actively dragging it. Confirmed as a
        real, reproducible bug: a deliberate partial drag ended up
        moving descender to the opposite extreme entirely, from the
        drag's own reference frame shifting out from under it mid-move.
        Reflow still belongs in _on_view_changed (genuine viewport
        changes -- zoom, pan, window resize -- should still resize the
        canvas as needed), just not triggered by every guide-drag tick
        too."""
        if self.canvas.box_rect is None:
            return
        # The canvas's own _zoom_level is relative to fit-to-window
        # (1.0 = fit), which isn't what "100%" means to a user -- the
        # actual on-screen scale (screen px per source px, the
        # standard "100% = true size" convention from image/vector
        # editors) is the transform's own scale factor instead.
        zoom_pct = self.canvas.transform().m11() * 100
        self.zoom_label.setText(f"{zoom_pct:4.0f}%")
        x0, y0, x1, y1 = self.canvas.box_rect
        visible = self.canvas.mapToScene(self.canvas.viewport().rect()).boundingRect()
        canvas_w = canvas_h = None
        if self.canvas.source_bounds is not None:
            canvas_w, canvas_h = self.canvas.source_bounds
        self.ruler.set_values(visible.left(), visible.right(), x0, x1,
                               average_advance=self._average_advance(), canvas_max=canvas_w)
        self.vertical_ruler.set_values(visible.top(), visible.bottom(), y0, y1, canvas_max=canvas_h)
        self.guide_strip.set_view(visible.top(), visible.bottom())
        self.advance_strip.set_view(visible.left(), visible.right())

    def _on_guide_changed(self, name: str, position: float):
        if not self.project:
            return
        m = self.project.metrics
        field_map = {"cap": "cap_top_px", "x-height": "xheight_top_px",
                     "baseline": "baseline_px", "descender": "descender_bottom_px",
                     "advance": "advance_px"}
        if name in field_map:
            setattr(m, field_map[name], position)
        self._refresh_metrics_label()
        # Refresh the rulers and both pill strips immediately -- they
        # otherwise only ever caught up on a resize (which is what
        # actually calls this same refresh via viewChanged), not on a
        # guide move itself. Confirmed as the source of two separate,
        # related symptoms: a pill visibly lagging behind its own
        # guide's live position, and the ruler's own regular ticks not
        # reflecting a moved cap/baseline until some LATER resize (e.g.
        # a fullscreen toggle) coincidentally triggered the refresh
        # that should have happened immediately. Deliberately the
        # lighter-weight refresh, not the full _on_view_changed --
        # see _refresh_rulers_and_strips for why reflow() specifically
        # must NOT run on every guide-drag tick.
        self._refresh_rulers_and_strips()

    def _on_point_committed(self, path_index: int, point_index: int, x: float, y: float):
        """Record a dragged point as a replayable edit step -- without
        this, edits only ever lived in the canvas's in-memory paths and
        vanished the moment a different glyph was selected.

        Always APPENDS a new move_point step now -- an earlier version
        updated an existing step for this exact (path_index,
        point_index) pair in place instead, to avoid piling up
        redundant history for repeated adjustments of one vertex.
        Confirmed as the actual cause of a real, reported bug:
        path_index/point_index are only ever local, structural
        identifiers -- break_path and join_paths both routinely
        renumber what a given pair refers to, so the SAME literal
        (index, point_index) can legitimately mean a completely
        different point at different moments in one glyph's history. A
        later drag whose (path_index, point_index) happened to
        numerically match a MUCH earlier, geometrically unrelated
        move_point step silently overwrote that older step's target
        coordinates in place -- invisible immediately (the canvas
        already showed the correct, live position), until something
        later forced a full retrace-and-replay of the whole edit
        history (Break and Join both used to, purely as a side effect
        of reusing _load_glyph_into_canvas for convenience -- see its
        own precomputed_paths parameter, added specifically so neither
        needs to anymore), at which point the corrupted step replayed
        against source and visibly displaced a point nobody had
        touched, often far outside the canvas, with nothing for Undo to
        revert since the corruption was never itself a distinct edit.
        The cost of always appending is a few extra, harmless entries
        in a glyph's saved edit history for a vertex nudged repeatedly
        across separate sessions -- trivial next to silent, undoable-
        only-by-accident data corruption."""
        if not self.project or self.current_cp is None:
            return
        recipe = self.project.glyphs[self.current_cp]
        self._push_undo(self.current_cp, recipe.edits)
        recipe.edits.append(EditStep(op="move_point", args={
            "index": path_index, "point_index": point_index,
            "new_x": x, "new_y": y,
        }))
        self._refresh_glyph_icon(self.current_cp)
        self._refresh_flatten_snapshot_if_stale({path_index})

    def _on_draw_mode_toggled(self, checked: bool):
        self.canvas.set_draw_mode(checked)
        self._refresh_onion_skin()

    def _on_onion_skin_toggled(self, _checked: bool):
        # Clicking this toolbar toggle (either direction) is the
        # confirmed way to clear a manually ALT+clicked reference (see
        # _refresh_onion_skin) and go back to the automatic nearest-
        # glyph search -- there's no separate "reset" control, this
        # IS it.
        self._onion_skin_override_cp = None
        self._refresh_onion_skin()

    def _refresh_onion_skin(self):
        # Previously gated on draw_action too, meaning the faint
        # reference outline only ever appeared while actively drawing
        # -- but comparing a glyph's proportions against its nearest
        # drawn neighbor is just as useful when simply reviewing, not
        # only when adding new strokes. The toolbar toggle alone is
        # the gate now.
        show = self.onion_skin_action.isChecked()
        if not show or not self.project or self.current_cp is None:
            self.canvas.set_onion_skin(None, 0, "round")
            return
        m = self.project.metrics
        half_width = self.project.font.weight_ratio * ((m.baseline_px - m.cap_top_px) / 2) \
            if m.cap_top_px is not None and m.baseline_px is not None else 10.0
        # A manually-picked reference (ALT+click on the glyph list --
        # see the eventFilter override) takes priority over the
        # automatic nearest-glyph search below, for as long as it's
        # set. Deliberately does NOT fall through to the automatic
        # search if this specific glyph happens to be blank -- an
        # explicit pick means "show exactly this one," and silently
        # substituting a different glyph the user didn't choose would
        # defeat the point of picking one at all. A DELETED override
        # (no longer in the project) is different: nothing meaningful
        # is left to keep showing, so that one case does fall back to
        # automatic, clearing the now-dangling override rather than
        # leaving onion skin silently blank indefinitely.
        if self._onion_skin_override_cp is not None:
            if self._onion_skin_override_cp in self.project.glyphs:
                paths = self._build_glyph_paths(self.project.glyphs[self._onion_skin_override_cp])
                self.canvas.set_onion_skin(paths or None, half_width, self.project.font.cap_style)
                return
            self._onion_skin_override_cp = None
        # Try candidates NEAREST-first, not just the single nearest one
        # -- "has source_file or edits" only means the glyph was
        # TOUCHED, not that it currently has real geometry (e.g. a
        # glyph drawn then fully deleted still has non-empty edits).
        # Falling back through the next-nearest ones until finding one
        # that actually traces to non-empty paths avoids silently
        # showing no onion skin just because the closest candidate
        # happens to be blank.
        candidates = sorted(
            (other_cp for other_cp, recipe in self.project.glyphs.items()
             if other_cp != self.current_cp and (recipe.source_file or recipe.edits)),
            key=lambda c: abs(c - self.current_cp))
        for neighbor_cp in candidates:
            paths = self._build_glyph_paths(self.project.glyphs[neighbor_cp])
            if paths:
                self.canvas.set_onion_skin(paths, half_width, self.project.font.cap_style)
                return
        self.canvas.set_onion_skin(None, 0, "round")

    def _build_glyph_paths(self, recipe) -> list:
        """Trace (if there's a source PNG) and apply every recorded
        edit -- the same reconstruction _load_glyph_into_canvas does,
        factored out here since onion skin needs it for an arbitrary
        OTHER glyph, possibly several times while searching for a
        non-blank neighbor."""
        if recipe.source_file:
            src_path = self.project.resolve_source_path(recipe)
            try:
                paths, _shape = tracing.trace_glyph(src_path)
            except Exception:
                # A neighboring glyph's own source PNG is missing or
                # unreadable -- falls back to empty rather than
                # crashing the CURRENT glyph's own view over a problem
                # with a completely different one it's merely trying to
                # show a faint overlay of. No status message here (see
                # _load_glyph_into_canvas's own identical situation,
                # which DOES show one) -- onion skin is a secondary,
                # assistive overlay, not the primary thing being
                # viewed; _on_selection_changed's own neighbor search
                # just moves on to the next candidate either way.
                paths = []
        else:
            paths = []
        return _apply_import_scale_and_edits(paths, recipe)

    def _on_path_drawn(self, points: list):
        if not self.project or self.current_cp is None:
            return
        recipe = self.project.glyphs[self.current_cp]
        self._push_undo(self.current_cp, recipe.edits)
        recipe.edits.append(EditStep(op="add_path", args={"new_path": points}))
        # rebuilds the scene, the icon, AND re-applies draw mode /
        # onion skin (see the end of _load_glyph_into_canvas) -- no
        # need to redo any of that here
        self._load_glyph_into_canvas(self.current_cp, preserve_view=True)

    def _reindex_groups_after_deletes(self, recipe, deleted_indices: list[int]):
        """Keeps recipe.groups in sync when one or more paths are
        deleted -- confirmed as a real, reported bug otherwise: Join
        already got this bookkeeping, but Delete and Break never did,
        so a delete anywhere in the glyph silently corrupted any
        stored group's indices, later restoring selection onto
        whatever unrelated segments happened to occupy those stale
        numbers.

        Processes indices in the given order, each treated as its OWN
        deletion applied one at a time -- correct as long as the
        caller passes them in DESCENDING order (deleting a higher
        index first never invalidates a lower index still queued)."""
        for deleted in deleted_indices:
            new_groups = []
            for g in recipe.groups:
                updated = [i - 1 if i > deleted else i for i in g if i != deleted]
                if len(updated) >= 2:
                    new_groups.append(updated)
            recipe.groups = new_groups

    def _delete_segments(self, recipe, segs) -> list:
        """Core of Delete, factored out so Paste can reuse the EXACT
        same per-piece delete logic for its own "replace a selection"
        behavior (see _on_paste_clicked) without duplicating it. No
        undo push and no canvas reload of its own -- the caller owns
        both, since Paste needs to chain more edits (the pasted paths
        themselves) into the SAME undo entry and reload only once at
        the very end, not once per step. Returns the resulting working
        paths list, since Paste needs to know how many paths remain
        (and in what order) to correctly index the ones it's about to
        append.

        Always item-level -- click/CTRL-click/drag-box only ever put
        specific PIECES into the selection (see
        GlyphCanvas._individual_selected), and SHIFT-click achieves a
        "delete the whole thing" result simply by having already
        flattened every one of a unit's pieces into the selection at
        click-time (see _select_shift_click) -- so by the time this
        runs there's no whole/partial distinction left to make;
        deleting exactly what's selected, piece by piece, produces the
        right result either way. For a polyline, this can split the
        remainder into several disconnected pieces; for a group, it
        shrinks (or, if every member is selected, dissolves) it."""
        by_path: dict[int, list[int]] = {}
        for seg in segs:
            by_path.setdefault(seg.path_index, []).append(seg.seg_index)

        working = [list(p) for p in self.canvas.paths]
        offset = 0
        for orig_path_index in sorted(by_path):
            cur_path = orig_path_index + offset
            for seg_index in sorted(by_path[orig_path_index], reverse=True):
                cur_start = seg_index
                piece_path = cur_path
                if cur_start > 0:
                    recipe.edits.append(EditStep(op="break_path", args={"index": piece_path, "point_index": cur_start}))
                    working = geometry.break_path(working, piece_path, cur_start)
                    self._reindex_groups_after_insert(recipe, piece_path + 1)
                    piece_path += 1
                    cur_start = 0
                    offset += 1
                if len(working[piece_path]) > 2:
                    recipe.edits.append(EditStep(op="break_path", args={"index": piece_path, "point_index": 1}))
                    working = geometry.break_path(working, piece_path, 1)
                    self._reindex_groups_after_insert(recipe, piece_path + 1)
                    offset += 1
                recipe.edits.append(EditStep(op="delete_path", args={"index": piece_path}))
                working = geometry.delete_path(working, piece_path)
                offset -= 1
                self._reindex_groups_after_deletes(recipe, [piece_path])
        return working

    def _on_delete_requested(self):
        if not self.project or self.current_cp is None:
            return
        segs = self.canvas.selected_segments
        if not segs:
            return
        recipe = self.project.glyphs[self.current_cp]
        self._push_undo(self.current_cp, recipe.edits)
        # Passed straight through as precomputed_paths -- _delete_segments
        # already computes this directly via geometry.break_path/delete_path
        # (a pure repartition of points already on screen, no coordinate
        # computation involved at all), so there's nothing to gain, and
        # real risk in doing so anyway, from also replaying the entire
        # edit history from source just to arrive at the same thing a
        # second way -- exactly the same reasoning Break/Join's own
        # precomputed_paths use already established (see
        # _load_glyph_into_canvas's own docstring). Confirmed as a real
        # gap otherwise: this had been silently discarding
        # _delete_segments' own returned result and forcing the full
        # retrace anyway.
        working = self._delete_segments(recipe, segs)
        self._load_glyph_into_canvas(self.current_cp, preserve_view=True, precomputed_paths=working)

    # -- Cut / Copy / Paste -------------------------------------------------

    def _build_clipboard_from_selection(self):
        """Same granularity distinction as Delete (see _delete_segments's
        own docstring), applied to what gets COPIED rather than what
        gets removed: for each distinct path touched by the current
        selection, if EVERY one of that path's own segments is
        present (regardless of which of the two selection layers each
        one happens to be in), the whole path is copied as one
        connected polyline, preserving its shape -- otherwise, each
        individually-present segment is copied as its own independent
        2-point piece, matching how a lone CTRL/plain-selected segment
        already behaves everywhere else. A SHIFT-selected whole
        polyline or group naturally lands in the first case, since
        SHIFT-click always flattens every one of a unit's pieces into
        the selection at once (see _select_shift_click).

        Existing groups are preserved into the clipboard's own
        'groups' (indices into 'paths', mirroring recipe.groups'
        structure) only when EVERY member of that group was itself
        copied whole -- a group with only some members selected isn't
        coherently "the group" anymore, so it's left ungrouped in the
        clipboard rather than guessing at a partial reconstruction."""
        segs = self.canvas.selected_segments
        if not segs:
            return None
        by_path: dict[int, set[int]] = {}
        for seg in segs:
            by_path.setdefault(seg.path_index, set()).add(seg.seg_index)

        clip_paths: list[list] = []
        orig_to_clip_index: dict[int, int] = {}
        whole_path_indices: set[int] = set()
        for pi in sorted(by_path):
            path = self.canvas.paths[pi]
            n_segs = len(path) - 1
            if by_path[pi] == set(range(n_segs)):
                orig_to_clip_index[pi] = len(clip_paths)
                clip_paths.append([tuple(pt) for pt in path])
                whole_path_indices.add(pi)
            else:
                for si in sorted(by_path[pi]):
                    clip_paths.append([tuple(path[si]), tuple(path[si + 1])])

        recipe = self.project.glyphs[self.current_cp]
        clip_groups = [
            [orig_to_clip_index[pi] for pi in g]
            for g in recipe.groups
            if all(pi in whole_path_indices for pi in g)
        ]
        return {"paths": clip_paths, "groups": clip_groups}

    def _on_copy_clicked(self):
        if not self.project or self.current_cp is None:
            return
        clip = self._build_clipboard_from_selection()
        if clip is None:
            return
        self._clipboard = clip
        self.paste_action.setEnabled(True)
        # Confirmed as a real, reported bug otherwise: _on_paste_placed
        # deliberately replaces whatever's currently selected with the
        # pasted content (see its own docstring) -- correct for the
        # genuine "select something, paste over it" case, but Copy
        # itself leaves the just-copied segment(s) selected too, with
        # nothing else having happened in between. Pasting right after
        # a Copy, in the SAME glyph, then saw that leftover selection
        # as "the thing to replace" and deleted the very thing that had
        # just been copied -- switching to a DIFFERENT glyph first only
        # ever avoided this by accident, since loading a glyph resets
        # selection anyway. Clearing it here (the same helper already
        # used for "the action is done, reset for whatever comes next"
        # elsewhere -- switching glyphs, Escape) means a Paste
        # immediately afterward has nothing of Copy's own to replace,
        # while an explicit new selection made afterward still replaces
        # exactly as intended.
        self.canvas.clear_all_selection()
        self._set_status_message(f"Copied {len(clip['paths'])} path(s).")

    def _on_cut_clicked(self):
        if not self.project or self.current_cp is None:
            return
        clip = self._build_clipboard_from_selection()
        if clip is None:
            return
        self._clipboard = clip
        self.paste_action.setEnabled(True)
        self._on_delete_requested()   # exact same delete path Delete itself uses

    def _on_paste_clicked(self):
        """Attaches a ghost preview of the clipboard to the cursor,
        placed with a single click -- see GlyphCanvas.start_paste_mode
        and _on_paste_placed (the actual insertion, deferred until
        placement) for the rest of this. Replaces the earlier
        immediate-paste-at-original-coordinates behavior entirely."""
        if not self.project or self.current_cp is None or not self._clipboard:
            return
        # Confirmed as a real, reported bug otherwise: without this,
        # start_paste_mode's own fallback (viewport centre) is what the
        # ghost starts at regardless of where the mouse actually is,
        # every time -- toolbar click, Ctrl+V, or the right-click canvas
        # menu all go through this exact same method. It only ever
        # LOOKED consistent for the right-click menu, purely because
        # right-clicking the canvas necessarily leaves the real cursor
        # already near the canvas, and the smallest subsequent move
        # snaps the ghost to it almost immediately -- Ctrl+V carries no
        # such guarantee (the mouse can be anywhere, including outside
        # the canvas or the window entirely), which is what made the
        # gap visible in the first place. Querying the live cursor here
        # and using it when it's actually over the canvas makes all
        # three trigger paths behave identically; start_paste_mode's own
        # viewport-centre fallback still covers the genuine case where
        # the mouse isn't over the canvas at all.
        viewport_pos = self.canvas.viewport().mapFromGlobal(QCursor.pos())
        if self.canvas.viewport().rect().contains(viewport_pos):
            cursor_pos = self.canvas.mapToScene(viewport_pos)
        else:
            cursor_pos = None
        self.canvas.start_paste_mode(self._clipboard["paths"], cursor_pos)

    def _on_paste_placed(self, x: float, y: float):
        """The actual insertion, run once GlyphCanvas emits pastePlaced
        (a single click during paste-follows-cursor placement -- see
        _on_paste_clicked/start_paste_mode). (x, y) is the snapped
        scene position the clipboard's own anchor (see
        canvas_view.paste_anchor_point's own docstring -- the top-left
        ENDPOINT among the clipboard's actual points, recomputed here
        the same way start_paste_mode computed it, not read back from
        the canvas's own private state, which start_paste_mode/
        mousePressEvent don't guarantee stays around past the emit)
        should land on.

        Replaces a current selection, not just adds alongside it --
        confirmed as the wanted behavior, matching how most Windows
        applications handle Paste, and specifically the SAME
        distinction Delete itself uses for what "replaces" means (see
        _delete_segments): whatever's currently selected is removed
        first, piece by piece, using the identical logic Delete runs,
        before the clipboard's paths are appended. Both steps share
        ONE undo entry, so a single Ctrl+Z reverses the whole paste,
        not just the insert half of it.

        add_path (see core/geometry.py) always appends to the END of
        the paths list, so the new paths' final indices are exactly
        len(working) before the loop, len(working)+1, and so on, in
        order -- no reindexing needed the way delete/break require,
        since nothing before them ever moves."""
        if not self.project or self.current_cp is None or not self._clipboard:
            return
        recipe = self.project.glyphs[self.current_cp]
        self._push_undo(self.current_cp, recipe.edits)

        segs = self.canvas.selected_segments
        working = self._delete_segments(recipe, segs) if segs else [list(p) for p in self.canvas.paths]

        anchor_x, anchor_y = paste_anchor_point(self._clipboard["paths"])
        dx, dy = x - anchor_x, y - anchor_y

        base_index = len(working)
        for path in self._clipboard["paths"]:
            offset_path = [(pt[0] + dx, pt[1] + dy) for pt in path]
            recipe.edits.append(EditStep(op="add_path", args={"new_path": [list(pt) for pt in offset_path]}))
            # Mirrors the recorded EditStep exactly -- see the
            # precomputed_paths call below for why that matters (same
            # reasoning as Delete's own fix, and Break/Join before it).
            # tuple(pt), not list(pt) -- matches self.canvas.paths' own
            # point representation everywhere else (see
            # _build_clipboard_from_selection, which already stores the
            # clipboard's own points as tuples for exactly this reason);
            # the EditStep's args use list(pt) instead, since those need
            # to be JSON-serializable, a completely separate concern
            # from what this in-memory precomputed_paths value should
            # look like.
            working.append(offset_path)
        new_indices = list(range(base_index, base_index + len(self._clipboard["paths"])))

        for group in self._clipboard["groups"]:
            recipe.groups.append(sorted(new_indices[i] for i in group))

        self._load_glyph_into_canvas(self.current_cp, preserve_view=True, restore_groups=False,
                                      precomputed_paths=working)
        # Left unselected, not selected as an individual/group piece
        # the way the old immediate-paste-at-original-coordinates flow
        # used to leave it -- confirmed as a real, reported bug
        # otherwise: pasting a second time (without the user
        # deliberately selecting something else first) saw the FIRST
        # paste's own, still-selected result as "the current selection
        # to replace" (see this method's own opening logic), silently
        # erasing it instead of adding a second, separate copy
        # alongside it. That earlier "select it so it's immediately
        # draggable" behavior made sense when paste landed wherever it
        # landed with no user control over position at all -- with
        # cursor-follows placement, the user already had full,
        # deliberate control over where this landed via the ghost
        # preview, so there's less need to immediately re-drag it right
        # after.
        self.canvas.clear_all_selection()
        self._on_selection_changed()

    def _reindex_groups_after_insert(self, recipe, insert_at: int):
        """Keeps recipe.groups in sync when a new path is inserted
        (e.g. by break_path, which always inserts its second half
        right after the first) -- everything at or after insert_at
        shifts up by one. Symmetric to _reindex_groups_after_deletes;
        same underlying bug this closes for the insert direction."""
        recipe.groups = [[i + 1 if i >= insert_at else i for i in g] for g in recipe.groups]

    def _on_break_clicked(self):
        if not self.project or self.current_cp is None or not self.canvas.selected_segments:
            return
        # Silently finishes any drag the canvas might still think is
        # active as a proper EditStep before reading self.canvas.paths
        # as a settled snapshot -- cheap, harmless insurance against
        # interactive state leaking into a structural operation, kept
        # even though it turned out unrelated to the specific
        # corruption bug this was originally added to help diagnose
        # (see _on_point_committed's own comment for the actual cause
        # and fix). No longer logged: the diagnostic warning here did
        # its job confirming this wasn't that bug's mechanism, and
        # nothing about a drag genuinely being active here is possible
        # in the app's current form regardless -- the cursor is
        # confined to the canvas during a drag and Break has no
        # keyboard shortcut, so there's no way for it to run mid-drag
        # to begin with.
        self.canvas.finish_any_pending_drag()
        recipe = self.project.glyphs[self.current_cp]
        segs = self.canvas.selected_segments
        by_path: dict[int, list[int]] = {}
        for seg in segs:
            by_path.setdefault(seg.path_index, []).append(seg.seg_index)

        # Per-path dispatch, not a single mode for the whole selection
        # -- click/CTRL-click/drag-box never merge touched pieces into
        # a single notion of "the selection's unit" the way the old
        # model did (see GlyphCanvas._select_ctrl_click and friends),
        # so a selection can legitimately mix a grouped path with an
        # ordinary polyline segment in the same click. Each touched
        # path gets whichever meaning applies to IT: a member of an
        # existing group is ungrouped (removed from that group, no
        # geometry change); anything else gets the normal geometric
        # split on whichever of its OWN segments are selected,
        # unchanged from how Break has always worked for a polyline.
        grouped_paths = {pi for pi in by_path if any(pi in g for g in recipe.groups)}
        geometric_paths = {pi: idxs for pi, idxs in by_path.items() if pi not in grouped_paths}

        self._push_undo(self.current_cp, recipe.edits)
        # Computed unconditionally, not just inside the geometric_paths
        # branch below -- this becomes what actually gets shown
        # afterward (see the precomputed_paths call at the end of this
        # method), so it needs to exist even when this Break turned out
        # to be ungroup-only with no geometry change at all.
        working = [list(p) for p in self.canvas.paths]

        if grouped_paths:
            new_groups = []
            for g in recipe.groups:
                remaining = [i for i in g if i not in grouped_paths]
                if len(remaining) >= 2:
                    new_groups.append(remaining)
            recipe.groups = new_groups

        if geometric_paths:
            # Isolate every selected segment among the non-grouped
            # touched paths. Two things make the index bookkeeping
            # safe here:
            #   - within one ORIGINAL path, processing selected segments
            #     in DESCENDING seg_index order means each break only
            #     ever peels the high end off into a new path at
            #     index+1, leaving the low remainder at the same index
            #     -- so earlier (higher-index) breaks never invalidate a
            #     later (lower-index) one still to come in the same path.
            #   - across DIFFERENT original paths, processing them in
            #     ascending order while tracking how many extra paths
            #     have been inserted so far (each break adds exactly
            #     one) gives the correct current index for each path as
            #     we reach it.
            steps: list[EditStep] = []
            offset = 0
            for orig_path_index in sorted(geometric_paths):
                cur_path = orig_path_index + offset
                for seg_index in sorted(geometric_paths[orig_path_index], reverse=True):
                    cur_start = seg_index
                    piece_path = cur_path
                    if cur_start > 0:
                        steps.append(EditStep(op="break_path", args={"index": piece_path, "point_index": cur_start}))
                        working = geometry.break_path(working, piece_path, cur_start)
                        self._reindex_groups_after_insert(recipe, piece_path + 1)
                        piece_path += 1
                        cur_start = 0
                        offset += 1
                    if len(working[piece_path]) > 2:
                        steps.append(EditStep(op="break_path", args={"index": piece_path, "point_index": 1}))
                        working = geometry.break_path(working, piece_path, 1)
                        self._reindex_groups_after_insert(recipe, piece_path + 1)
                        offset += 1
            recipe.edits.extend(steps)

        # restore_groups=False -- confirmed as a real, reported issue
        # otherwise: the generic restoration this would normally run
        # sweeps in and highlights EVERY OTHER, unrelated persisted
        # group too, not just whatever this specific Break just
        # touched (see _load_glyph_into_canvas's own comment).
        #
        # precomputed_paths=working -- Break already computed the exact
        # correct result directly (a pure repartition of points already
        # on screen, no coordinate computation involved at all), so
        # there's nothing to gain from also replaying the entire edit
        # history from source just to arrive at the same thing a second
        # way -- and real risk in doing so anyway, confirmed as the
        # actual mechanism behind a previously-reported bug where a
        # DIFFERENT, unrelated earlier edit's corrupted value only ever
        # surfaced at the exact moment something forced a full retrace
        # (see _on_point_committed's own comment on the corruption
        # itself, which this avoids re-triggering here but doesn't fix
        # on its own).
        self._load_glyph_into_canvas(self.current_cp, preserve_view=True, restore_groups=False,
                                      precomputed_paths=working)
        # refreshes the list icon correctly, using the paths just computed above --
        # refreshing it here instead would use self.canvas.paths from BEFORE this
        # edit, showing the previous state until the glyph was next reselected
        if grouped_paths:
            # The just-ungrouped pieces are ordinary, individual paths
            # again -- shown as such (not re-highlighted as a group,
            # since they no longer are one).
            self.canvas.clear_all_selection()
            self.canvas._individual_selected = {(pi, 0) for pi in grouped_paths
                                                 if 0 <= pi < len(self.canvas.paths)}
            self.canvas._last_action_was_plain_click = False
            self.canvas._last_action_was_shift = False
            self.canvas._refresh_selection_visuals()

    def _on_join_clicked(self):
        if not self.project or self.current_cp is None:
            return
        segs = self.canvas.selected_segments
        distinct_paths = sorted({s.path_index for s in segs})
        if len(distinct_paths) < 2:
            return
        recipe = self.project.glyphs[self.current_cp]
        self._push_undo(self.current_cp, recipe.edits)
        # Checked ONLY against these selected paths, nothing else in
        # the glyph -- see find_join_chain's own docstring. All-or-
        # nothing: either the whole selection chains into one
        # polyline, or the whole selection groups instead -- never a
        # partial mix of some pairs joining while others get left
        # aside, which is what an earlier version did and which made
        # results hard to predict.
        chain = geometry.find_join_chain(self.canvas.paths, distinct_paths)

        if chain is not None:
            working = [list(p) for p in self.canvas.paths]
            current_index = {pi: pi for pi in distinct_paths}
            base = current_index[chain[0]]
            for pi in chain[1:]:
                other = current_index[pi]
                recipe.edits.append(EditStep(op="join_paths", args={
                    "index_a": base, "index_b": other,
                }))
                # Mirrors the recorded EditStep exactly -- computed
                # locally so the display below doesn't need a full
                # retrace to arrive at the same result a second way
                # (see the precomputed_paths call at the end of this
                # method for why that matters).
                working = geometry.join_paths(working, base, other)
                deleted, kept = max(base, other), min(base, other)
                base = kept
                for key in current_index:
                    if current_index[key] == deleted:
                        current_index[key] = kept
                    elif current_index[key] > deleted:
                        current_index[key] -= 1
                # Existing groups need the SAME remap -- a path that
                # was part of an earlier group and just got merged
                # into another now refers to the surviving index.
                #
                # Deliberately NOT the shared _reindex_groups_after_
                # deletes helper, even though this looks like the same
                # "a path index went away" case it handles -- confirmed
                # via a direct comparison that the two need genuinely
                # different behavior, not just different code: that
                # helper REMOVES a deleted index from any group it's in
                # (correct for an actual deletion, where the path is
                # simply gone), while a JOIN is a MERGE -- a group
                # referencing the deleted index should keep referencing
                # what it merged INTO, not silently lose that member
                # entirely. Consolidating this into the shared helper
                # would be a real regression, not a cleanup.
                recipe.groups = [[kept if idx == deleted else (idx - 1 if idx > deleted else idx)
                                  for idx in group] for group in recipe.groups]
            final_selection_paths = {base}
        else:
            # Doesn't form one clean chain -- the WHOLE selection
            # groups instead. Any multi-point polyline among the
            # selected paths is decomposed into individual single-
            # segment pieces first -- once something is part of a
            # group, polylines no longer exist, only atomic segments
            # do (this is also what makes Break's "ungroup" meaning
            # coherent: there's no leftover multi-point structure to
            # worry about splitting further).
            working = [list(p) for p in self.canvas.paths]
            # Each original path can become SEVERAL final indices once
            # decomposed, so this maps to a LIST, not a single int.
            current_index: dict[int, list[int]] = {pi: [pi] for pi in distinct_paths}
            # Highest original index first -- decomposing a path only
            # ever inserts new pieces AFTER its own position, so
            # processing this way means paths with a lower original
            # index are never touched by an earlier iteration.
            for pi in sorted(distinct_paths, reverse=True):
                idx = current_index[pi][0]
                n = len(working[idx]) if 0 <= idx < len(working) else 0
                if n <= 2:
                    continue   # already a single segment
                new_indices = [idx]
                for _ in range(n - 2):
                    recipe.edits.append(EditStep(op="break_path", args={
                        "index": idx, "point_index": 1,
                    }))
                    working = geometry.break_path(working, idx, 1)
                    idx += 1
                    new_indices.append(idx)
                    for key in current_index:
                        if key == pi:
                            continue
                        current_index[key] = [i + 1 if i >= idx else i for i in current_index[key]]
                    # Byte-for-byte the same shift _reindex_groups_after_
                    # insert already does -- unlike the delete-side case
                    # above, this one really is a plain duplicate (a new
                    # path inserted at idx, nothing else about it is
                    # Join-specific), so it reuses the shared helper
                    # directly instead of hand-maintaining a second copy.
                    self._reindex_groups_after_insert(recipe, idx)
                current_index[pi] = new_indices

            all_final_indices = sorted({i for indices in current_index.values() for i in indices})
            # Merges with (rather than duplicates alongside) any
            # existing group sharing a member -- flat structure, never
            # groups nested inside groups.
            merged_group = set(all_final_indices)
            remaining_groups = []
            for g in recipe.groups:
                if merged_group & set(g):
                    merged_group |= set(g)
                else:
                    remaining_groups.append(g)
            recipe.groups = remaining_groups + [sorted(merged_group)]
            final_selection_paths = merged_group
            self._set_status_message(
                "Selected segments don't form one continuous line \u2014 "
                "grouped instead so they can still be moved together.")

        # restore_groups=False -- confirmed as a real, reported issue
        # otherwise: the generic restoration this would normally run
        # sweeps in and highlights EVERY OTHER, unrelated persisted
        # group too, not just the one this specific Join just touched
        # (see _load_glyph_into_canvas's own comment). The selection is
        # set explicitly below instead, reflecting only this join's own
        # outcome -- the resulting polyline if the chain succeeded, or
        # the new/merged group otherwise.
        #
        # precomputed_paths=working -- same reasoning as Break's own
        # call site: both branches above already compute the exact
        # correct result locally (join_paths/break_path calls mirroring
        # the recorded EditSteps exactly), so there's nothing to gain,
        # and real risk, in also replaying the whole edit history from
        # source just to arrive at the same thing a second way.
        self._load_glyph_into_canvas(self.current_cp, preserve_view=True, restore_groups=False,
                                      precomputed_paths=working)
        self.canvas.clear_all_selection()
        seg_pairs: set[tuple[int, int]] = set()
        for pi in final_selection_paths:
            seg_pairs |= {(pi, si) for si in range(len(self.canvas.paths[pi]) - 1)} if 0 <= pi < len(self.canvas.paths) else set()
        self.canvas._shift_selected = seg_pairs
        self.canvas._last_action_was_shift = True
        self.canvas._last_action_was_plain_click = False
        self.canvas._refresh_selection_visuals()

    def _refresh_editing_locked_state(self) -> None:
        """Reflects whether the currently-displayed glyph is excluded
        from export -- viewable, but not editable (see canvas.read_only
        and the toolbar actions this locks). Called from
        _load_glyph_into_canvas (the glyph itself changed) and
        _set_glyphs_excluded (the currently-viewed glyph's own excluded
        state changed without switching away from it).

        draw_action and paste_action specifically, not delegated to
        _on_selection_changed's own enabled-state logic -- neither
        depends on there being a current selection at all (a mode
        toggle, and clipboard content respectively), so neither would
        naturally end up disabled just because canvas.read_only is
        blocking selection itself. Break/Join/Cut/Copy/Delete DO all
        depend on an actual selection existing, which read_only already
        prevents from ever forming in the first place (see
        GlyphCanvas.mousePressEvent's own guard) -- _on_selection_changed
        still checks this explicitly too, defensively, rather than
        relying solely on the selection always being empty."""
        recipe = self.project.glyphs.get(self.current_cp) if self.project and self.current_cp is not None else None
        locked = bool(recipe.excluded) if recipe else False
        self.canvas.read_only = locked
        self.draw_action.setEnabled(not locked)
        self.paste_action.setEnabled(not locked and bool(self._clipboard))
        self.select_all_toolbar_action.setEnabled(not locked)

    def _on_selection_changed(self):
        segs = self.canvas.selected_segments
        distinct_paths = {s.path_index for s in segs}
        # Break's logic (_on_break_clicked) handles two different
        # things per selected path: an ordinary polyline with more
        # than 2 points can be geometrically split, and a path that's
        # part of an existing group can be ungrouped -- confirmed as a
        # real, reported bug checking only the first: a group's own
        # members are always 2-point (single-segment) paths by the
        # time they're grouped, so "has more than 2 points" was always
        # False for them, leaving Break permanently disabled on a
        # group no matter how it was selected. Bounds-checked the same
        # way as before: this can in principle fire while paths is
        # mid-update relative to what's selected (e.g. during scene
        # teardown), so an out-of-range path_index just doesn't count
        # rather than raising.
        recipe = self.project.glyphs[self.current_cp] if self.project and self.current_cp is not None else None
        grouped_paths = {i for g in recipe.groups for i in g} if recipe else set()
        can_break = any(
            0 <= s.path_index < len(self.canvas.paths)
            and (len(self.canvas.paths[s.path_index]) > 2 or s.path_index in grouped_paths)
            for s in segs
        )
        can_join = len(distinct_paths) >= 2
        # Defensive, not the primary mechanism -- canvas.read_only
        # already prevents a selection from ever forming at all while
        # the current glyph is excluded (see GlyphCanvas.
        # mousePressEvent's own guard), so segs/distinct_paths should
        # already be empty by the time this runs. ANDed in explicitly
        # anyway rather than relying solely on that always holding true.
        locked = bool(recipe.excluded) if recipe else False
        can_break = can_break and not locked
        can_join = can_join and not locked
        self.break_action.setEnabled(can_break)
        self.join_action.setEnabled(can_join)
        has_selection = bool(segs) and not locked
        self.copy_action.setEnabled(has_selection)
        self.cut_action.setEnabled(has_selection)

        valid_paths = sorted(p for p in distinct_paths if 0 <= p < len(self.canvas.paths))
        if valid_paths:
            # Only start a FRESH session if this is actually a different
            # selection than what's already being tracked -- reacting to
            # every selectionChanged unconditionally (e.g. re-clicking
            # the same already-selected segment) would re-baseline the
            # snapshot to the canvas's CURRENT state mid-session, which
            # is exactly the bug this whole redesign fixes: it'd make
            # "0" quietly stop meaning the true original again.
            current_indices = self._flatten_snapshot[0] if self._flatten_snapshot else None
            if valid_paths != current_indices:
                self._flatten_snapshot = (valid_paths, copy.deepcopy(self.canvas.paths))
                self._flatten_session_edit = None
                # Reset to 0/0 (no change) for this NEW selection --
                # blocked so the reset itself doesn't trigger a preview
                # against a snapshot that was just replaced.
                for slider, label in ((self.flatten_x_slider, self.flatten_x_value_label),
                                       (self.flatten_y_slider, self.flatten_y_value_label)):
                    slider.blockSignals(True)
                    slider.setValue(0)
                    slider.blockSignals(False)
                    label.setText("0")
                # Link doesn't carry over to a genuinely different
                # selection either -- signal-blocked, since the normal
                # toggle handler's own commit-and-fresh-session work is
                # exactly what the reset just above already did by hand.
                self.flatten_link_check.blockSignals(True)
                self.flatten_link_check.setChecked(False)
                self.flatten_link_check.blockSignals(False)
                self._set_flatten_link_visual(False)
            self._set_flatten_enabled(True)
        else:
            self._flatten_snapshot = None
            self._flatten_session_edit = None
            self._set_flatten_enabled(False)
            # Reset the displayed value too, not just the snapshot/
            # enabled state -- leaving it showing the last value (e.g.
            # 70) while merely greyed out reads as "resume from here",
            # which isn't true: the moment the selection is gone, so is
            # the only record of what "original" meant (short of Undo).
            # Jumping to 0 here, at deselection, makes that visually
            # honest immediately rather than only on the NEXT selection.
            for slider, label in ((self.flatten_x_slider, self.flatten_x_value_label),
                                   (self.flatten_y_slider, self.flatten_y_value_label)):
                slider.blockSignals(True)
                slider.setValue(0)
                slider.blockSignals(False)
                label.setText("0")
            # Confirmed as a real, reported source of confusion
            # otherwise: the checkbox already greys out correctly here
            # (see _set_flatten_enabled), but staying visibly CHECKED
            # while disabled read as "link mode is still on, just
            # temporarily unavailable" rather than "this reset" --
            # switching glyphs (or losing the selection any other way)
            # now starts the next selection unlinked, same as a brand
            # new session should.
            self.flatten_link_check.blockSignals(True)
            self.flatten_link_check.setChecked(False)
            self.flatten_link_check.blockSignals(False)
            self._set_flatten_link_visual(False)

    def _select_all_segments(self):
        """Stale from before the two-layer selection model existed --
        confirmed as a real, reported regression: this only toggled
        Qt's own raw setSelected() flag directly, never touching
        _individual_selected/_shift_selected (the actual source of
        truth _refresh_selection_visuals and selected_segments read
        from now), so nothing visually highlighted and nothing
        downstream -- Delete, Join, Copy -- saw anything as selected
        either.

        Populates _individual_selected, not _shift_selected -- matches
        how a plain drag-box selection works (see _select_drag_box),
        the closest existing analog: multiple, potentially unrelated
        segments selected at once, not a single SHIFT-style whole-unit
        grab. _last_action_was_plain_click is explicitly False, same
        reasoning as a CTRL-built multi-selection: with everything
        selected at once, endpoint-dragging shouldn't be treated as
        unambiguous the way a single plain-clicked item's does."""
        canvas = self.canvas
        if not canvas._segments or canvas.read_only:
            return
        canvas._individual_selected = {(seg.path_index, seg.seg_index) for seg in canvas._segments}
        canvas._shift_selected = set()
        canvas._last_action_was_plain_click = False
        canvas._last_action_was_shift = False
        canvas._refresh_selection_visuals()

    def _refresh_undo_redo_enabled_state(self):
        """Reflects whether there's ANY undo/redo history for the
        currently-displayed glyph specifically -- not the earlier,
        GLOBAL-stack check (which stayed enabled even when every entry
        on it belonged to a DIFFERENT glyph entirely). Called from
        _push_undo/_undo/_redo (the stack itself changed) AND from
        _load_glyph_into_canvas (the CURRENT glyph changed, which can
        flip this even though neither stack did). Confirmed as
        sufficient on its own, replacing the earlier cross-glyph
        prompt-and-continue behavior entirely: the button being
        correctly grayed out already communicates "nothing left to undo
        for this glyph," with no separate message needed."""
        self.undo_action.setEnabled(any(entry[0] == self.current_cp for entry in self._undo_stack))
        self.redo_action.setEnabled(any(entry[0] == self.current_cp for entry in self._redo_stack))

    def _push_undo(self, cp: int, current_edits: list):
        # groups snapshotted alongside edits, not passed in separately
        # -- this keeps every EXISTING call site unchanged (they only
        # ever knew about edits) while still making group membership
        # undo/redo correctly, since it's part of the same recipe
        # state a user would expect one Ctrl+Z to step back through.
        #
        # source_file/import_scale deliberately NOT tracked here at all
        # (this once carried a 5-tuple including both) -- confirmed
        # safe now that adding a glyph or replacing its source file no
        # longer pushes anything onto this stack at all (see
        # _add_glyph_recipe's own docstring for why): those two fields
        # now only ever change via that path, so by the time any
        # ordinary, segment-level edit reaches this method, they're
        # already fixed for the rest of this glyph's own lifetime on
        # this stack and have nothing left to snapshot.
        recipe = self.project.glyphs[cp] if self.project else None
        current_groups = recipe.groups if recipe else []
        self._undo_stack.append((cp, copy.deepcopy(current_edits), copy.deepcopy(current_groups)))
        # Only THIS glyph's own pending redo entries -- confirmed as
        # the actual wanted behavior for per-glyph undo/redo (see
        # _undo's own comment on the redesign this is part of): a new
        # edit to glyph A shouldn't invalidate glyph B's own, unrelated
        # redo history just because it happened to be more recent in
        # the GLOBAL sequence. An earlier version cleared the WHOLE
        # redo stack unconditionally here, matching how most single-
        # document editors work -- not what per-glyph scoping is for.
        self._redo_stack = [entry for entry in self._redo_stack if entry[0] != cp]
        self._refresh_undo_redo_enabled_state()

    def _undo(self):
        if not self._undo_stack or not self.project:
            return
        # Per-glyph scoped -- searches for the MOST RECENT entry
        # belonging to the glyph currently on screen, not just the
        # literal top of the stack. Confirmed as a real, reported
        # point of friction otherwise: editing glyph 2 after glyph 1
        # meant Undo had to walk all the way back through every one of
        # glyph 2's own changes before ever reaching what you actually
        # wanted undone in glyph 1.
        match_index = next((i for i in range(len(self._undo_stack) - 1, -1, -1)
                             if self._undo_stack[i][0] == self.current_cp), None)
        if match_index is None:
            # Nothing left to undo for the CURRENT glyph specifically --
            # simply does nothing, full stop. Confirmed as the wanted,
            # simplified behavior, replacing the earlier cross-glyph
            # "continue with a DIFFERENT glyph's own history?" prompt
            # entirely: undo now stays strictly scoped to whatever
            # glyph is currently displayed. Reaching a different
            # glyph's own history means switching to it first,
            # manually, via the glyph list -- not something this button
            # does on its own anymore. The button itself is already
            # correctly grayed out whenever this branch would be
            # reached (see _refresh_undo_redo_enabled_state), so
            # there's nothing further to communicate here.
            return
        cp, prev_edits, prev_groups = self._undo_stack.pop(match_index)
        recipe = self.project.glyphs[cp]
        self._redo_stack.append((cp, copy.deepcopy(recipe.edits), copy.deepcopy(recipe.groups)))
        recipe.edits = prev_edits
        recipe.groups = prev_groups
        # _load_glyph_into_canvas already recomputes undo/redo's own
        # enabled state internally (cp == self.current_cp here, since
        # undo only ever operates on the currently-displayed glyph now)
        # -- no separate call needed.
        self._load_glyph_into_canvas(cp, preserve_view=True)   # refreshes the icon correctly, using post-undo paths

    def _redo(self):
        if not self._redo_stack or not self.project:
            return
        # Same per-glyph scoping as _undo, symmetrically.
        match_index = next((i for i in range(len(self._redo_stack) - 1, -1, -1)
                             if self._redo_stack[i][0] == self.current_cp), None)
        if match_index is None:
            # Same reasoning as _undo's own identical branch.
            return
        cp, next_edits, next_groups = self._redo_stack.pop(match_index)
        recipe = self.project.glyphs[cp]
        self._undo_stack.append((cp, copy.deepcopy(recipe.edits), copy.deepcopy(recipe.groups)))
        recipe.edits = next_edits
        recipe.groups = next_groups
        self._load_glyph_into_canvas(cp, preserve_view=True)   # same reasoning as _undo's own identical comment

    def _update_collapsed_tabs(self):
        """Polled on a plain QTimer (not from within a QSplitterHandle
        subclass -- see CollapsedPaneTab's own comment for why) to
        detect when either the glyph list or the metrics sidebar has
        been dragged to zero width, and show a small named tab over
        that edge so it isn't lost with no visual trace once
        collapsed."""
        sizes = self.splitter.sizes()
        if len(sizes) != 3:
            return
        # The tabs are parented to MainWindow, not the splitter (see
        # why in __init__), so their coordinates need mapping into
        # MainWindow's own space rather than assuming the splitter's
        # origin is (0, 0).
        origin = self.splitter.mapTo(self, QPoint(0, 0))
        h = self.splitter.height()
        tab_h = min(80, max(h - 40, 20))
        tab_y = origin.y() + (h - tab_h) // 2
        if sizes[0] == 0:
            self._left_collapsed_tab.setGeometry(origin.x(), tab_y, 18, tab_h)
            self._left_collapsed_tab.show()
            self._left_collapsed_tab.raise_()
        else:
            self._left_collapsed_tab.hide()
        if sizes[2] == 0:
            self._right_collapsed_tab.setGeometry(
                origin.x() + self.splitter.width() - 18, tab_y, 18, tab_h)
            self._right_collapsed_tab.show()
            self._right_collapsed_tab.raise_()
            self._float_grid_box(True)
        else:
            self._right_collapsed_tab.hide()
            self._float_grid_box(False)

        # Grip indicators -- shown on a handle only while the pane
        # it's attached to is actually expanded; once that pane
        # collapses, its handle itself goes to zero width (nothing
        # left to show a grip on), and the collapsed-pane tab above
        # already takes over as the "there's something here" cue.
        for grip, handle, pane_size in (
            (self._left_grip, self.splitter.handle(1), sizes[0]),
            (self._right_grip, self.splitter.handle(2), sizes[2]),
        ):
            if pane_size == 0:
                grip.hide()
                continue
            handle_origin = handle.mapTo(self, QPoint(0, 0))
            grip.setGeometry(handle_origin.x(), handle_origin.y(),
                              handle.width(), handle.height())
            grip.show()
            grip.raise_()

    def _float_grid_box(self, should_float: bool):
        """Moves Grid & snap from right_layout into bottom_bar_layout
        (should_float=True), alongside Weight and Flatten, or puts it
        back exactly where it came from (should_float=False) -- see
        _grid_box_floating's own comment for why this only ever
        happens transiently, during an actual collapsed state, rather
        than living there permanently. Idempotent: safe to call every
        timer tick regardless of whether a transition is actually
        needed, since _grid_box_floating is checked first.

        A real bottom_bar_layout.addWidget, not a manually-positioned
        overlay -- confirmed as a real, reported bug with an earlier,
        manually-positioned version: bottom_bar's own content
        (Weight/Flatten) already grows to fill whatever width the
        collapsed sidebar freed up (see their own stretch factors),
        so an independent overlay anchored to the window's right edge
        ends up competing for the exact same pixels Flatten just grew
        into, rather than actually sharing the row. Letting
        bottom_bar_layout itself place Grid & snap as a normal third
        member removes the possibility of that overlap entirely --
        Weight/Flatten simply get correspondingly less of the row's
        width while it's there, which only ever happens for as long as
        the sidebar stays collapsed.

        Restoring is a plain right_layout.addWidget(grid_box) with no
        index -- always correct here because Grid & snap is already
        the LAST widget in that layout in the normal, expanded state
        (see its own construction), so appending it again lands it
        back in exactly the same place it was pulled from."""
        if should_float == self._grid_box_floating:
            return
        if should_float:
            self.right_panel.layout().removeWidget(self.grid_box)
            self.grid_box.setParent(self.bottom_bar)
            self.bottom_bar.layout().addWidget(self.grid_box, 0)
            self.grid_box.show()
        else:
            self.bottom_bar.layout().removeWidget(self.grid_box)
            self.grid_box.setParent(self.right_panel)
            self.right_panel.layout().addWidget(self.grid_box)
            self.grid_box.show()
        self._grid_box_floating = should_float


    def _clamp_pane_widths(self, pos, index):
        """Connected to splitter.splitterMoved -- corrects the left or
        right pane back to its own ideal width whenever an interactive
        drag leaves it at anything OTHER than exactly that width or
        exactly zero (fully collapsed). Confirmed empirically that
        this doesn't interfere with actually reaching the collapsed
        state: Qt's own drag tracking is driven by the current mouse
        position relative to the handle, not by the pane's own
        previously-reported size, so repeatedly snapping an
        in-between size back to ideal just means the drag has to
        travel a little further before crossing Qt's own internal
        collapse threshold -- it still reaches zero cleanly once it
        does, confirmed by testing the actual drag in small steps.

        The pane whose OWN handle isn't the one this event is for
        (see index, 1 for the left/center handle, 2 for the
        center/right one) is never allowed to change AT ALL here,
        restored to its last confirmed-correct size the instant it
        deviates -- not just corrected toward "ideal or zero" the way
        the dragged pane itself is. Confirmed as a real, reported bug
        otherwise: dragging the RIGHT pane back out from a collapsed
        state could drive the LEFT pane's size to zero as pure
        collateral damage within a single internal Qt drag-tick
        calculation (bottom_bar's own minimum width being temporarily
        inflated by Grid & snap still sitting in it -- see the
        reparenting call below), and since that lands EXACTLY on zero,
        not some obviously-wrong in-between value, the "only correct
        if not already 0" logic used for the pane actually being
        dragged would leave it there as if the user had collapsed it
        on purpose.

        Deliberately still NOT a hard size constraint on either
        widget itself (no setFixedWidth/setMinimumWidth) -- see
        _right_pane_width's own comment for why a genuinely
        fixed-width child breaks the splitter's normal collapse
        behavior (moves the whole widget off-screen at full size
        instead of resizing it down), which is what this reactive
        correction exists specifically to avoid while still achieving
        the same "only ever full width or fully collapsed" result."""
        sizes = self.splitter.sizes()
        if len(sizes) != 3:
            return
        # Grid & snap reparented back out of bottom_bar immediately,
        # BEFORE any size correction below, if this tick is the one
        # where the right pane first becomes non-zero again -- not
        # left for the next _update_collapsed_tabs timer tick.
        # Confirmed as a real, reported bug otherwise: while it's
        # still there, bottom_bar_layout's own minimum width is
        # inflated by its presence, and setSizes has no way to know
        # that -- Qt silently grows the center pane past whatever was
        # actually requested to satisfy that minimum, taking the
        # difference from the left pane. Doing this first, then
        # re-reading sizes(), means the correction below is computed
        # against the sidebar's TRUE minimum width, not the
        # temporarily-inflated one.
        if sizes[2] != 0 and self._grid_box_floating:
            self._float_grid_box(False)
            sizes = self.splitter.sizes()
        changed = False
        if index == 1:
            if sizes[0] != 0 and sizes[0] != self._left_pane_width:
                sizes[1] += sizes[0] - self._left_pane_width
                sizes[0] = self._left_pane_width
                changed = True
            if sizes[2] != self._last_right_size:
                sizes[1] += sizes[2] - self._last_right_size
                sizes[2] = self._last_right_size
                changed = True
        elif index == 2:
            if sizes[2] != 0 and sizes[2] != self._right_pane_width:
                sizes[1] += sizes[2] - self._right_pane_width
                sizes[2] = self._right_pane_width
                changed = True
            if sizes[0] != self._last_left_size:
                sizes[1] += sizes[0] - self._last_left_size
                sizes[0] = self._last_left_size
                changed = True
        if changed:
            self.splitter.setSizes(sizes)
        self._last_left_size = sizes[0]
        self._last_right_size = sizes[2]

    def _restore_collapsed_pane(self, index: int, default_width: int):
        # Grid & snap reparented back into right_layout FIRST, before
        # touching sizes at all, if it's currently floating in
        # bottom_bar -- confirmed as a real, reported bug otherwise:
        # while it's still there, bottom_bar_layout's own minimum
        # width is inflated by its presence (on top of Weight/
        # Flatten's own), and setSizes below has no way to know that,
        # so Qt silently grows the center pane past what was actually
        # requested to satisfy that minimum -- taking the difference
        # from the LEFT pane, which is what made restoring the right
        # pane look like it was collapsing the left one. Normally this
        # reparenting only happens later, on the next _update_
        # collapsed_tabs timer tick, which is far too late for this
        # specific call.
        if index == 2 and self._grid_box_floating:
            self._float_grid_box(False)
        # Takes the space from whichever pane currently has the most
        # to spare (the canvas, in this app's actual layout, found
        # dynamically here rather than assumed by a fixed index) --
        # same restore logic the original QSplitterHandle-based
        # attempt used, just called from ordinary MainWindow code now.
        sizes = self.splitter.sizes()
        donor_index = max(range(len(sizes)), key=lambda i: sizes[i])
        take = min(default_width, max(sizes[donor_index] - 100, 0))
        sizes[index] = take
        sizes[donor_index] -= take
        self.splitter.setSizes(sizes)
        self._last_left_size = sizes[0]
        self._last_right_size = sizes[2]

    def _on_grid_settings_changed(self, *_args):
        spacing = self.grid_spacing_spin.value()
        self.canvas.set_grid(spacing if spacing > 0 else None,
                              self.snap_grid_check.isChecked(),
                              self.snap_points_check.isChecked(),
                              self.snap_xheight_baseline_check.isChecked())

    def _on_family_edited(self):
        if not self.project:
            return
        text = self.family_edit.text().strip()
        # Falls back to whatever it already was rather than accepting
        # an empty name -- same "don't overwrite good data with a
        # blank" convention the old Advanced Settings dialog's own
        # _on_accept used (fs.family_name = edit.text() or
        # fs.family_name).
        self.project.font.family_name = text or self.project.font.family_name
        self.family_edit.setText(self.project.font.family_name)

    def _refresh_project_name_label(self) -> None:
        """This project's own name (whatever it was saved/will be
        saved as, per ProjectNameDialog) and its folder -- see
        project_name_label's own setup comment for why this exists and
        where it sits. Called from every project-lifecycle method that
        changes self.project or _saved_project_path (New Project,
        Import folder, Open project, crash recovery, the first save,
        and Clear project) -- there's no single shared call site all of
        those already pass through the way glyph-level refreshes have
        in _load_glyph_into_canvas, so each calls this explicitly
        rather than it being inferred from some other, unrelated event.

        The name itself is never cut off, even when the combined text
        doesn't fit -- only the folder portion is elided, since the
        name is the short, most-at-a-glance-important identifier and
        the folder path is the one actually likely to run long."""
        if not self.project:
            self.project_name_label.setText("")
            self.project_name_label.setToolTip("")
            return
        if self._saved_project_path:
            name = os.path.basename(self._saved_project_path)
            for suffix in (".pnglyph.json", ".json"):
                if name.lower().endswith(suffix):
                    name = name[:-len(suffix)]
                    break
        else:
            # Not yet saved -- New Project/Import folder haven't gone
            # through ProjectNameDialog yet, so there's no chosen name
            # to show. Distinguished from a real, saved name rather
            # than silently showing a made-up placeholder as if it
            # were one.
            name = "(unsaved)"
        folder = self.project.source_dir
        full_text = f"{name}  \u2014  {folder}"
        metrics = QFontMetrics(self.project_name_label.font())
        max_w = self.project_name_label.maximumWidth()
        if metrics.horizontalAdvance(full_text) <= max_w:
            display_text = full_text
        else:
            prefix = f"{name}  \u2014  "
            remaining_w = max_w - metrics.horizontalAdvance(prefix)
            elided_folder = metrics.elidedText(folder, Qt.ElideMiddle, max(remaining_w, 20))
            display_text = prefix + elided_folder
        self.project_name_label.setText(display_text)
        self.project_name_label.setToolTip(full_text)

    def _set_status_message(self, text: str) -> None:
        """Every non-glyph-info status update goes through here now,
        replacing self.statusBar().showMessage(...) everywhere it used
        to be called directly -- see status_message_label's own setup
        comment for why. No timeout parameter: confirmed as the wanted
        behavior over any auto-clearing timer -- this stays exactly as
        set until either another message replaces it, or the user loads
        a different glyph (see _load_glyph_into_canvas's own clear)."""
        self.status_message_label.setText(text)

    def _refresh_metrics_label(self):
        if not self.project:
            return
        # Populated here too (not just at project-load time) so it
        # stays correct if the project reference itself changes without
        # going through a dedicated "load" path -- cheap to just
        # re-set on every refresh, and avoids a second place that could
        # drift out of sync with the first.
        if not self.family_edit.hasFocus():
            self.family_edit.setText(self.project.font.family_name)
        # MetricsDiagram now owns this computation itself (it needs the
        # same logic for its own edit handlers, to stay consistent
        # whether a refresh comes from an external change like a guide
        # move, or from the widget's own EM/override editing) --
        # set_project keeps it pointed at the current project (a no-op
        # if unchanged), _resync_from_project is the single shared path
        # both external and internal updates go through.
        self.metrics_diagram.set_project(self.project)
        self.metrics_diagram._resync_from_project()
        # The actual canvas pixel dimensions -- shown here, once, as
        # the GroupBox's own title, rather than trying to fit them
        # into the drawing itself (which has no natural place for
        # them) or relying on Advance to stand in for canvas width,
        # which only coincidentally matches it and only until the
        # advance guide is ever moved.
        canvas_size = self.project.metrics.canvas_size
        if canvas_size:
            self.metrics_box.setTitle(f"Canvas ({canvas_size[0]} x {canvas_size[1]})")
        else:
            self.metrics_box.setTitle("Canvas")
