# help_dialog.py -- in-app Help viewer.
#
# Deliberately reads the SAME .md files that back the GitHub-facing docs
# (see the project's own docs/ folder and README.md), rather than
# maintaining separate in-app help text -- one source of truth, so the
# two can't quietly drift apart the way two independently-written
# copies of "the same idea" tend to in this project's own experience.
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtGui import QImageReader, QTextCursor, QTextImageFormat
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter, QListWidget, QListWidgetItem,
    QLineEdit, QTextBrowser, QLabel, QWidget,
)


class _Section(NamedTuple):
    """One heading and the body text directly under it (up to the next
    heading of the same or higher level), from one doc file -- the
    unit search actually matches against and navigates to. A doc with
    no headings at all (shouldn't happen for any of this project's own
    docs, but a stray file might) still gets one section, headed by
    its own title."""
    doc_path: Path
    doc_title: str
    heading: str
    body: str


def _load_sections(docs_root: Path) -> list[_Section]:
    """Parses every .md file under docs_root (plus README.md alongside
    it, if present) into a flat list of _Section entries. A "section"
    starts at each Markdown heading line (# through ######) and runs
    to the next one -- deliberately simple line-based parsing, not a
    full Markdown parser, since headings-as-section-boundaries is all
    search actually needs; QTextBrowser's own setMarkdown() handles the
    real rendering later."""
    sections: list[_Section] = []
    # Explicit order, matching README.md's own documentation list --
    # NOT alphabetical (which would scatter Drawing/Export/Getting
    # Started/etc. in an order that has nothing to do with how someone
    # would actually want to read through them). Any .md file present
    # under docs_root but not named here still gets appended at the
    # end, alphabetically, rather than silently dropped.
    _ORDER = [
        "getting-started.md",
        "adding-glyphs.md",
        "capture.md",
        "selection-and-editing.md",
        "drawing.md",
        "snapping.md",
        "size-and-weight.md",
        "metrics-and-canvas.md",
        "export.md",
        "reference.md",
    ]
    all_md = {p.name: p for p in docs_root.glob("*.md")}
    md_files = [all_md.pop(name) for name in _ORDER if name in all_md]
    md_files += sorted(all_md.values())   # anything unlisted, appended
    readme = docs_root.parent / "README.md"
    if readme.exists():
        md_files = [readme] + md_files

    heading_re = re.compile(r"^(#{1,6})\s+(.*)$")
    for path in md_files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        doc_title = path.stem.replace("-", " ").title()
        current_heading = doc_title
        current_body: list[str] = []

        def _flush():
            if current_body or current_heading:
                sections.append(_Section(path, doc_title, current_heading,
                                          "\n".join(current_body).strip()))

        for line in lines:
            m = heading_re.match(line)
            if m:
                _flush()
                current_heading = m.group(2).strip()
                current_body = []
            else:
                current_body.append(line)
        _flush()
    return sections


