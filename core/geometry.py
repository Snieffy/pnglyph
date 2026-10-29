"""
Stroke thickening and path-editing operations, all working on the plain
polyline representation produced by core.tracing (list of (x, y) tuples
in source-pixel space). No Qt, no fontTools -- pure geometry.
"""
from __future__ import annotations

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

Point = tuple[float, float]
Path = list[Point]


# --- Thickening ----------------------------------------------------------

def thicken(paths: list[Path], half_width: float, cap_style: str = "round"):
    """Buffer a set of centerline paths into a single (Multi)Polygon.

    cap_style: "round" (matches most stroke-font sources: overshoot of
    half_width at each open end, and how corners get rounded in the
    first place) or "flat" (no overshoot).

    A zero or near-zero half_width has no valid buffered representation
    (a zero-width stroke has zero area -- shapely's buffer() of a line
    at radius 0 returns a degenerate/empty polygon, not a real shape),
    so that's treated the same as "no ink" rather than passed through
    to become a crash three calls later.
    """
    if half_width <= 1e-6:
        return None
    lines = [LineString(p) for p in paths if len(p) >= 2 and LineString(p).length > 0]
    if not lines:
        return None
    buffered = [ln.buffer(half_width, cap_style=cap_style, join_style="round",
                           quad_segs=8) for ln in lines]
    geom = unary_union(buffered)
    # Morphological closing bridges 1-2px gaps from skeleton/graph
    # discretization noise (e.g. a crossbar not quite reaching its stem)
    # without measurably altering real corners.
    closing_eps = max(2.0, half_width * 0.15)
    geom = geom.buffer(closing_eps, join_style="round").buffer(-closing_eps, join_style="round")
    return geom


def clearance(paths: list[Path], half_width: float | None = None) -> float:
    """Minimum distance between any two DIFFERENT (non-touching) traced
    paths, excluding gaps small enough that the thickening step's
    morphological closing already bridges them silently (real junctions
    like a crossbar meeting a stem, which always leave a tiny 1-3px gap
    from skeleton-tracing discretization -- not a genuine crowding
    problem). Pass half_width to size that exclusion correctly for the
    current stroke weight; without it, a conservative fixed value is used.
    """
    lines = [LineString(p) for p in paths if len(p) >= 2]
    if len(lines) < 2:
        return float("inf")
    junction_tol = 2 * max(2.0, (half_width or 20.0) * 0.15)
    best = float("inf")
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            d = lines[i].distance(lines[j])
            if d < junction_tol:
                continue  # legitimate junction, already handled by closing
            best = min(best, d)
    return best


# --- Edit operations -----------------------------------------------------
# Each takes (paths, path_index, ...) and returns a NEW paths list.
# Kept as small, composable, pure functions -- these are exactly the
# primitives a GUI edit tool (move / flatten / offset) calls, and the
# same primitives a saved "recipe" step replays non-interactively.

def move_point(paths: list[Path], index: int, point_index: int,
                new_x: float, new_y: float) -> list[Path]:
    out = [list(p) for p in paths]
    out[index][point_index] = (new_x, new_y)
    return out


def delete_path(paths: list[Path], index: int) -> list[Path]:
    return [p for i, p in enumerate(paths) if i != index]


def add_path(paths: list[Path], new_path: Path) -> list[Path]:
    return list(paths) + [list(new_path)]


def flatten(paths: list[Path], indices: list[int], scale: float, anchor: str = "min_y",
            bounds: tuple[float, float, float, float] | None = None
            ) -> list[Path]:
    """Scale a GROUP of paths' variation around a SHARED anchor
    coordinate, computed across all of them together -- so a mark
    traced as several separate polyline pieces (e.g. a multi-stroke
    accent) compresses as one visual unit, not each piece toward its
    own local extreme. A single-element indices list reduces to
    exactly the original single-path behavior.

    anchor="min_y": keep the topmost point (across the whole group)
    fixed, compress everything else toward it (e.g. flattening a
    tilde's amplitude while keeping its top aligned with where it
    originally sat).
    anchor="max_y" / "min_x" / "max_x": same idea, other axis/direction.

    bounds=(x0, y0, x1, y1), if given, clamps every resulting point to
    it -- needed once scale could exceed 1 (Stretch, not just Flatten):
    unlike ordinary point-dragging, which was already bounded to the
    glyph's own working area from the start, a stretched point had
    nothing stopping it from landing well outside it. Clamping HERE,
    inside the actual transform, rather than as a separate post-
    processing step some callers remember to apply and others don't,
    is what keeps every caller consistent -- in particular, an
    EditStep with op="flatten" is replayed through this exact function
    by the generic edit-dispatch used for undo/redo and reloading a
    glyph, with no separate clamping step of its own; the bounds this
    was committed with are stored in that EditStep's own args (see
    MainWindow._commit_flatten) specifically so a later replay
    reproduces the SAME clamped result, not a silently different,
    unclamped one."""
    out = list(paths)
    all_points = [pt for i in indices for pt in out[i]]
    if not all_points:
        return out
    if anchor in ("min_y", "max_y"):
        ref = min(y for _x, y in all_points) if anchor == "min_y" else max(y for _x, y in all_points)
        for i in indices:
            out[i] = [(x, ref + (y - ref) * scale) for x, y in out[i]]
    else:
        ref = min(x for x, _y in all_points) if anchor == "min_x" else max(x for x, _y in all_points)
        for i in indices:
            out[i] = [(ref + (x - ref) * scale, y) for x, y in out[i]]
    if bounds is not None:
        x0, y0, x1, y1 = bounds
        for i in indices:
            out[i] = [(min(max(x, x0), x1), min(max(y, y0), y1)) for x, y in out[i]]
    return out


