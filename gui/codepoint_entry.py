"""
A shared codepoint-entry widget used everywhere a codepoint gets typed
in: the capture assignment window's per-row fields, AddGlyphDialog's
character field, and the "Reassign codepoint..." dialog. Consolidating
these means all three get the same speed features and the same right-
click picker, rather than three separately-maintained entry points.

Two speed features on top of plain typing:

  - A single printable character typed into an EMPTY field commits
    immediately and emits commitReady() -- callers with a natural
    "next field" (the assignment window's rows) can connect to this to
    auto-advance focus; callers with no such target (AddGlyphDialog,
    Reassign codepoint) simply don't connect to it, since their own
    live validation already reacts to every keystroke regardless.
  - Ctrl+U inserts "U+" and switches into hex-entry mode: subsequent
    hex digits (0-9, A-F) are collected, committing (and emitting
    commitReady()) on the 4th digit or on Enter/Return with fewer.
    Deliberately NOT triggered by typing "+" or a doubled letter --
    both of those either collide with assigning that literal character
    itself or reintroduce a timing guess about what's coming next;
    Ctrl+U isn't a printable character at all, so it takes nothing away
    from the plain single-keystroke path above, including "+" itself.

Right-click opens the block/search Unicode picker directly (no
intervening menu -- see CodepointLineEdit.contextMenuEvent).

The picker itself is UnicodePickerWidget -- embedded permanently in the
Capture window's side panel, and wrapped by UnicodePickerDialog for the
right-click pop-up everywhere else. Both draw with the bundled DejaVu
Sans Mono (gui/fonts/) rather than whatever system fonts Qt happens to
fall back to: the font only changes how a cell LOOKS -- the list of
codepoints comes from Python's Unicode database either way -- but one
consistent font means no mixed styles and no empty boxes.
"""
from __future__ import annotations

import unicodedata

import os

from PySide6.QtCore import Qt, Signal, QSize, QPoint
from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics, QColor, QBrush
from PySide6.QtWidgets import (
    QLineEdit, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QDialogButtonBox, QAbstractItemView, QListView,
    QWidget, QSlider, QCheckBox,
)

from core.codepages import cp437_display_set
from .capture.codepoint_parse import parse_codepoint
from . import app_settings

_HEX_DIGITS = set("0123456789ABCDEF")


class CodepointLineEdit(QLineEdit):
    commitReady = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hex_mode = False
        # Ctrl+U named right in the hint -- typing a literal "U" can't
        # start hex entry, since a single character in an empty field
        # commits immediately (that's the fast path), so the shortcut
        # has to be discoverable without reading the docs.
        self.setPlaceholderText("Character, or Ctrl+U for U+XXXX")
        self.setToolTip(
            "Type a single character to assign it directly,\n"
            "or press Ctrl+U and type the hex code (U+XXXX).\n"
            "Right-click to pick from the character table.")

    def keyPressEvent(self, event):
        key = event.key()
        text = event.text()

        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            super().keyPressEvent(event)
            return

        if key == Qt.Key_U and (event.modifiers() & Qt.ControlModifier):
            self.setText("U+")
            self._hex_mode = True
            self.end(False)
            return

        if self._hex_mode:
            if key in (Qt.Key_Backspace, Qt.Key_Delete, Qt.Key_Left, Qt.Key_Right,
                       Qt.Key_Home, Qt.Key_End):
                super().keyPressEvent(event)
                if len(self.text()) <= 2:   # backspaced back past "U+" itself
                    self._hex_mode = False
                return
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if len(self.text()) > 2:   # at least one hex digit typed
                    self._hex_mode = False
                    self.commitReady.emit()
                return
            if len(text) == 1 and text.upper() in _HEX_DIGITS:
                super().keyPressEvent(event)
                if len(self.text()) - 2 >= 4:   # "U+" + 4 hex digits
                    self._hex_mode = False
                    self.commitReady.emit()
                return
            # Any other key while collecting hex digits is ignored --
            # letting it through could corrupt the U+XXXX entry with
            # something parse_codepoint would just reject anyway.
            return

        if not self.text() and len(text) == 1 and text.isprintable():
            super().keyPressEvent(event)
            self.commitReady.emit()
            return

        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        # No standard Undo/Redo/Cut/Copy/Paste/Select-All menu -- none
        # of that is useful for a single-line codepoint field, and it
        # was just an extra click in front of the one action that
        # actually matters here. Right-click goes straight to the
        # picker.
        cp = UnicodePickerDialog.pick(self)
        if cp is not None:
            self._hex_mode = False
            self.setText(f"U+{cp:04X}")
            self.commitReady.emit()