class HelpDialog(QDialog):
    """Topic list + search on the left, rendered Markdown on the
    right -- a QTextBrowser showing the SAME .md files the project
    ships as its GitHub-facing docs (see docs/ and README.md), not
    separately-authored in-app text. Search matches against every
    section's heading and body text (see _load_sections); picking a
    result loads that section's own doc and scrolls to the matching
    heading via QTextBrowser's own find(), rather than needing Markdown
    headings to carry HTML anchors (setMarkdown() doesn't reliably give
    them one)."""

    def __init__(self, docs_root: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PNGlyph Help")
        self.resize(880, 620)
        self.docs_root = docs_root
        self.sections = _load_sections(docs_root)

        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.search_box = QLineEdit(placeholderText="Search help…")
        self.search_box.textChanged.connect(self._on_search_changed)
        left_layout.addWidget(self.search_box)
        # Set alongside the topic list on every search, read back by
        # _on_topic_clicked -- see that method's own comment for why
        # jumping to the section's heading alone isn't enough.
        self._current_query: str = ""

        self.topic_list = QListWidget()
        self.topic_list.itemClicked.connect(self._on_topic_clicked)
        left_layout.addWidget(self.topic_list)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        # So a screenshot referenced as ![alt](images/foo.png) in a doc
        # resolves correctly regardless of which doc is currently
        # loaded -- both the docs/ folder itself and the project root
        # (for README.md's own docs/... links) are searched.
        self.browser.setSearchPaths([str(docs_root), str(docs_root.parent)])
        # Screenshots are scaled down to fit the text width (never up --
        # see _fit_images); re-fitted, after a short pause, whenever the
        # viewer is resized.
        self.browser.document().setUndoRedoEnabled(False)
        self._image_sizes: dict[str, tuple[int, int] | None] = {}
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(60)
        self._fit_timer.timeout.connect(self._fit_images)
        self.browser.viewport().installEventFilter(self)
        right_layout.addWidget(self.browser)
        splitter.addWidget(right)
        splitter.setSizes([260, 620])

        self._populate_topic_list()
        if self.sections:
            self._show_doc(self.sections[0].doc_path)

    # -- topic list (unfiltered: one entry per doc, not per section) --

    def _populate_topic_list(self):
        self.topic_list.clear()
        seen: set[Path] = set()
        for section in self.sections:
            if section.doc_path in seen:
                continue
            seen.add(section.doc_path)
            item = QListWidgetItem(section.doc_title)
            item.setData(Qt.UserRole, ("doc", section.doc_path, None))
            self.topic_list.addItem(item)

    def _on_topic_clicked(self, item: QListWidgetItem):
        kind, doc_path, heading = item.data(Qt.UserRole)
        self._show_doc(doc_path)
        if kind == "section" and heading:
            self.browser.find(heading)
            # Confirmed as a real, reported problem otherwise: a
            # section can match a search purely because the query
            # appears somewhere in its BODY text, with the heading
            # itself never containing it at all -- landing only on the
            # heading left the actual query nowhere highlighted,
            # forcing a manual scan through the whole section to find
            # it. Searching forward for the query too, right after
            # positioning at the heading, lands on that real
            # occurrence instead. Harmless when the query already WAS
            # the heading (or is contained in it): the search either
            # re-finds the same spot or the next occurrence nearby,
            # not a wrong one.
            query = self._current_query.strip()
            if query and query.lower() != heading.strip().lower():
                self.browser.find(query)

    def _show_doc(self, doc_path: Path):
        try:
            text = doc_path.read_text(encoding="utf-8")
        except OSError:
            text = f"*Could not open {doc_path.name}.*"
        self.browser.setMarkdown(text)
        self._fit_images()

    # -- screenshots fitted to the window ----------------------------------

    def eventFilter(self, obj, event):
        if obj is self.browser.viewport() and event.type() == QEvent.Resize:
            self._fit_timer.start()
        return super().eventFilter(obj, event)

    def _natural_size(self, name: str) -> tuple[int, int] | None:
        """An image's own pixel size, read from the file (same search
        paths the browser itself uses), cached per name."""
        if name not in self._image_sizes:
            size = None
            for root in (self.docs_root, self.docs_root.parent):
                path = root / name
                if path.is_file():
                    qs = QImageReader(str(path)).size()
                    if qs.isValid():
                        size = (qs.width(), qs.height())
                    break
            self._image_sizes[name] = size
        return self._image_sizes[name]

    def _fit_images(self) -> None:
        """Gives every image in the current page a display width of
        min(its own width, the text area's width), keeping its aspect
        ratio -- so a wide screenshot fits the window instead of forcing
        it to be enlarged, while anything that already fits (toolbar
        icons, small shots) stays at its real, sharp size. Widening the
        window lets screenshots grow back, up to their real size, never
        beyond. The image files themselves (and the docs as GitHub shows
        them) are untouched; only how this viewer lays them out."""
        doc = self.browser.document()
        avail = self.browser.viewport().width() - 2 * doc.documentMargin() - 6
        if avail < 50:
            return   # not laid out yet -- the first resize event re-fits
        edits = []
        block = doc.begin()
        while block.isValid():
            # An image inside a list item or an indented block starts
            # further right, so it has that much less room.
            bf = block.blockFormat()
            indent = bf.leftMargin() + bf.textIndent() + bf.indent() * doc.indentWidth()
            if block.textList() is not None:
                indent += block.textList().format().indent() * doc.indentWidth()
            room = max(50, avail - indent)
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid() and frag.charFormat().isImageFormat():
                    fmt = frag.charFormat().toImageFormat()
                    natural = self._natural_size(fmt.name())
                    if natural:
                        w = min(natural[0], room)
                        h = natural[1] * w / natural[0]
                        if abs(fmt.width() - w) > 0.5:
                            edits.append((frag.position(), frag.length(), fmt, w, h))
                it += 1
            block = block.next()
        if not edits:
            return
        bar = self.browser.verticalScrollBar()
        ratio = bar.value() / bar.maximum() if bar.maximum() else 0.0
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        for pos, length, fmt, w, h in edits:
            new_fmt = QTextImageFormat(fmt)
            new_fmt.setWidth(w)
            new_fmt.setHeight(h)
            cursor.setPosition(pos)
            cursor.setPosition(pos + length, QTextCursor.KeepAnchor)
            cursor.setCharFormat(new_fmt)
        cursor.endEditBlock()
        # Keep roughly the same place in the page after re-flowing.
        bar.setValue(round(ratio * bar.maximum()))

    # -- search: filters the SAME list to matching sections ------------

    def _on_search_changed(self, query: str):
        self._current_query = query
        query = query.strip()
        if not query:
            self._populate_topic_list()
            return
        q = query.lower()
        self.topic_list.clear()
        for section in self.sections:
            if q in section.heading.lower() or q in section.body.lower():
                label = f"{section.doc_title} \u203a {section.heading}"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, ("section", section.doc_path, section.heading))
                self.topic_list.addItem(item)
        if self.topic_list.count() == 0:
            placeholder = QListWidgetItem("No matches")
            placeholder.setFlags(Qt.NoItemFlags)
            self.topic_list.addItem(placeholder)
