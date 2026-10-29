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
"""
from __future__ import annotations

import unicodedata

from PySide6.QtCore import Qt, Signal, QSize, QPoint
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QLineEdit, QDialog, QVBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QDialogButtonBox, QAbstractItemView, QListView,
)

from .capture.codepoint_parse import parse_codepoint

_HEX_DIGITS = set("0123456789ABCDEF")


class CodepointLineEdit(QLineEdit):
    commitReady = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hex_mode = False
        self.setPlaceholderText("Character or U+XXXX")

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


# Curated, not exhaustive -- broad enough to cover what this project's
# own source material actually uses (CP437/CP850 box-drawing and Greek
# letters, per the Ultiboard font work; general symbols and punctuation
# beyond plain ASCII), without trying to be a complete Unicode browser.
_BLOCKS: list[tuple[str, range]] = [
    ("Basic Latin", range(0x21, 0x7F)),
    ("Latin-1 Supplement", range(0xA1, 0x100)),
    ("Greek and Coptic", range(0x370, 0x400)),
    ("General Punctuation", range(0x2010, 0x2050)),
    ("Box Drawing", range(0x2500, 0x2580)),
    ("Block Elements", range(0x2580, 0x25A0)),
    ("Miscellaneous Symbols", range(0x2600, 0x2640)),
]

# Search sweeps this whole span (a superset of every block above) so a
# name search finds things not called out as their own tab -- still
# small enough that a per-keystroke re-scan stays well under human-
# noticeable latency.
_SEARCH_RANGE = range(0x21, 0x2600)


class UnicodePickerDialog(QDialog):
    _CELL_SIZE = 40

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick a character")
        self.resize(420, 480)
        self.selected_codepoint: int | None = None
        # (block_name, start_index, end_index) inclusive, in the SAME
        # order items are added to the grid -- rebuilt on every
        # _populate call (including a search re-filtering it), used to
        # look up which block the item currently at the top of the
        # viewport belongs to (see _update_group_label).
        self._block_ranges: list[tuple[str, int, int]] = []

        layout = QVBoxLayout(self)

        self._search = QLineEdit()
        self._search.setPlaceholderText('Search by name (e.g. "greek", "box", "degree")\u2026')
        self._search.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search)

        # A single continuous grid across every block, bare glyphs only
        # -- no per-block tabs, no code/name text in each cell (kept
        # deliberately sparse since there are few enough blank-looking
        # characters in these blocks that a bare glyph is fine; name
        # and codepoint only ever appear in the detail line below, on
        # click, not repeated in every cell).
        self._grid = QListWidget()
        self._grid.setViewMode(QListWidget.IconMode)
        self._grid.setFlow(QListView.LeftToRight)
        self._grid.setWrapping(True)
        self._grid.setResizeMode(QListWidget.Adjust)
        self._grid.setMovement(QListView.Static)
        self._grid.setUniformItemSizes(True)
        self._grid.setSpacing(2)
        self._grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self._grid.setGridSize(QSize(self._CELL_SIZE, self._CELL_SIZE))
        font = QFont()
        font.setPointSize(16)
        self._grid.setFont(font)
        self._grid.itemClicked.connect(self._on_item_clicked)
        self._grid.itemDoubleClicked.connect(lambda _item: self._on_accept())
        self._grid.verticalScrollBar().valueChanged.connect(self._update_group_label)
        layout.addWidget(self._grid, 1)

        # Both labels sit BELOW the grid: the group name updates live
        # while scrolling (replacing the old per-block tabs), the
        # detail line updates on a single click without closing the
        # dialog.
        self._group_label = QLabel("")
        self._group_label.setStyleSheet("color: #777777; font-size: 11px;")
        layout.addWidget(self._group_label)

        self._detail_label = QLabel("")
        layout.addWidget(self._detail_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate_blocks()

    def _populate_blocks(self) -> None:
        """The default, non-search view -- every curated block, in
        order, concatenated into the one grid."""
        self._grid.clear()
        self._block_ranges = []
        for name, rng in _BLOCKS:
            start = self._grid.count()
            for cp in rng:
                self._add_item(cp)
            end = self._grid.count() - 1
            if end >= start:
                self._block_ranges.append((name, start, end))
        self._update_group_label()

    def _add_item(self, cp: int) -> None:
        try:
            ch = chr(cp)
            name = unicodedata.name(ch, "")
        except ValueError:
            return
        if not name:
            return
        item = QListWidgetItem(ch)
        item.setTextAlignment(Qt.AlignCenter)
        item.setData(Qt.UserRole, cp)
        item.setData(Qt.UserRole + 1, name.title())
        item.setSizeHint(QSize(self._CELL_SIZE, self._CELL_SIZE))
        self._grid.addItem(item)

    def _update_group_label(self, *_args) -> None:
        """Called on scroll (and once at populate time) -- finds
        whichever item currently renders at the top of the viewport and
        looks up which block's recorded index range contains it,
        replacing the old static per-block tabs with a label that
        tracks scroll position live."""
        if not self._block_ranges:
            self._group_label.setText("")
            return
        item = self._grid.itemAt(QPoint(2, 2))
        if item is None:
            # Nothing rendered at the very top-left corner (can happen
            # transitionally, e.g. right after a resize) -- fall back
            # to the first item's own block rather than showing nothing.
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
        self._detail_label.setText(f"U+{cp:04X}   {name}")

    def _on_search_changed(self, text: str) -> None:
        text = text.strip().lower()
        self._detail_label.setText("")
        if not text:
            self._populate_blocks()
            return
        self._grid.clear()
        for cp in _SEARCH_RANGE:
            try:
                name = unicodedata.name(chr(cp), "")
            except ValueError:
                continue
            if name and text in name.lower():
                self._add_item(cp)
        # One pseudo-block covering the whole filtered result -- the
        # group label still works the same way (and will just always
        # say "Search results" for as long as a search is active),
        # rather than needing a special case to disable it.
        self._block_ranges = [("Search results", 0, self._grid.count() - 1)] if self._grid.count() else []
        self._update_group_label()

    def _on_accept(self) -> None:
        item = self._grid.currentItem()
        if item is None:
            return
        self.selected_codepoint = item.data(Qt.UserRole)
        self.accept()

    @staticmethod
    def pick(parent=None) -> int | None:
        dlg = UnicodePickerDialog(parent)
        if dlg.exec() == QDialog.Accepted:
            return dlg.selected_codepoint
        return None