def flatten_xy(paths: list[Path], indices: list[int], x_scale: float, y_scale: float,
                x_anchor: str = "min_x", y_anchor: str = "min_y",
                bounds: tuple[float, float, float, float] | None = None
                ) -> list[Path]:
    """Independent X and Y scaling in one combined step, for the Size
    panel's own two sliders -- composes two calls to flatten() itself
    rather than reimplementing the transform: X and Y are fully
    independent under that function's own linear formula (an X-anchor
    call never touches Y coordinates or vice versa), so chaining them
    produces exactly the same result a single combined pass would,
    with the well-tested single-axis code left completely untouched.
    bounds is applied only on the second (final) call, not both --
    clamping is idempotent, so doing it twice would be correct but
    wasted work, since the first call's own clamp pass would just be
    redone identically by the second."""
    out = flatten(paths, indices, scale=x_scale, anchor=x_anchor, bounds=None)
    out = flatten(out, indices, scale=y_scale, anchor=y_anchor, bounds=bounds)
    return out


def offset_part(paths: list[Path], indices: list[int], dx: float, dy: float
                 ) -> list[Path]:
    """Shift a GROUP of paths together as a rigid unit -- important
    whenever a single visual element (e.g. a multi-stroke digit) is
    traced as several separate polylines that must move as one, not
    independently (see the u00bc '4' regression from this project)."""
    out = list(paths)
    for i in indices:
        out[i] = [(x + dx, y + dy) for x, y in out[i]]
    return out


def break_path(paths: list[Path], index: int, point_index: int) -> list[Path]:
    """Split a polyline into two independent paths at point_index (which
    ends up as the shared/duplicated endpoint of both halves). Lets a
    single segment of a multi-point polyline be moved or offset on its
    own, since offset_part otherwise acts on the whole thing.
    A path with only 2 points (already a single segment) is returned
    unchanged -- nothing to break."""
    out = list(paths)
    p = out[index]
    if point_index <= 0 or point_index >= len(p) - 1:
        return out   # breaking at an endpoint would just yield an empty half
    first_half = p[:point_index + 1]
    second_half = p[point_index:]
    out[index] = first_half
    out.insert(index + 1, second_half)
    return out


