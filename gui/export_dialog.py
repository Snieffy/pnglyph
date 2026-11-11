from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QWidget,
    QLineEdit, QSpinBox, QCheckBox, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QMessageBox, QProgressDialog, QApplication, QLabel,
    QComboBox,
)

from core.project import Project
from core.font_export import FontExporter, export_multi_weight
from core.coverage import coverage_report


class ExportDialog(QDialog):
    """Everything involved in producing a TTF from the current project,
    in one place -- export-time settings (spacing, multi-weight),
    name-table metadata, and a glyph-coverage report. All four are
    genuinely "Export TTF" concerns (metadata is what goes into the
    TTF's name table, coverage is which characters end up in it,
    spacing/multi-weight are export-time settings), not something
    separate that happens to live nearby -- so this replaces both the
    old standalone "Advanced Settings" dialog AND the old "Export TTF"
    toolbar action's direct-to-file-picker behavior with one dialog,
    reachable from a single "Export TTF" toolbar button. Family and
    EM/vertical-metrics moved to the main screen's own Canvas panel in
    an earlier pass and live there now, edited live rather than
    through this dialog's own buttons -- this dialog no longer
    duplicates them."""

    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.project = project
        self.setWindowTitle("Export TTF")
        self.resize(580, 520)

        layout = QVBoxLayout(self)
        self.tabs = tabs = QTabWidget()
        layout.addWidget(tabs)
        tabs.addTab(self._build_export_tab(), "Export")
        tabs.addTab(self._build_metadata_tab(), "Metadata")
        tabs.addTab(self._build_coverage_tab(), "Coverage")
        self._multiweight_section.setEnabled(self.multi_weight_check.isChecked())

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.export_btn = QPushButton("Export…")
        self.export_btn.setDefault(True)
        self.export_btn.clicked.connect(self._on_export_clicked)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(self.export_btn)
        layout.addLayout(buttons)

    # -- Export: spacing, line gap, multi-weight toggle + config -------------

    def _build_export_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        fs = self.project.font

        self.monospace_combo = QComboBox()
        self.monospace_combo.setEditable(False)
        self.monospace_combo.addItems(["Monospace", "Proportional"])
        self.monospace_combo.setCurrentIndex(0 if fs.monospace else 1)
        form.addRow("Spacing", self.monospace_combo)

        self.line_gap_spin = QSpinBox()
        self.line_gap_spin.setRange(0, 4096)
        self.line_gap_spin.setValue(fs.line_gap)
        form.addRow("Line gap", self.line_gap_spin)

        self.multi_weight_check = QCheckBox("Export multiple weight variants at once")
        self.multi_weight_check.toggled.connect(self._on_multi_weight_toggled)
        form.addRow(self.multi_weight_check)

        note = QLabel("Family name and vertical metrics (EM, Ascent, Descent) are set "
                       "live on the main screen's Canvas panel now, not here.")
        note.setStyleSheet("color: #888;")
        form.addRow(note)

        # Multi-weight's own config lives directly on this tab, shown
        # only once the checkbox above is enabled -- not a separate
        # tab. A dedicated tab meant checking the box, then switching
        # tabs just to actually configure what was checked, for a
        # feature most exports never touch; putting it here removes
        # that extra step AND gives this otherwise fairly sparse tab
        # something to do with its own space when it's actually in use.
        self._multiweight_section = self._build_multiweight_section()
        form.addRow(self._multiweight_section)
        return w

    def _on_multi_weight_toggled(self, checked: bool):
        # Grayed out, not hidden -- setEnabled cascades to every child
        # widget in the section (table, Add/Remove row buttons) with
        # this one call, disabling and visually graying them out while
        # the row still reserves its normal layout space -- so the
        # weight/style setup is visible (if inert) as soon as the
        # dialog opens, rather than appearing only once the checkbox
        # above is checked.
        self._multiweight_section.setEnabled(checked)
        self.export_btn.setText("Export All Weights…" if checked else "Export…")

    # -- Metadata: name-table fields ------------------------------------

    def _build_metadata_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        fs = self.project.font
        self.designer_edit = QLineEdit(fs.designer)
        self.manufacturer_edit = QLineEdit(fs.manufacturer)
        self.copyright_edit = QLineEdit(fs.copyright)
        self.trademark_edit = QLineEdit(fs.trademark)
        self.description_edit = QLineEdit(fs.description)
        self.vendor_url_edit = QLineEdit(fs.vendor_url)
        self.designer_url_edit = QLineEdit(fs.designer_url)
        self.license_edit = QLineEdit(fs.license)
        self.license_url_edit = QLineEdit(fs.license_url)
        form.addRow("Designer", self.designer_edit)
        form.addRow("Manufacturer", self.manufacturer_edit)
        form.addRow("Copyright", self.copyright_edit)
        form.addRow("Trademark", self.trademark_edit)
        form.addRow("Description", self.description_edit)
        form.addRow("Vendor URL", self.vendor_url_edit)
        form.addRow("Designer URL", self.designer_url_edit)
        form.addRow("License", self.license_edit)
        form.addRow("License URL", self.license_url_edit)
        note = QLabel("Blank fields are left out of the font entirely, not written as empty.")
        note.setStyleSheet("color: #888;")
        form.addRow(note)
        return w

    # -- Multi-weight batch export config (embedded in the Export tab) ------

    def _build_multiweight_section(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(QLabel("Export several stroke weights of the current glyph set at once,\n"
                                 "each as its own .ttf file named \"<base name>-<style>.ttf\"."))

        self.weight_table = QTableWidget(0, 2)
        self.weight_table.setHorizontalHeaderLabels(["Weight %", "Style name"])
        self.weight_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.weight_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        current_pct = round(self.project.font.weight_ratio * 100)
        for pct, name in [(max(current_pct - 5, 1), "Light"),
                           (current_pct, self.project.font.style_name or "Regular"),
                           (current_pct + 6, "Bold")]:
            self._add_weight_row(pct, name)
        layout.addWidget(self.weight_table)

        row_btns = QHBoxLayout()
        add_btn = QPushButton("Add row")
        add_btn.clicked.connect(lambda: self._add_weight_row(10, "Style"))
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove_selected_weight_row)
        row_btns.addWidget(add_btn)
        row_btns.addWidget(remove_btn)
        row_btns.addStretch()
        layout.addLayout(row_btns)
        return w

    def _add_weight_row(self, pct: int, name: str):
        row = self.weight_table.rowCount()
        self.weight_table.insertRow(row)
        pct_spin = QSpinBox()
        pct_spin.setRange(1, 50)
        pct_spin.setValue(pct)
        self.weight_table.setCellWidget(row, 0, pct_spin)
        self.weight_table.setItem(row, 1, QTableWidgetItem(name))

    def _remove_selected_weight_row(self):
        rows = sorted({idx.row() for idx in self.weight_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.weight_table.removeRow(r)

    # -- Coverage report ---------------------------------------------------

    def _build_coverage_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Block", "Covered", "%"])
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        report = coverage_report(set(self.project.glyphs.keys()))
        table.setRowCount(len(report))
        for i, block in enumerate(report):
            table.setItem(i, 0, QTableWidgetItem(block["name"]))
            if block["total"] is not None:
                table.setItem(i, 1, QTableWidgetItem(f"{block['covered']}/{block['total']}"))
                pct = 100 * block["covered"] / block["total"] if block["total"] else 0
                table.setItem(i, 2, QTableWidgetItem(f"{pct:.0f}%"))
            else:
                table.setItem(i, 1, QTableWidgetItem(str(block["covered"])))
                table.setItem(i, 2, QTableWidgetItem("—"))
        layout.addWidget(table)
        return w

    # -- Export ---------------------------------------------------------

    def _apply_settings(self):
        """Writes the Export/Metadata tabs' current values into the
        project -- called right before actually exporting, in either
        flow below, rather than needing a separate OK/Apply step.
        Family and vertical metrics aren't touched here at all (see
        this class's own docstring): they're live-edited on the main
        screen already, so self.project.font already reflects
        whatever's currently set there."""
        fs = self.project.font
        fs.monospace = (self.monospace_combo.currentIndex() == 0)
        fs.line_gap = self.line_gap_spin.value()
        fs.designer = self.designer_edit.text()
        fs.manufacturer = self.manufacturer_edit.text()
        fs.copyright = self.copyright_edit.text()
        fs.trademark = self.trademark_edit.text()
        fs.description = self.description_edit.text()
        fs.vendor_url = self.vendor_url_edit.text()
        fs.designer_url = self.designer_url_edit.text()
        fs.license = self.license_edit.text()
        fs.license_url = self.license_url_edit.text()

    def _on_export_clicked(self):
        self._apply_settings()
        if self.multi_weight_check.isChecked():
            self._export_multi_weight()
        else:
            self._export_single()

    def _export_single(self):
        # Explicit default directory rather than relying on Qt's
        # implicit "last browsed folder" memory -- that's a global,
        # shared-across-dialogs mechanism that happened to make export
        # LOOK like it was defaulting sensibly (since it usually runs
        # right after browsing to the project's own folder for some
        # other reason), while save/open had no such coincidence to
        # ride on. Being explicit here means it's correct regardless of
        # whatever the user happened to browse to previously.
        default_path = os.path.join(self.project.source_dir, f"{self.project.font.family_name}.ttf")
        path, _ = QFileDialog.getSaveFileName(self, "Export TTF", default_path, filter="TrueType font (*.ttf)")
        if not path:
            return

        # If the family name is still whatever got auto-filled from the
        # SOURCE FOLDER at import time (e.g. "Unicode_PNG"), and was
        # never deliberately changed, syncing it to the chosen output
        # filename means the font's own internal Title/Full-name
        # metadata actually matches the file the user is looking at,
        # instead of naming itself after an unrelated folder. Left
        # alone the moment it no longer matches that original default,
        # since that means the user already set something deliberately.
        default_family = os.path.basename(self.project.source_dir.rstrip("/\\")) or "New Font"
        if self.project.font.family_name == default_family:
            chosen_name = os.path.splitext(os.path.basename(path))[0]
            if chosen_name:
                self.project.font.family_name = chosen_name

        progress = QProgressDialog("Building glyphs…", "Cancel", 0, len(self.project.glyphs), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(300)   # don't flash it for a near-instant export

        def on_progress(index: int, total: int, cp: int):
            progress.setMaximum(total)
            progress.setValue(index)
            progress.setLabelText(f"Building glyphs… U+{cp:04X} ({index + 1}/{total})")
            QApplication.processEvents()

        try:
            exporter = FontExporter(self.project)
            report = exporter.export(path, progress_cb=on_progress)
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Export failed", str(e))
            return
        progress.close()

        msg = f"Built {len(report['built'])} glyphs, {len(report['failed'])} failed, {len(report['skipped'])} skipped."
        if report["failed"]:
            details = "\n".join(f"U+{cp:04X}: {err}" for cp, err in report["failed"][:10])
            if len(report["failed"]) > 10:
                details += f"\n… and {len(report['failed']) - 10} more"
            msg += f"\n\nFailed:\n{details}"
        if report["skipped"]:
            details = "\n".join(f"U+{cp:04X}: {reason}" for cp, reason in report["skipped"][:10])
            if len(report["skipped"]) > 10:
                details += f"\n… and {len(report['skipped']) - 10} more"
            msg += f"\n\nSkipped:\n{details}"
        QMessageBox.information(self, "Export complete", msg)
        self.accept()

    def _export_multi_weight(self):
        variants = []
        for row in range(self.weight_table.rowCount()):
            pct_widget = self.weight_table.cellWidget(row, 0)
            name_item = self.weight_table.item(row, 1)
            name = (name_item.text() if name_item else "").strip()
            if not name:
                QMessageBox.warning(self, "Missing style name",
                                     f"Row {row + 1} needs a style name.")
                return
            variants.append((pct_widget.value() / 100, name))
        if not variants:
            QMessageBox.warning(self, "No variants", "Add at least one weight row first.")
            return
        names = [v[1] for v in variants]
        if len(names) != len(set(names)):
            QMessageBox.warning(self, "Duplicate style names",
                                 "Each row needs a distinct style name.")
            return

        out_dir = QFileDialog.getExistingDirectory(self, "Export All Weights — choose a folder")
        if not out_dir:
            return
        base_name = self.project.font.family_name.replace(" ", "")

        progress = QProgressDialog("Exporting weight variants…", "Cancel", 0, len(variants), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(300)

        def on_progress(vi, vtotal, style, gi, gtotal, cp):
            progress.setMaximum(vtotal)
            progress.setValue(vi)
            progress.setLabelText(f"Exporting {style} ({vi + 1}/{vtotal})… glyph {gi + 1}/{gtotal}")
            QApplication.processEvents()

        results = export_multi_weight(self.project, out_dir, base_name, variants,
                                       progress_cb=on_progress)
        progress.close()

        lines = []
        for style, report in results.items():
            if "error" in report:
                lines.append(f"{style}: FAILED — {report['error']}")
            else:
                lines.append(f"{style}: {len(report['built'])} built, "
                              f"{len(report['failed'])} failed, {len(report['skipped'])} skipped")
        QMessageBox.information(self, "Multi-weight export complete", "\n".join(lines))
        self.accept()
