"""
A flat, modern QSS stylesheet -- the earlier HTML/CSS mockup was a
different rendering technology (browser) from what actually got built
(bare, unstyled Qt widgets), which is why the two didn't match. This
brings the real app closer to that visual language: flat panels,
hairline borders, soft accent colors, no default OS chrome.
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QPainter, QColor, QPolygon, QPen
from PySide6.QtCore import QPoint

_ICON_DIR = os.path.join(os.path.dirname(__file__), "_generated_icons")


def _ensure_arrow_icons() -> dict[str, str]:
    """Generate small up/down/dropdown arrow PNGs, plus the checkbox
    checkmark, once and return their absolute paths. Done as real image
    files rather than a QSS border-triangle trick -- that trick
    silently fails to render (in at least this Qt/style combination)
    once a parent subcontrol like ::up-button or ::indicator gets
    custom rules, leaving the shape blank instead of just
    unstyled-but-visible. The checked checkbox state previously showed
    only a solid color fill with no checkmark glyph at all -- the same
    underlying gap as the arrows, just not yet fixed for this one."""
    os.makedirs(_ICON_DIR, exist_ok=True)
    paths = {}
    specs = {
        "up": [(1, 7), (5, 1), (9, 7)],
        "down": [(1, 1), (5, 7), (9, 1)],
    }
    for name, pts in specs.items():
        path = os.path.join(_ICON_DIR, f"arrow_{name}.png")
        paths[name] = path.replace("\\", "/")
        if os.path.exists(path):
            continue
        pix = QPixmap(10, 8)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#555555"))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(QPolygon([QPoint(x, y) for x, y in pts]))
        painter.end()
        pix.save(path)

    check_path = os.path.join(_ICON_DIR, "checkmark.png")
    paths["check"] = check_path.replace("\\", "/")
    if not os.path.exists(check_path):
        pix = QPixmap(12, 12)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#ffffff"), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(QPolygon([QPoint(2, 6), QPoint(5, 9), QPoint(10, 3)]))
        painter.end()
        pix.save(check_path)

    return paths


def get_stylesheet() -> str:
    icons = _ensure_arrow_icons()
    return (_STYLESHEET_TEMPLATE
            .replace("__UP_ARROW__", icons["up"])
            .replace("__DOWN_ARROW__", icons["down"])
            .replace("__CHECKMARK__", icons["check"]))


_STYLESHEET_TEMPLATE = """
QMainWindow, QWidget {
    background-color: #ffffff;
    color: #222222;
    font-size: 9pt;
}

QToolBar {
    background-color: #f3f5f9;
    border: none;
    border-bottom: 1px solid #dde1e8;
    padding: 4px;
    spacing: 2px;
}

QToolBar QToolButton {
    background: #eef1f6;
    border: 1px solid #d8dee8;
    border-radius: 6px;
    padding: 6px 10px;
    color: #222222;
    margin: 2px;
}

QToolBar QToolButton:hover {
    background-color: #e4e9f2;
    border-color: #b8c4d8;
}

QToolBar QToolButton:pressed {
    background-color: #3a6ff0;
    border-color: #3a6ff0;
    color: white;
}

QToolBar QToolButton:checked {
    background-color: #3a6ff0;
    border-color: #3a6ff0;
    color: white;
}

QToolBar QToolButton:checked:hover {
    background-color: #2f5fd0;
}

QToolBar QToolButton:disabled {
    color: #bbbbbb;
    background: #f7f8fa;
    border-color: #e8ecf2;
}

QToolBar::separator {
    background: #dde1e8;
    width: 1px;
    margin: 6px 4px;
}

QListWidget {
    background-color: #ffffff;
    border: 1px solid #dde1e8;
    border-radius: 8px;
    outline: none;
}

QListWidget::item {
    border-radius: 6px;
    padding: 4px;
    margin: 1px;
}

QListWidget::item:selected {
    background-color: #3a6ff0;
    color: white;
}

QListWidget::item:hover:!selected {
    background-color: #eef1f6;
}

QMenu {
    background-color: #ffffff;
    border: 1px solid #dde1e8;
    border-radius: 8px;
    padding: 4px;
}

QMenu::item {
    background-color: transparent;
    color: #222222;
    padding: 5px 24px 5px 12px;
    border-radius: 6px;
    margin: 1px;
}

QMenu::item:selected {
    background-color: #3a6ff0;
    color: white;
}

QMenu::item:disabled {
    color: #bbbbbb;
}

QMenu::separator {
    height: 1px;
    background-color: #dde1e8;
    margin: 4px 8px;
}

QTabBar::tab {
    padding: 6px 12px;
    /* Enough vertical room for a full descender (the 'g' in
       "Multi-weight"/"Coverage" was getting its tail clipped by Qt's
       default tab sizing) -- min-height, not a fixed height, so the
       tab still grows if a larger font is ever used. */
    min-height: 20px;
    background-color: #f3f5f9;
    border: 1px solid #dde1e8;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
}

QTabBar::tab:selected {
    background-color: #ffffff;
    color: #222222;
}

QTabBar::tab:!selected {
    color: #666666;
}

QTabWidget::pane {
    border: 1px solid #dde1e8;
    border-radius: 6px;
    top: -1px;
}

QGroupBox {
    border: 1px solid #dde1e8;
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 8px;
    font-weight: 500;
    color: #555555;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #555555;
}

QSpinBox#valuePill {
    background-color: #eef1f6;
    border: 1px solid #d8dee8;
    border-radius: 10px;
    padding: 1px 6px;
    min-width: 44px;
    color: #222222;
    font-weight: 500;
}

