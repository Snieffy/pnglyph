"""
"Assign from text file…" for the Capture window.

The user writes a plain text file holding the source application's own
characters in the SAME ORDER the glyphs were captured (e.g. typed from
a DOS program's CP437 text). Opening it asks which codepage the
file uses, decodes it (core.codepages), and shows a preview -- each
capture's thumbnail beside the character it would get -- before anything
is applied. Nothing is written into the project here either: applying
only fills in the capture rows' codes, exactly as if they'd been typed,
and Commit works as usual afterwards.

Rules, kept deliberately simple (and the same as typing them in):
  - Spaces, tabs and line breaks in the file are separators, never
    counted, so the file can be laid out freely.
  - The n-th counted character goes to the n-th capture (or the n-th
    capture still without a code, in "only unassigned" mode).
  - A character that can't become a glyph (control code, undefined
    byte) still takes its slot -- it doesn't shift the rest -- and that
    capture is left without a code.
  - A code already used by another capture in this session is not
    assigned twice (same rule the window enforces when typing).
  - A code already in the project is only assigned when "include codes
    already in the project" is ticked (they overwrite on commit) --
    defaulting to the window's own "Overwrite existing glyphs without
    asking" setting.
"""
from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QColor, QBrush
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QRadioButton,
    QCheckBox, QTableWidget, QTableWidgetItem, QDialogButtonBox,
    QAbstractItemView, QHeaderView, QButtonGroup,
)

from core.codepages import CODEPAGES, decode_assignment_file, guess_codepage
from .thumbnails import readable_thumbnail_tile

_THUMB = 40
_WARN = QColor("#b8860b")
_BAD = QColor("#b23333")
_DIM = QColor("#888888")


@dataclass
class CaptureRowInfo:
    seq: int
    thumb_path: str
    codepoint: int | None


@dataclass
class _PlanRow:
    info: CaptureRowInfo
    file_text: str           # what the file had for this capture ("" = nothing)
    new_cp: int | None       # code it will get (None = none)
    applies: bool            # False = this row is left exactly as it is
    note: str
    note_color: QColor | None


def _char_name(cp: int) -> str:
    try:
        return unicodedata.name(chr(cp), "").title()
    except ValueError:
        return ""


def build_plan(rows: list[CaptureRowInfo], data: bytes, codepage: str,
               only_unassigned: bool, include_existing: bool,
               existing_cps: set[int]) -> tuple[list[_PlanRow], dict]:
    """Pure function (no widgets) -- returns the per-row plan plus a
    small summary dict, so the preview and the final apply can never
    disagree about what will happen."""
    items = decode_assignment_file(data, codepage)
    targets = [r for r in rows if r.codepoint is None] if only_unassigned else list(rows)
    n = min(len(targets), len(items))
    covered = {r.seq for r in targets[:n]}
    # Codes held by captures this file does NOT touch stay reserved, so
    # the file can't assign them a second time.
    taken: set[int] = {r.codepoint for r in rows
                       if r.seq not in covered and r.codepoint is not None}

    plan: list[_PlanRow] = []
    item_iter = iter(items[:n])
    applied = problems = 0
    for r in rows:
        if r.seq not in covered:
            if only_unassigned and r.codepoint is not None:
                note = "already has a code – skipped"
            else:
                note = "no character left in the file – left as is"
            plan.append(_PlanRow(r, "", r.codepoint, False, note, _DIM))
            continue
        it = next(item_iter)
        if it.codepoint is None:
            plan.append(_PlanRow(r, it.source, None, True, it.problem or "no glyph", _BAD))
            problems += 1
        elif it.codepoint in taken:
            plan.append(_PlanRow(r, it.source, None, True,
                                 f"U+{it.codepoint:04X} used by another capture – not assigned", _BAD))
            problems += 1
        elif it.codepoint in existing_cps and not include_existing:
            plan.append(_PlanRow(r, it.source, None, True,
                                 "already in the project – not assigned", _WARN))
            problems += 1
        else:
            note, color = "", None
            if it.codepoint in existing_cps:
                note, color = "replaces the project's glyph on commit", _WARN
            plan.append(_PlanRow(r, it.source, it.codepoint, True, note, color))
            taken.add(it.codepoint)
            applied += 1
    summary = {
        "file_chars": len(items),
        "targets": len(targets),
        "applied": applied,
        "problems": problems,
        "unused_chars": max(0, len(items) - len(targets)),
        "uncovered": max(0, len(targets) - len(items)),
    }
    return plan, summary


