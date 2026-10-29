"""
Per-project capture-session temp storage: <source_dir>/.capture_session/,
holding the cropped PNGs captured so far plus a JSON manifest recording
each one's assigned codepoint (or None if not yet assigned).

Nothing here touches the real Project -- see CaptureSessionStore.commit(),
which is the ONLY place that reaches into project.add_glyph, and only at
the very end of a session. Everything before that is just files on disk,
so a full discard is a clean no-op against the actual project, and a
crash mid-session loses nothing beyond what the last atomic manifest
write already captured.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, asdict

SESSION_DIRNAME = ".capture_session"
MANIFEST_NAME = "manifest.json"


@dataclass
class CaptureItem:
    seq: int
    filename: str                  # e.g. "capture_003.png", relative to the session dir
    codepoint: int | None = None
    timestamp: str = ""
    flatness_warning: str | None = None   # set once, at capture time -- see add_capture


@dataclass
class CaptureManifest:
    items: list[CaptureItem] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({"items": [asdict(i) for i in self.items]}, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "CaptureManifest":
        d = json.loads(text)
        return cls(items=[CaptureItem(**i) for i in d.get("items", [])])


class CaptureSessionStore:
    """Owns <source_dir>/.capture_session/ for one project. One instance
    per open session (created fresh for a new session, or pointed at an
    existing folder to resume one)."""

    def __init__(self, source_dir: str):
        self.source_dir = source_dir
        self.session_dir = os.path.join(source_dir, SESSION_DIRNAME)
        self.manifest_path = os.path.join(self.session_dir, MANIFEST_NAME)
        self._manifest = CaptureManifest()
        self._next_seq = 1

    # -- existence / loading ---------------------------------------------

    @staticmethod
    def has_pending_session(source_dir: str) -> bool:
        manifest_path = os.path.join(source_dir, SESSION_DIRNAME, MANIFEST_NAME)
        return os.path.exists(manifest_path)

    def load_existing(self) -> bool:
        """Loads the manifest if one exists. Returns True if a session
        was actually found and loaded, False if the folder is empty/
        absent (i.e. this is really a fresh session)."""
        if not os.path.exists(self.manifest_path):
            return False
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            self._manifest = CaptureManifest.from_json(f.read())
        self._next_seq = (max((i.seq for i in self._manifest.items), default=0) + 1)
        return True

    @property
    def items(self) -> list[CaptureItem]:
        return list(self._manifest.items)

    def item_path(self, item: CaptureItem) -> str:
        return os.path.join(self.session_dir, item.filename)

    # -- mutation (each writes the manifest atomically before returning) --

    def _write_manifest_atomic(self) -> None:
        os.makedirs(self.session_dir, exist_ok=True)
        tmp_path = self.manifest_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(self._manifest.to_json())
        os.replace(tmp_path, self.manifest_path)   # atomic on POSIX and Windows

    def add_capture(self, png_bytes: bytes) -> CaptureItem:
        """png_bytes is the already-encoded PNG data for one crop (the
        caller does the QPixmap -> PNG encoding; this module has no Qt
        dependency, matching the rest of core/-style modules)."""
        import datetime
        os.makedirs(self.session_dir, exist_ok=True)
        seq = self._next_seq
        self._next_seq += 1
        filename = f"capture_{seq:03d}.png"
        full_path = os.path.join(self.session_dir, filename)
        with open(full_path, "wb") as f:
            f.write(png_bytes)
        # Computed once, here, rather than every time a row widget
        # happens to be (re)built -- a captured region is a raw,
        # unprocessed screenshot crop (see session_controller.py's own
        # on_captured), so unlike a purpose-prepared source PNG it's
        # genuinely likely to carry real anti-aliasing/font-smoothing
        # even when it looks clean by eye -- see
        # core.tracing.load_glyph_mask's own docstring for why that
        # specifically produces small deviations on otherwise-straight
        # traced segments, not just a resolution problem.
        from core import tracing
        flatness_warning = tracing.load_glyph_mask(full_path).flatness_warning
        item = CaptureItem(seq=seq, filename=filename,
                            timestamp=datetime.datetime.now().isoformat(timespec="seconds"),
                            flatness_warning=flatness_warning)
        self._manifest.items.append(item)
        self._write_manifest_atomic()
        return item

    def update_codepoint(self, seq: int, codepoint: int | None) -> None:
        for item in self._manifest.items:
            if item.seq == seq:
                item.codepoint = codepoint
                self._write_manifest_atomic()
                return

    def remove_item(self, seq: int) -> None:
        for item in list(self._manifest.items):
            if item.seq == seq:
                try:
                    os.remove(self.item_path(item))
                except OSError:
                    pass
                self._manifest.items.remove(item)
                self._write_manifest_atomic()
                return

    # -- ending the session ------------------------------------------------

    def discard(self) -> None:
        """Deletes the whole session folder. Safe even mid-write since
        the manifest itself is never left half-written (see
        _write_manifest_atomic) -- there's nothing here that a discard
        could leave in a half-committed state."""
        shutil.rmtree(self.session_dir, ignore_errors=True)
        self._manifest = CaptureManifest()

    def clear_after_commit(self) -> None:
        """Called by the caller (MainWindow) once every assigned item has
        already been copied into the real project's source_dir -- this
        just removes the now-redundant session folder. Distinct from
        discard() only in naming/intent; the effect is the same."""
        self.discard()