# -- picker font ---------------------------------------------------------

_FONT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "DejaVuSansMono.ttf")
_font_family: str | None = None


def picker_font_family() -> str:
    """Registers the bundled DejaVu Sans Mono with Qt once (per process)
    and returns its family name. Chosen because it covers every CP437
    character (checked against the font's own cmap: all 255 glyphs
    present), box drawing, block elements, arrows, Greek and Cyrillic,
    in ~330 KB. Falls back to the default UI font if the file is
    missing or can't be loaded -- the picker still works, just with
    system-font fallback as before."""
    global _font_family
    if _font_family is None:
        family = ""
        if os.path.exists(_FONT_FILE):
            font_id = QFontDatabase.addApplicationFont(_FONT_FILE)
            if font_id >= 0:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    family = families[0]
        _font_family = family or QFont().family()
    return _font_family


# Ordered sections of the browse view. Curated, not exhaustive -- what
# this project's source material actually uses: the full CP437 set
# first (the DOS-program case -- every character it can display,
# in byte order, so the section reads like the original code chart),
# then the Unicode blocks those characters come from, plus the common
# Latin/Greek/Cyrillic/symbol blocks around them.
_BLOCKS: list[tuple[str, list[int] | range]] = [
    ("CP437 (DOS screen characters, in byte order)", cp437_display_set()),
    ("Basic Latin", range(0x21, 0x7F)),
    ("Latin-1 Supplement", range(0xA1, 0x100)),
    ("Latin Extended-A", range(0x100, 0x180)),
    ("Greek and Coptic", range(0x370, 0x400)),
    ("Cyrillic", range(0x400, 0x460)),
    ("General Punctuation", range(0x2010, 0x2060)),
    ("Superscripts and Subscripts", range(0x2070, 0x20A0)),
    ("Currency Symbols", range(0x20A0, 0x20C1)),
    ("Letterlike Symbols", range(0x2100, 0x2150)),
    ("Number Forms", range(0x2150, 0x2190)),
    ("Arrows", range(0x2190, 0x2200)),
    ("Mathematical Operators", range(0x2200, 0x2300)),
    ("Miscellaneous Technical", range(0x2300, 0x2400)),
    ("Box Drawing", range(0x2500, 0x2580)),
    ("Block Elements", range(0x2580, 0x25A0)),
    ("Geometric Shapes", range(0x25A0, 0x2600)),
    ("Miscellaneous Symbols", range(0x2600, 0x2700)),
]

# What a name search sweeps: every block above (deduplicated) plus
# Latin Extended-B. Deliberately NOT one contiguous span from U+0021
# upward any more -- that swept right-to-left and complex-shaping
# scripts too (Syriac, N'Ko, Thaana...), which never belong in this
# kind of font and were what made Qt print "OpenType support missing
# for <font>, script 9/66" whenever a search happened to list them.
_SEARCH_BLOCKS: list[int] = sorted(
    {cp for _name, rng in _BLOCKS for cp in rng} | set(range(0x180, 0x250)))

# Character colour for codepoints already used (in the project, or
# assigned elsewhere in the current capture session) -- see set_marked.
# A text colour rather than a cell background: the application
# stylesheet's QListWidget::item rules make Qt ignore per-item
# backgrounds, but per-item text colours still apply.
_MARKED_FG = QColor("#c98a00")


def _cell_size_for(point_size: int) -> int:
    # Proportional to the font size, with a little padding -- matches
    # the old fixed 40px cell at the old fixed 16pt.
    return round(point_size * 2.3) + 4