class TextFileAssignDialog(QDialog):
    """Codepage choice + live preview. result_assignments (seq -> code
    or None) is filled on Apply; only rows the plan actually touches
    are included."""

    def __init__(self, parent, file_path: str, data: bytes,
                 rows: list[CaptureRowInfo], existing_cps: set[int],
                 include_existing_default: bool):
        super().__init__(parent)
        self.setWindowTitle("Assign from text file")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.resize(640, 600)
        self.result_assignments: dict[int, int | None] = {}
        self._data = data
        self._rows = rows
        self._existing = existing_cps
        self._thumbs: dict[int, QIcon] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"File: <b>{os.path.basename(file_path)}</b> "
                                f"({len(data)} bytes)"))

        cp_row = QHBoxLayout()
        cp_row.addWidget(QLabel("Codepage used by this file:"))
        self._codepage = QComboBox()
        for cid, label in CODEPAGES:
            self._codepage.addItem(label, cid)
        guess = guess_codepage(data)
        self._codepage.setCurrentIndex(max(0, self._codepage.findData(guess)))
        self._codepage.currentIndexChanged.connect(self._refresh)
        cp_row.addWidget(self._codepage, 1)
        layout.addLayout(cp_row)

        self._mode_all = QRadioButton("All captures, in capture order (replaces codes already entered)")
        self._mode_unassigned = QRadioButton("Only captures without a code yet, in capture order")
        group = QButtonGroup(self)
        group.addButton(self._mode_all)
        group.addButton(self._mode_unassigned)
        self._mode_all.setChecked(True)
        self._mode_all.toggled.connect(self._refresh)
        layout.addWidget(self._mode_all)
        layout.addWidget(self._mode_unassigned)

        self._include_existing = QCheckBox(
            "Include codes already in the project (they replace those glyphs on commit)")
        self._include_existing.setChecked(include_existing_default)
        self._include_existing.toggled.connect(self._refresh)
        layout.addWidget(self._include_existing)

        hint = QLabel("Spaces, tabs and line breaks in the file are ignored. "
                      "Any row can still be corrected by hand afterwards.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #777777;")
        layout.addWidget(hint)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Capture", "File", "Code", "Name", "Note"])
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setIconSize(QSize(_THUMB, _THUMB))
        self._table.verticalHeader().setDefaultSectionSize(_THUMB + 6)
        self._table.verticalHeader().setVisible(False)   # "#n" in the Capture column already numbers rows
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Interactive)
        hdr.setStretchLastSection(True)
        self._table.setColumnWidth(3, 170)
        layout.addWidget(self._table, 1)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._apply_btn = buttons.button(QDialogButtonBox.Ok)
        self._apply_btn.setText("Apply")
        buttons.accepted.connect(self._on_apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._plan: list[_PlanRow] = []
        self._refresh()

    def _thumb(self, info: CaptureRowInfo) -> QIcon:
        icon = self._thumbs.get(info.seq)
        if icon is None:
            icon = QIcon(readable_thumbnail_tile(info.thumb_path, _THUMB))
            self._thumbs[info.seq] = icon
        return icon

    def _refresh(self, *_args) -> None:
        self._plan, summary = build_plan(
            self._rows, self._data, self._codepage.currentData(),
            only_unassigned=self._mode_unassigned.isChecked(),
            include_existing=self._include_existing.isChecked(),
            existing_cps=self._existing)
        self._table.setRowCount(len(self._plan))
        for i, pr in enumerate(self._plan):
            cap = QTableWidgetItem(f"#{i + 1}")
            cap.setIcon(self._thumb(pr.info))
            self._table.setItem(i, 0, cap)
            self._table.setItem(i, 1, QTableWidgetItem(pr.file_text))
            code = f"U+{pr.new_cp:04X}" if pr.new_cp is not None else "—"
            code_item = QTableWidgetItem(code)
            name_item = QTableWidgetItem(_char_name(pr.new_cp) if pr.new_cp is not None else "")
            if not pr.applies:
                code_item.setForeground(QBrush(_DIM))
                name_item.setForeground(QBrush(_DIM))
            self._table.setItem(i, 2, code_item)
            self._table.setItem(i, 3, name_item)
            note = QTableWidgetItem(pr.note)
            if pr.note_color is not None:
                note.setForeground(QBrush(pr.note_color))
            self._table.setItem(i, 4, note)

        parts = [f"{summary['file_chars']} character(s) in the file, "
                 f"{summary['targets']} capture(s) to fill: "
                 f"<b>{summary['applied']} will be assigned</b>."]
        if summary["problems"]:
            parts.append(f"{summary['problems']} can't be assigned (see Note).")
        if summary["unused_chars"]:
            parts.append(f"<span style='color:#b8860b'>The file has {summary['unused_chars']} "
                         "more character(s) than captures – those are ignored. "
                         "Check the capture order.</span>")
        if summary["uncovered"]:
            parts.append(f"<span style='color:#b8860b'>{summary['uncovered']} capture(s) "
                         "have no character in the file and are left as they are.</span>")
        self._summary.setText(" ".join(parts))
        self._apply_btn.setEnabled(any(pr.applies for pr in self._plan))

    def _on_apply(self) -> None:
        self.result_assignments = {pr.info.seq: pr.new_cp for pr in self._plan if pr.applies}
        self.accept()
