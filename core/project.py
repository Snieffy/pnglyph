"""
Project file format: a single JSON file that fully describes a PNGlyph
project -- where the source PNGs live, the calibrated (or manually set)
metrics grid, font-wide settings, and a per-glyph list of edit steps.

Storing edits as a replayable recipe (rather than baking them silently
into a one-off script, which is how this project's own font build
started out) means:
  - re-running the same recipe against updated source PNGs reproduces
    the same fixes automatically
  - a diff between two project files shows exactly what changed
  - a bug in one step (see the u00bc '4'-digit regression during this
    project) is visible and fixable in place, not buried in a function
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, asdict


SCHEMA_VERSION = 1

# Hidden subfolder INSIDE source_dir (never a sibling) that holds
# replacement source images -- see Project.staging_dir's own docstring
# for the full reasoning (portability, and safety from
# scan_source_dir's own scan).
STAGING_DIRNAME = ".pnglyph_staging"


@dataclass
class FontSettings:
    family_name: str = "New Font"
    style_name: str = "Regular"
    em: int = 1000
    weight_ratio: float = 0.10        # stroke width as a fraction of cap height
    cap_style: str = "round"          # "round" | "flat"
    monospace: bool = True
    version: str = "0.1"

    # Vertical metrics: None means "derive from cap height automatically",
    # matching the export's existing fallback formula -- set explicitly
    # only when the auto-derived value isn't what's wanted.
    ascent_override: int | None = None
    descent_override: int | None = None    # positive number of design units below baseline
    line_gap: int = 0

    # name-table metadata (nameIDs 0, 7-14) -- all optional; empty means
    # "don't write this field into the font at all" rather than writing
    # an empty string, since an absent field is standard and a blank one
    # can look like an error in font-inspection tools.
    designer: str = ""
    manufacturer: str = ""
    copyright: str = ""
    trademark: str = ""
    description: str = ""
    vendor_url: str = ""
    designer_url: str = ""
    license: str = ""
    license_url: str = ""


@dataclass
class Metrics:
    cap_top_px: float | None = None
    baseline_px: float | None = None
    xheight_top_px: float | None = None
    descender_bottom_px: float | None = None
    advance_px: float | None = None
    canvas_size: list[int] | None = None   # [w, h]


def auto_vertical_metrics(font: FontSettings, metrics: Metrics) -> tuple[int, int]:
    """The same auto-derived (ascent, descent) formula the export step
    actually uses when no explicit override is set. Shared by
    font_export.py and the Advanced Settings dialog specifically so the
    dialog's "auto" preview can never drift out of sync with what an
    export would really produce -- which is exactly what happened
    before this was factored out: the dialog had its own copy of this
    formula that used a fixed em-based fallback for descent
    unconditionally, instead of the calibration-based one export.py
    uses whenever real calibration data is available (the normal
    case) -- so the dialog could show a smaller "auto" value than
    export would actually use, and if a user enabled the override
    while that wrong number was showing, it got baked in as a real,
    too-small descent -- small enough on some projects to clip a
    descender in some rendering paths that treat usWinDescent as a
    hard bound."""
    em = font.em
    cap_design = round(em * 0.68)
    auto_ascent = round(cap_design * 1.2)
    if metrics.cap_top_px is not None and metrics.baseline_px is not None \
            and metrics.descender_bottom_px is not None:
        cap_px = metrics.baseline_px - metrics.cap_top_px
        if cap_px > 0:
            scale = cap_design / cap_px
            des_design = round((metrics.descender_bottom_px - metrics.baseline_px) * scale)
            return auto_ascent, round(des_design * 1.5)
    return auto_ascent, round(em * 0.3)


@dataclass
class EditStep:
    """One replayable edit operation, matching a core.geometry function
    name and its keyword arguments."""
    op: str                      # e.g. "flatten", "offset_part", "move_point"
    args: dict = field(default_factory=dict)


@dataclass
class GlyphRecipe:
    codepoint: int
    source_file: str = ""        # empty = no source PNG; drawn from scratch, zero paths to start
    edits: list[EditStep] = field(default_factory=list)
    excluded: bool = False       # user marked this glyph as skip-for-now
    # Persistent groups of path indices -- distinct from the transient,
    # selection-based group-drag mechanism (select several segments,
    # drag one, they all move together): that already worked and isn't
    # what this is. This is specifically for pieces Join found were
    # non-continuous (a junction, or too far apart to be the same
    # stroke) and therefore couldn't be safely merged into one
    # polyline -- rather than leave them as ordinary, unrelated
    # separate paths, they're remembered here so the SAME set stays
    # selected together every time the glyph loads (a fresh session,
    # switching glyphs and back, reopening the project), not just for
    # as long as the current selection happens to survive. Indices are
    # into the CURRENT (fully edited) paths list; kept in sync with
    # index shifts from break/join/delete at the points those happen
    # (see main_window's own handlers), and re-validated (stale
    # indices dropped) every time the glyph is loaded, since a
    # positional index has no way to survive edits it wasn't updated
    # for.
    groups: list[list[int]] = field(default_factory=list)
    # A structural, import-time-only scale applied to fit an oversized
    # source image to the project's own canvas -- deliberately kept
    # SEPARATE from edits, not appended to it as a scale_uniform
    # EditStep the way it used to be. Confirmed as a real, reported
    # point of confusion otherwise: appending it to edits made the
    # glyph's list icon show the "edited" marker background (see
    # MainWindow._refresh_glyph_icon's own check of recipe.edits)
    # despite the user never having touched anything themselves --
    # purely a side effect of their source PNG happening to be a
    # different size than the canvas. Importing a whole PNG folder
    # (Project.scan_source_dir) never applied any such resize at all,
    # so it never triggered this either; keeping this out of edits
    # entirely reproduces that same, already-correct outcome for
    # File.../Capture's own single- and multi-glyph adds too, rather
    # than requiring the "edited" check itself to somehow guess which
    # of a glyph's own edits were "real" and which were structural.
    # 1.0 means "no import-time resize was needed" -- applied (see
    # MainWindow._load_glyph_into_canvas and every other place that
    # traces+applies edits) BEFORE edits itself, the same position the
    # old scale_uniform step always occupied as the first entry in a
    # freshly-imported glyph's own edits list.
    import_scale: float = 1.0


@dataclass
class Project:
    source_dir: str
    font: FontSettings = field(default_factory=FontSettings)
    metrics: Metrics = field(default_factory=Metrics)
    glyphs: dict[int, GlyphRecipe] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    # -- serialization --------------------------------------------------

    def to_json(self) -> str:
        d = {
            "schema_version": self.schema_version,
            # source_dir intentionally NOT included -- the .json and
            # its PNGs are required to live in the same folder (see
            # MainWindow._save_project's own naming-only dialog, which
            # no longer offers browsing to any other location), so
            # source_dir is always simply wherever this .json file
            # itself currently is, derived fresh at load time (see
            # load's own computation of it), never trusted as stored
            # data. Leaving it out entirely avoids ever giving the
            # false impression it's meaningful, versioned data a user
            # might reasonably expect to be respected on load -- it
            # never is.
            "font": asdict(self.font),
            "metrics": asdict(self.metrics),
            "glyphs": {
                str(cp): {
                    "codepoint": g.codepoint,
                    "source_file": g.source_file,
                    "excluded": g.excluded,
                    "edits": [{"op": e.op, "args": e.args} for e in g.edits],
                    "groups": g.groups,
                    "import_scale": g.import_scale,
                }
                for cp, g in self.glyphs.items()
            },
        }
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str, source_dir: str) -> "Project":
        """source_dir is a required, explicit parameter, never read
        from text itself -- see to_json's own comment on why a stored
        source_dir is never trusted regardless of whether one happens
        to be present (e.g. in a .json saved before this rule existed).
        Callers must supply where this project's own PNGs actually
        live; load() is the one caller that knows this (the .json
        file's own current folder) and is expected to be used instead
        of calling this directly in normal use."""
        d = json.loads(text)
        font = FontSettings(**d.get("font", {}))
        metrics = Metrics(**d.get("metrics", {}))
        glyphs = {}
        for cp_str, g in d.get("glyphs", {}).items():
            glyphs[int(cp_str)] = GlyphRecipe(
                codepoint=g["codepoint"],
                source_file=g["source_file"],
                excluded=g.get("excluded", False),
                edits=[EditStep(op=e["op"], args=e.get("args", {}))
                       for e in g.get("edits", [])],
                groups=g.get("groups", []),
                import_scale=g.get("import_scale", 1.0),
            )
        return cls(
            source_dir=source_dir,
            font=font,
            metrics=metrics,
            glyphs=glyphs,
            schema_version=d.get("schema_version", SCHEMA_VERSION),
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "Project":
        """source_dir is always the .json file's own current parent
        folder -- never read from the file's own contents, and no
        fallback logic is needed for a folder having been moved,
        copied, or renamed: wherever this .json currently sits IS
        source_dir, by definition, every time. This is what actually
        closes the "moved project folder" problem at its root, rather
        than patching around a stored, potentially-stale absolute path
        after the fact."""
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        source_dir = os.path.dirname(os.path.abspath(path))
        project = cls.from_json(text, source_dir=source_dir)
        project._reconcile_staging()
        return project

    def find_missing_glyphs(self) -> list[int]:
        """Codepoints whose own source_file is set but can't actually
        be found (via resolve_source_path, so a staged replacement
        still correctly counts as present) -- individual PNGs
        deliberately or accidentally deleted/renamed separately from
        this project's own .json, which nothing else here would
        otherwise notice until the affected glyph is actually clicked
        on. Called once, right after load (see MainWindow._load_project's
        own use of this), so the user is told about every affected
        glyph up front, all at once -- not discovered piecemeal,
        possibly much later, one confusing crash at a time."""
        missing = []
        for cp, recipe in self.glyphs.items():
            if recipe.source_file and not os.path.exists(self.resolve_source_path(recipe)):
                missing.append(cp)
        return sorted(missing)

    # -- staged (replacement) source images --------------------------------

    @property
    def staging_dir(self) -> str:
        """Where a REPLACEMENT source image goes once the codepoint
        it's replacing already exists in this project -- never
        source_dir itself, which stays untouched forever once a
        project has been created or imported (see
        resolve_source_path's own docstring for the full picture). A
        hidden subfolder INSIDE source_dir, not a sibling, deliberately
        -- so moving or archiving the whole project folder carries
        staged replacements along automatically, with nothing extra to
        track separately. Confirmed safe from scan_source_dir's own
        os.listdir()-based scan: that only lists direct children and
        rejects anything not matching u<hex>.png by name, so this
        folder is never mistaken for a real source glyph."""
        return os.path.join(self.source_dir, STAGING_DIRNAME)

    def resolve_source_path(self, recipe: "GlyphRecipe") -> str:
        """The actual file to read for recipe's own source image --
        staging first, source_dir as the fallback. Centralizes what
        would otherwise be duplicated "check staging, else fall back"
        logic at every call site that resolves a glyph's own source
        path (tracing, display, font export) -- calibration is the one
        deliberate exception, since it only ever runs against
        source_dir directly, at project-creation/import time, before
        any staging data could even exist yet.

        Returns "" (never raises) when recipe has no source_file at
        all -- a from-scratch, drawn-not-captured glyph -- matching
        what every existing caller already expected from a plain
        os.path.join(source_dir, recipe.source_file) in that case."""
        if not recipe.source_file:
            return ""
        staged = os.path.join(self.staging_dir, recipe.source_file)
        if os.path.exists(staged):
            return staged
        return os.path.join(self.source_dir, recipe.source_file)

    def stage_source_file(self, dest_filename: str, src_path: str) -> None:
        """Copies src_path into this project's own staging area under
        dest_filename, creating the staging folder itself if this is
        the first replacement this project has ever needed. Overwrites
        any EXISTING staged file under that same name outright -- that
        file was already unsaved, in-memory-adjacent data (never the
        original PNG), so replacing it is exactly as safe as
        overwriting source_dir directly used to be, just redirected to
        a location that never touches the genuinely original PNG."""
        os.makedirs(self.staging_dir, exist_ok=True)
        shutil.copyfile(src_path, os.path.join(self.staging_dir, dest_filename))

    def _reconcile_staging(self) -> None:
        """Deletes any staged file this project's own glyphs don't
        currently reference -- an orphan from a capture/file-add that
        was staged but never made it into a saved .json, most commonly
        a crash or closing without saving in between the two. Run once,
        right after loading (see load's own call above), the one
        moment this actually needs checking -- no separate, persistent
        tracking needed the way an in-progress capture session's own
        manifest provides: anything that WAS saved is, by definition,
        still referenced here and survives this untouched, and nothing
        needs remembering between one load and the next."""
        if not os.path.isdir(self.staging_dir):
            return
        referenced = {g.source_file for g in self.glyphs.values() if g.source_file}
        for name in os.listdir(self.staging_dir):
            if name not in referenced:
                try:
                    os.remove(os.path.join(self.staging_dir, name))
                except OSError:
                    pass   # best-effort cleanup -- a leftover orphan next time isn't harmful

    # -- convenience ------------------------------------------------------

    def add_glyph(self, codepoint: int, source_file: str = "") -> GlyphRecipe:
        g = GlyphRecipe(codepoint=codepoint, source_file=source_file)
        self.glyphs[codepoint] = g
        return g

    @staticmethod
    def default_metrics_for_canvas(canvas_width: int, canvas_height: int) -> Metrics:
        """Sensible starting proportions for cap/x-height/baseline/
        descender/advance, given only a canvas size -- NOT arbitrary
        guesses. Taken directly (no rounding) from what calibrate()
        actually measures on this project's own real, calibrated
        dataset (291x509): cap_top_px/height, xheight_top_px/height,
        baseline_px/height, descender_bottom_px/height, and critically
        advance_px/width == 1.0 exactly, not some smaller fraction of
        it. That last one matters most: calibrate() literally sets
        advance_px to the mode canvas width across the whole imported
        set (the SAME number as canvas_size[0]), so for an imported
        project "canvas size" and "advance width" are the same thing
        by construction. A from-scratch project entering the identical
        width/height should land on essentially the same box, not one
        shrunk by an unrelated, much smaller ratio -- confirmed as a
        real, confusing mismatch: entering 291x509 here used to
        produce only a 175x417 usable box, a difference easy to
        mistake for a bug in the canvas machinery itself rather than
        in these starting numbers.

        Shared between new_blank (a from-scratch project has nothing
        AT ALL to calibrate from) and MainWindow._import_folder (a
        real PNG set calibrate() only PARTIALLY succeeded on -- some
        specific metric(s) came back None, with a warning explaining
        why) as a per-metric fallback: guide lines need SOME starting
        position to exist at all before they can be dragged into
        place. None means "nothing to show yet", not "figure it out
        interactively" -- confirmed as a real, reported bug in the
        latter case: a calibration warning telling the user to
        manually position cap/x-height/descender, with no guide for
        any of them ever appearing on screen to actually do that with,
        since the guide-building step skips any metric that's still
        None entirely rather than falling back to something."""
        return Metrics(
            cap_top_px=round(canvas_height * 0.0019646365422396855),
            xheight_top_px=round(canvas_height * 0.2318271119842829),
            baseline_px=round(canvas_height * 0.7654223968565815),
            descender_bottom_px=round(canvas_height * 0.9941060903732809),
            advance_px=canvas_width,
            canvas_size=[canvas_width, canvas_height],
        )

    @classmethod
    def new_blank(cls, source_dir: str, canvas_width: int, canvas_height: int) -> "Project":
        """A from-scratch project: no source PNGs to calibrate from at
        all, so metrics start at default_metrics_for_canvas's own
        sensible proportions of the given canvas size -- see that
        method's own docstring for the full reasoning and the OTHER
        case it now also covers."""
        metrics = cls.default_metrics_for_canvas(canvas_width, canvas_height)
        return cls(source_dir=source_dir, metrics=metrics)

    def scan_source_dir(self) -> list[int]:
        """Populate self.glyphs from every u<hex>.png file found in
        source_dir that isn't already present. Returns newly added
        codepoints.

        Matched case-insensitively (U0041.PNG, u0041.png, U0041.Png,
        etc. all count) since that's an easy, harmless accommodation --
        but the STRUCTURE (u, hex digits, .png) is still required.
        Other filename patterns are deliberately out of scope: this
        tool reads Ultiboard-style u<hex>.png sets, not arbitrary,
        differently-named image collections, which would need asking
        the user what maps to what rather than just scanning a folder."""
        added = []
        for fname in sorted(os.listdir(self.source_dir)):
            lower = fname.lower()
            if not (lower.startswith("u") and lower.endswith(".png")):
                continue
            try:
                cp = int(fname[1:-4], 16)
            except ValueError:
                continue
            if cp not in self.glyphs:
                self.add_glyph(cp, fname)   # preserve the file's ACTUAL casing for opening it later
                added.append(cp)
        return added