def find_join_chain(paths: list[Path], distinct_indices: list[int],
                     tolerance: float = 5.0) -> list[int] | None:
    """Checks whether the GIVEN paths -- considered only among
    themselves, nothing else in the glyph -- form exactly one valid,
    unbranched chain end-to-end, using a tight coincidence tolerance
    ("indistinctly" the same point, not a loose nearby-enough guess).

    Deliberately does NOT look at any other path in the glyph, even
    one sitting at the exact same point. An earlier version tried to
    detect risky merges by checking ambiguity against the WHOLE glyph,
    which produced confusing, hard-to-predict results -- a pair the
    user explicitly selected together would sometimes not join because
    of some OTHER, unselected path elsewhere entirely. The model here
    is deliberately simple: the user's own selection is the only input
    that matters. Two selected paths whose closest endpoints coincide
    join, full stop, regardless of what else happens to sit nearby; if
    they don't coincide, or a larger selection doesn't resolve to one
    single chain, the whole selection groups instead (see
    MainWindow._on_join_clicked).

    Returns the ordered sequence of path indices (consecutive entries
    share a coincident endpoint) if a valid chain exists, or None if
    it doesn't -- a branch (3+ path-ends meeting at one point within
    the selection), a gap anywhere, or more than one separate cluster
    all count as "no"."""
    if len(distinct_indices) < 2:
        return None
    valid = [pi for pi in distinct_indices if 0 <= pi < len(paths) and paths[pi]]
    if len(valid) != len(distinct_indices):
        return None

    # Each path contributes exactly 2 consecutive entries here: path k
    # (in `valid`) is endpoints[2k] (start) and endpoints[2k+1] (end).
    endpoints: list[tuple[int, str, float, float]] = []
    for pi in valid:
        p = paths[pi]
        endpoints.append((pi, "start", p[0][0], p[0][1]))
        endpoints.append((pi, "end", p[-1][0], p[-1][1]))

    n = len(endpoints)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        xi, yi = endpoints[i][2], endpoints[i][3]
        for j in range(i + 1, n):
            xj, yj = endpoints[j][2], endpoints[j][3]
            if ((xi - xj) ** 2 + (yi - yj) ** 2) ** 0.5 <= tolerance:
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    # A valid simple chain: every node (cluster) has degree 1 or 2
    # (never 3+, which is a branch within the selection itself), and
    # either exactly two nodes have degree 1 (an open chain, with its
    # own two true ends) or zero do (a CLOSED loop -- e.g. breaking a
    # closed contour like "O" or "o" into pieces and selecting them
    # all back still forms a closed shape, not an open-ended one;
    # confirmed as a real gap when this only accepted open chains --
    # every closed-contour glyph tested failed to reconnect). Any
    # other count of degree-1 nodes means disconnected fragments.
    degree = {node: len(members) for node, members in clusters.items()}
    if any(d > 2 for d in degree.values()):
        return None
    degree1_nodes = [node for node, d in degree.items() if d == 1]
    is_closed_loop = len(degree1_nodes) == 0
    if not is_closed_loop and len(degree1_nodes) != 2:
        return None

    # Walk the chain from one true end to the other (or, for a closed
    # loop, from an arbitrary starting node all the way around),
    # collecting path order as we go.
    def other_endpoint_index(ep_idx: int) -> int:
        k = ep_idx // 2
        return ep_idx + 1 if ep_idx == 2 * k else ep_idx - 1

    visited_paths: set[int] = set()
    chain_order: list[int] = []
    start_node = find(0) if is_closed_loop else degree1_nodes[0]
    current_node = start_node
    while len(visited_paths) < len(valid):
        next_ep = None
        for ep_idx in clusters[current_node]:
            pi = endpoints[ep_idx][0]
            if pi not in visited_paths:
                next_ep = ep_idx
                break
        if next_ep is None:
            return None   # shouldn't happen given the degree checks above, but don't guess
        pi = endpoints[next_ep][0]
        chain_order.append(pi)
        visited_paths.add(pi)
        current_node = find(other_endpoint_index(next_ep))
    expected_end = start_node if is_closed_loop else degree1_nodes[1]
    if current_node != expected_end:
        return None   # sanity check -- should always hold given the invariants above
    return chain_order


def join_paths(paths: list[Path], index_a: int, index_b: int) -> list[Path]:
    """Merge two paths into one polyline by connecting whichever pair of
    their endpoints is closest together. The reverse of break_path --
    reassembles a segment that was split (or joins two originally-
    separate pieces) back into a single editable polyline."""
    out = list(paths)
    if index_a == index_b or not (0 <= index_a < len(out)) or not (0 <= index_b < len(out)):
        return out
    a, b = out[index_a], out[index_b]

    def dist(p, q):
        return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5

    options = [
        (dist(a[-1], b[0]), a + b[1:]),
        (dist(a[-1], b[-1]), a + list(reversed(b))[1:]),
        (dist(a[0], b[-1]), b + a[1:]),
        (dist(a[0], b[0]), list(reversed(b)) + a[1:]),
    ]
    _best_d, merged = min(options, key=lambda o: o[0])

    lo, hi = sorted((index_a, index_b))
    out[lo] = merged
    del out[hi]
    return out


def scale_uniform(paths: list[Path], factor: float, origin: tuple[float, float] = (0.0, 0.0)
                   ) -> list[Path]:
    """Uniformly scales every point in every path by `factor`, relative
    to a fixed `origin` (which stays put; everything else moves toward
    or away from it proportionally).

    Used for "resize to fit" when a single-glyph PNG import exceeds the
    project's canvas size in either dimension: applied to the ALREADY-
    TRACED skeleton, not the source bitmap, so shrinking to fit doesn't
    cost any tracing precision the way pre-downscaling the raw image
    before tracing would."""
    ox, oy = origin
    return [[(ox + (x - ox) * factor, oy + (y - oy) * factor) for x, y in path] for path in paths]