QSpinBox#valuePill:disabled {
    background-color: #f3f5f9;
    color: #a8adb8;
    border-color: #e5e8ee;
}

QPushButton#stepButton {
    padding: 2px;
    min-width: 20px;
    max-width: 20px;
    font-weight: 500;
}

QPushButton {
    background-color: #eef1f6;
    border: 1px solid #d8dee8;
    border-radius: 6px;
    padding: 6px 12px;
    color: #222222;
}

QPushButton:hover {
    background-color: #e4e9f2;
}

QPushButton:checked {
    background-color: #3a6ff0;
    border-color: #3a6ff0;
    color: white;
}

QPushButton:disabled {
    color: #bbbbbb;
}

QLineEdit, QComboBox, QDoubleSpinBox {
    background-color: #ffffff;
    border: 1px solid #c8d0dc;
    border-radius: 6px;
    padding: 4px 6px;
    color: #222222;
}

QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {
    border-color: #3a6ff0;
}

/* -- QComboBox drop-down button -- */
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 20px;
    border-left: 1px solid #c8d0dc;
    background-color: #eef1f6;
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
}

QComboBox::down-arrow {
    image: url(__DOWN_ARROW__);
    width: 10px;
    height: 8px;
}

QComboBox QAbstractItemView {
    background-color: #ffffff;
    border: 1px solid #c8d0dc;
    selection-background-color: #3a6ff0;
    selection-color: white;
    outline: none;
}

/* -- QDoubleSpinBox up/down buttons: explicit geometry so the visible
   button area and the clickable hit area actually match -- a mismatch
   here (e.g. from border-radius clipping without matching subcontrol
   rules) is what made only part of the button respond to clicks. -- */
QDoubleSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    height: 12px;
    border-left: 1px solid #c8d0dc;
    border-bottom: 1px solid #c8d0dc;
    background-color: #eef1f6;
    border-top-right-radius: 6px;
}

QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    height: 12px;
    border-left: 1px solid #c8d0dc;
    background-color: #eef1f6;
    border-bottom-right-radius: 6px;
}

QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: #e4e9f2;
}

QDoubleSpinBox::up-arrow {
    image: url(__UP_ARROW__);
    width: 10px;
    height: 8px;
}

QDoubleSpinBox::down-arrow {
    image: url(__DOWN_ARROW__);
    width: 10px;
    height: 8px;
}

QSlider::groove:horizontal {
    height: 4px;
    background: #dde1e8;
    border-radius: 2px;
}

QSlider::handle:horizontal {
    background: #3a6ff0;
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}

QLabel {
    color: #444444;
}

QLabel:disabled {
    color: #aaaaaa;
}

QStatusBar {
    background-color: #f3f5f9;
    border-top: 1px solid #dde1e8;
    color: #666666;
}

QCheckBox {
    color: #444444;
    spacing: 6px;
}

QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid #c8d0dc;
    border-radius: 4px;
    background-color: #ffffff;
}

QCheckBox::indicator:checked {
    background-color: #3a6ff0;
    border-color: #3a6ff0;
    image: url(__CHECKMARK__);
}

QGraphicsView {
    border: none;
    background-color: #f7f7f8;
}

/* -- scrollbars: default OS chrome (thin blue-gray rail + slate handle)
   instead of falling back to unstyled native widgets that vanish
   against a white panel -- */
QScrollBar:vertical {
    background: #f3f5f9;
    width: 12px;
    border-radius: 6px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: #c8d0dc;
    border-radius: 6px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover {
    background: #aab4c4;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
}

QScrollBar:horizontal {
    background: #f3f5f9;
    height: 12px;
    border-radius: 6px;
    margin: 0;
}

QScrollBar::handle:horizontal {
    background: #c8d0dc;
    border-radius: 6px;
    min-width: 24px;
}

QScrollBar::handle:horizontal:hover {
    background: #aab4c4;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}

QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: none;
}

QSplitter::handle {
    background-color: #f0f2f6;
}

QSplitter::handle:hover {
    background-color: #dde3ee;
}
"""

# Canvas-specific colors (not QSS -- these are QColor values used directly
# by canvas_view.py, kept alongside the stylesheet so the two stay in sync
# when switching themes).
CANVAS_BG = "#f7f7f8"
INK_COLOR = "#2a2a2e"
GUIDE_COLOR = "#5b8def"
BASELINE_COLOR = "#999999"
BOX_COLOR = "#bbbbbb"
POINT_COLOR = "#e0662f"
POINT_SELECTED_COLOR = "#1f7a3d"
SKELETON_COLOR = "#c9c9cc"
HIGHLIGHT_SEGMENT_COLOR = "#1f7a3d"
GRID_COLOR = "#e6e6e8"
# A deliberately different hue from both POINT_COLOR (idle, orange) and
# HIGHLIGHT_SEGMENT_COLOR (a real, committed selection, green) -- the
# disambiguation popup's hover preview (see GlyphCanvas.set_preview_tint)
# is neither of those: nothing is actually selected yet, so it needs its
# own, unmistakably distinct color rather than borrowing one that already
# means something else elsewhere in the app.
POPUP_PREVIEW_COLOR = "#ff0090"
MARGIN_FILL = "#eef0f4"   # shaded zone around the box, sized to the max
                          # weight's round-cap overshoot, so ink that
                          # legitimately extends past the box (rather
                          # than clipping) has visibly-reserved room
EDITED_MARKER_COLOR = (210, 215, 222, 255)   # light gray-blue, RGBA