class UnicodePickerWidget(QWidget):
    """Search field + one continuous character grid + size slider and
    "only characters in this font" filter, with the current section
    name and a detail line underneath.

    codepointClicked fires on a single click, codepointActivated on
    double-click / Enter. The Capture window's side panel assigns on a
    single click (fast: one click per capture); the pop-up dialog uses
    activation (or OK) so a stray click doesn't close it."""

    codepointClicked = Signal(int)
    codepointActivated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        # (block_name, start_index, end_index) inclusive, in the SAME
        # order items are added to the grid -- rebuilt on every
        # _populate call (including a search re-filtering it), used to
        # look up which block the item currently at the top of the
        # viewport belongs to (see _update_group_label).
        self._block_ranges: list[tuple[str, int, int]] = []
        self._marked: set[int] = set()
        self._family = picker_font_family()
        self._point_size = app_settings.get_picker_point_size()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._search = QLineEdit()
        self._search.setPlaceholderText('Search by name (e.g. "greek", "box", "arrow")\u2026')
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._repopulate)
        layout.addWidget(self._search)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Size"))
        self._size_slider = QSlider(Qt.Horizontal)
        self._size_slider.setRange(app_settings.PICKER_PT_MIN, app_settings.PICKER_PT_MAX)
        self._size_slider.setValue(self._point_size)
        self._size_slider.setToolTip("Character size in the grid (remembered)")
        self._size_slider.valueChanged.connect(self._on_size_changed)
        controls.addWidget(self._size_slider, 1)
        self._in_font_check = QCheckBox("Only characters in this font")
        self._in_font_check.setChecked(app_settings.get_picker_only_in_font())
        self._in_font_check.setToolTip(
            f"Hide characters the picker font ({self._family}) has no glyph for,\n"
            "instead of drawing them in some other, fallback font.")
        self._in_font_check.toggled.connect(self._on_in_font_toggled)
        controls.addWidget(self._in_font_check)
        layout.addLayout(controls)

        # A single continuous grid across every block, bare glyphs only
        # -- no per-block tabs, no code/name text in each cell; name and
        # codepoint appear in the detail line below (on click) and as
        # each cell's tooltip.
        self._grid = QListWidget()
        self._grid.setViewMode(QListWidget.IconMode)
        self._grid.setFlow(QListView.LeftToRight)
        self._grid.setWrapping(True)
        self._grid.setResizeMode(QListWidget.Adjust)
        self._grid.setMovement(QListView.Static)
        self._grid.setUniformItemSizes(True)
        self._grid.setSpacing(2)
        self._grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self._grid.itemClicked.connect(self._on_item_clicked)
        self._grid.itemActivated.connect(self._on_item_activated)
        self._grid.verticalScrollBar().valueChanged.connect(self._update_group_label)
        layout.addWidget(self._grid, 1)

        # Both labels sit BELOW the grid: the group name updates live
        # while scrolling, the detail line on a single click.
        self._group_label = QLabel("")
        self._group_label.setStyleSheet("color: #777777; font-size: 11px;")
        layout.addWidget(self._group_label)

        self._detail_label = QLabel("")
        layout.addWidget(self._detail_label)

        self._apply_font()
        self._repopulate()

    # -- public ------------------------------------------------------------

    def current_codepoint(self) -> int | None:
        item = self._grid.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def set_marked(self, cps: set[int]) -> None:
        """Colours the characters that are already in use (amber) -- so
        "already assigned" is visible BEFORE clicking, not only as a
        warning afterwards. Purely visual; nothing is disabled."""
        cps = set(cps)
        if cps == self._marked:
            return
        self._marked = cps
        for i in range(self._grid.count()):
            self._style_item(self._grid.item(i))

    def focus_search(self) -> None:
        self._search.setFocus()
        self._search.selectAll()

    # -- internals ---------------------------------------------------------

    def _apply_font(self) -> None:
        font = QFont(self._family)
        font.setPointSize(self._point_size)
        self._grid.setFont(font)
        # setFont() alone is NOT enough: the application stylesheet's
        # "QWidget { font-size: 9pt }" rule wins over it, which is why
        # the old picker always drew its characters at 9pt despite
        # asking for 16pt. A widget-level stylesheet takes precedence
        # over the application one for this grid only.
        self._grid.setStyleSheet(
            f"QListWidget {{ font-family: '{self._family}'; font-size: {self._point_size}pt; }}")
        self._metrics = QFontMetrics(font)
        cell = _cell_size_for(self._point_size)
        self._grid.setGridSize(QSize(cell, cell))

    def _on_size_changed(self, pt: int) -> None:
        self._point_size = pt
        app_settings.remember_picker_point_size(pt)
        self._apply_font()
        cell = _cell_size_for(pt)
        for i in range(self._grid.count()):
            self._grid.item(i).setSizeHint(QSize(cell, cell))
        self._update_group_label()

    def _on_in_font_toggled(self, on: bool) -> None:
        app_settings.remember_picker_only_in_font(on)
        self._repopulate()

    def _include(self, cp: int) -> bool:
        if self._in_font_check.isChecked() and not self._metrics.inFontUcs4(cp):
            return False
        return True

    def _repopulate(self, *_args) -> None:
        text = self._search.text().strip().lower()
        self._detail_label.setText("")
        self._grid.clear()
        self._block_ranges = []
        if not text:
            for name, rng in _BLOCKS:
                start = self._grid.count()
                for cp in rng:
                    self._add_item(cp)
                end = self._grid.count() - 1
                if end >= start:
                    self._block_ranges.append((name, start, end))
        else:
            for cp in _SEARCH_BLOCKS:
                name = unicodedata.name(chr(cp), "")
                if name and text in name.lower():
                    self._add_item(cp)
            # One pseudo-block covering the whole filtered result -- the
            # group label keeps working the same way.
            if self._grid.count():
                self._block_ranges = [("Search results", 0, self._grid.count() - 1)]
        self._update_group_label()

    def _add_item(self, cp: int) -> None:
        try:
            ch = chr(cp)
            name = unicodedata.name(ch, "")
        except ValueError:
            return
        if not name or not self._include(cp):
            return
        item = QListWidgetItem(ch)
        item.setTextAlignment(Qt.AlignCenter)
        item.setData(Qt.UserRole, cp)
        item.setData(Qt.UserRole + 1, name.title())
        cell = _cell_size_for(self._point_size)
        item.setSizeHint(QSize(cell, cell))
        self._style_item(item)
        self._grid.addItem(item)

    def _style_item(self, item: QListWidgetItem) -> None:
        cp = item.data(Qt.UserRole)
        name = item.data(Qt.UserRole + 1)
        used = cp in self._marked
        item.setForeground(QBrush(_MARKED_FG) if used else QBrush())
        item.setToolTip(f"U+{cp:04X}  {name}" + ("\n(already in use)" if used else ""))

    def _update_group_label(self, *_args) -> None:
        """Called on scroll (and after populating) -- finds whichever
        item currently renders at the top of the viewport and looks up
        which block's recorded index range contains it."""
        if not self._block_ranges:
            self._group_label.setText("" if self._grid.count() else "No matching characters")
            return
        item = self._grid.itemAt(QPoint(2, 2))
        if item is None:
            item = self._grid.item(0)
        idx = self._grid.row(item) if item is not None else -1
        for name, start, end in self._block_ranges:
            if start <= idx <= end:
                self._group_label.setText(name)
                return
        self._group_label.setText("")

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        cp = item.data(Qt.UserRole)
        name = item.data(Qt.UserRole + 1)
        used = "   (already in use)" if cp in self._marked else ""
        self._detail_label.setText(f"U+{cp:04X}   {name}{used}")
        self.codepointClicked.emit(cp)

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        self.codepointActivated.emit(item.data(Qt.UserRole))


class UnicodePickerDialog(QDialog):
    """The right-click pop-up: UnicodePickerWidget plus OK/Cancel.
    Double-click (or Enter) on a cell accepts directly."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick a character")
        self.resize(460, 520)
        self.selected_codepoint: int | None = None

        layout = QVBoxLayout(self)
        self._picker = UnicodePickerWidget(self)
        self._picker.codepointActivated.connect(self._accept_cp)
        layout.addWidget(self._picker, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._picker.focus_search()

    def _accept_cp(self, cp: int) -> None:
        self.selected_codepoint = cp
        self.accept()

    def _on_accept(self) -> None:
        cp = self._picker.current_codepoint()
        if cp is not None:
            self._accept_cp(cp)

    @staticmethod
    def pick(parent=None) -> int | None:
        dlg = UnicodePickerDialog(parent)
        if dlg.exec() == QDialog.Accepted:
            return dlg.selected_codepoint
        return None
