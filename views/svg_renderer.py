"""
An SVG "org card" for a single ship or colony: what a captain looks at to
answer "what does my ship look like right now" without re-deriving it from a
raw show_organization() response.

Split in two on purpose. layout_org_card() computes geometry and returns a
list of marks -- plain dicts carrying numbers and strings, no markup anywhere.
emit_svg() turns marks into an SVG document. Anything that computes marks some
other way reuses the emitter; anything that draws marks some other way (a
rasterizer, HTML/CSS, Block Kit) reuses the layout.

Pure functions over the dict show_organization()
(xsettlers_mcp/tools/organization_reports.py) already returns -- no DB access
and no engine imports, which is what keeps views/ a leaf. Feed it a dict of
that shape from anywhere: a live call, a fixture, a captured JSON blob.

Two things are shown, and they answer different questions. Five *tasking* bars
say what the pods are assigned to -- idle first, then the three producing
tasks, then scan -- each out of the org's total pod count. One composite
*storage* bar says what the hold contains: energy, food and goods as coloured
segments of total capacity, with the empty tail showing headroom. A legend
under it carries the figures the segments are too small to.

All five tasking bars draw whether or not any pod is on that task, so the card
is a fixed shape a captain can compare against itself turn to turn -- and idle
is always visible, in an alarm colour the moment a pod is on it.

Storage sums across every pod, not just the matching produce_<resource> group:
storage is generic per pod and independent of task (see show_organization's
docstring), so a produce_goods pod can still be sitting on energy left over
from before a retask. An org spends its pods as one purse
(engine/org_resources.py), which is why one bar tells the truth about it.
"""
from xml.sax.saxutils import escape

CARD_WIDTH = 260
_MARGIN = 20
_HEADER_HEIGHT = 112
_BAR_HEIGHT = 18
_BAR_ROW_HEIGHT = 34      # one label + one bar + gutter to the next bar
_SECTION_GAP = 18         # between the tasking bars and the storage bar
_LEGEND_ROW_HEIGHT = 20   # the figures under the storage bar
_SCANNER_ROW_HEIGHT = 24
_FOOTER_PAD = 20
_ERROR_HEIGHT = 100

_BG = "#161b2c"
_BORDER = "#2e3757"
_TEXT = "#eef1fa"
_MUTED = "#93a0c2"
_DIVIDER = "#2e3757"
_TRACK = "#232b45"
_DOT_DOCKED = "#22c55e"
_DOT_TRANSIT = "#f59e0b"
_NEUTRAL = "#7c8bb0"

_CATEGORY_ORDER = ("energy", "food", "goods")
_CATEGORY_LABEL = {"energy": "Energy", "food": "Food", "goods": "Goods"}
_CATEGORY_COLOR = {"energy": "#3b82f6", "food": "#4ade80", "goods": "#c2743a"}

# Idle leads: it is the one row a captain has to act on. Scan trails the
# producing tasks -- instrumentation, not output.
_TASK_ORDER = ("idle", "produce_energy", "produce_food", "produce_goods", "scan")
_TASK_LABEL = {"idle": "Idle", "scan": "Scan",
               **{f"produce_{r}": _CATEGORY_LABEL[r] for r in _CATEGORY_ORDER}}
_TASK_COLOR = {"idle": _NEUTRAL, "scan": "#c0c0c8",
               **{f"produce_{r}": _CATEGORY_COLOR[r] for r in _CATEGORY_ORDER}}

# Idle is drawn in this instead the moment any pod is on it: those pods are
# paid for and producing nothing.
_ALARM = "#ef4444"

_FONT_STACK = "system-ui, sans-serif"

# Icon geometry at the origin; layout translates it into place.
_SHIP_POINTS = ((8, 1), (14, 15), (8, 11), (2, 15))
_COLONY_ROOF = ((1, 7), (8, 1), (15, 7))
_COLONY_BODY = (2, 7, 12, 8)      # x, y, w, h


ICON_BOX = 16     # the box the outlines above are drawn on


def icon_marks(org_type: str, x: float, y: float, color: str,
               size: float = ICON_BOX) -> list:
    """
    The org-type glyph, its top-left at (x, y) and scaled to `size`. A ship is
    a chevron, a colony a roofed block.

    Public and sized because the neighborhood map draws the same two shapes at
    8-18px for a lattice node. One ship outline, one colony outline, whoever is
    drawing -- a shape duplicated per caller is a shape that drifts, and these
    two carry meaning ("this is a colony") rather than decoration.
    """
    k = size / ICON_BOX
    if org_type == "ship":
        return [{"kind": "polygon", "fill": color,
                 "points": [(x + px * k, y + py * k) for px, py in _SHIP_POINTS]}]
    bx, by, bw, bh = _COLONY_BODY
    return [
        {"kind": "rect", "x": x + bx * k, "y": y + by * k,
         "w": bw * k, "h": bh * k, "fill": color},
        {"kind": "polygon", "fill": color,
         "points": [(x + px * k, y + py * k) for px, py in _COLONY_ROOF]},
    ]


def centered_icon_marks(org_type: str, cx: float, cy: float, size: float,
                        color: str) -> list:
    """icon_marks placed by its centre rather than its corner -- what a map
    node wants, since a lattice knows where the middle of a cell is."""
    return icon_marks(org_type, cx - size / 2, cy - size / 2, color, size)


def _bar_marks(x: int, y: int, width: int, fraction: float, color: str,
               label: str, readout: str) -> list:
    """Track + one solid fill + a label above and a centered readout inside --
    the shared shape both the tasking and storage bars draw with."""
    fill_width = max(0.0, min(width, width * fraction))
    return [
        {"kind": "text", "x": x, "y": y - 5, "s": label, "size": 11, "fill": _MUTED},
        {"kind": "rect", "x": x, "y": y, "w": width, "h": _BAR_HEIGHT,
         "rx": 4, "fill": _TRACK},
        {"kind": "rect", "x": x, "y": y, "w": fill_width, "h": _BAR_HEIGHT,
         "rx": 4, "fill": color},
        {"kind": "text", "x": x + width / 2, "y": y + _BAR_HEIGHT - 5, "s": readout,
         "size": 11, "weight": 600, "fill": _TEXT, "anchor": "middle"},
    ]


def _pod_totals(tasks: list) -> tuple:
    """(total pods on the org, total storage capacity across every pod) -- the
    denominator both bars in a category are drawn against. Both are fixed for
    the life of a game: pods are created once at bootstrap and storage_capacity
    is never updated."""
    total_pods = sum(t.get("count") or 0 for t in tasks)
    total_capacity = sum(t.get("capacity") or 0 for t in tasks)
    return total_pods, total_capacity


def _tasking_marks(x: int, y: int, width: int, counts: dict,
                   total_pods: int) -> tuple:
    """One bar per task in _TASK_ORDER, drawn whether or not a pod is on it, so
    the card keeps the same shape as pods retask. Returns (marks, new_y)."""
    marks = []
    for task in _TASK_ORDER:
        count = counts.get(task, 0)
        color = _ALARM if task == "idle" and count else _TASK_COLOR[task]
        marks += _bar_marks(x, y, width, count / total_pods if total_pods else 0,
                            color, _TASK_LABEL[task], f"{count}/{total_pods} pods")
        y += _BAR_ROW_HEIGHT
    return marks, y


def _storage_marks(x: int, y: int, width: int, stored: dict,
                   total_capacity: float) -> tuple:
    """
    The hold as one bar: energy, food and goods as segments of total capacity,
    the tail left empty so headroom is visible. Returns (marks, new_y).

    Segments are painted left edge to used edge in category order, each over
    the last, so the bar reads as one rounded shape with crisp internal
    boundaries rather than a row of separate rounded pills.
    """
    used = sum(stored.values())
    scale = width / total_capacity if total_capacity else 0
    used_end = x + used * scale

    marks = [
        {"kind": "text", "x": x, "y": y - 5, "s": "Storage", "size": 11,
         "fill": _MUTED},
        {"kind": "text", "x": x + width, "y": y - 5, "size": 11, "fill": _MUTED,
         "s": f"{used:.0f}/{total_capacity:.0f}", "anchor": "end"},
        {"kind": "rect", "x": x, "y": y, "w": width, "h": _BAR_HEIGHT,
         "rx": 4, "fill": _TRACK},
    ]
    offset = 0.0
    for category in _CATEGORY_ORDER:
        amount = stored.get(category) or 0
        if amount <= 0:
            continue
        seg_x = x + offset * scale
        marks.append({"kind": "rect", "x": seg_x, "y": y, "w": used_end - seg_x,
                      "h": _BAR_HEIGHT, "rx": 4, "fill": _CATEGORY_COLOR[category]})
        offset += amount
    y += _BAR_ROW_HEIGHT

    # Four evenly spaced entries: the three categories, then what is left.
    step = width / 4
    for i, category in enumerate(_CATEGORY_ORDER):
        marks.append({"kind": "circle", "cx": x + i * step + 3, "cy": y - 4, "r": 3,
                      "fill": _CATEGORY_COLOR[category]})
        marks.append({"kind": "text", "x": x + i * step + 11, "y": y,
                      "s": f"{stored.get(category) or 0:.0f}", "size": 11,
                      "fill": _TEXT})
    marks.append({"kind": "text", "x": x + 3 * step, "y": y, "size": 11,
                  "fill": _MUTED, "s": f"free {total_capacity - used:.0f}"})
    return marks, y + _LEGEND_ROW_HEIGHT


def layout_org_card(data: dict) -> tuple:
    """
    Compute one show_organization() result into (marks, dims).

    Raises KeyError if `data` doesn't have that shape -- deliberately not
    defensive, since a malformed card is a bug in whoever produced the data,
    not something to paper over with a blank card.
    """
    if "error" in data:
        return _error_marks(data["error"])

    tasks = data.get("tasks") or []
    scanners = data.get("scanners") or []
    total_pods, total_capacity = _pod_totals(tasks)
    # GROUP BY task only returns the groups that exist; every task in
    # _TASK_ORDER draws regardless, so absent ones read zero rather than
    # vanishing and shifting the rows below them.
    counts = {t.get("task"): t.get("count") or 0 for t in tasks}
    stored = {c: sum(t.get(c) or 0 for t in tasks) for c in _CATEGORY_ORDER}

    docked = data.get("sector_id") != -1
    name = data.get("short_name") or data.get("name") or ""

    marks = []
    bar_width = CARD_WIDTH - 2 * _MARGIN
    y = _HEADER_HEIGHT
    if tasks:
        block, y = _tasking_marks(_MARGIN, y, bar_width, counts, total_pods)
        marks += block
        y += _SECTION_GAP
        block, y = _storage_marks(_MARGIN, y, bar_width, stored, total_capacity)
        marks += block
    else:
        marks.append({"kind": "text", "x": _MARGIN, "y": y, "s": "(no pods)",
                      "size": 12, "fill": _MUTED})
        y += _BAR_ROW_HEIGHT

    if scanners:
        bearings = ", ".join(s["bearing"] or "unaimed" for s in scanners)
        marks.append({"kind": "text", "x": _MARGIN, "y": y + 6,
                      "s": f"Scanning: {bearings}", "size": 12, "fill": _MUTED})
        y += _SCANNER_ROW_HEIGHT

    height = y + _FOOTER_PAD
    header = [
        _frame(height),
        *icon_marks(data.get("org_type"), _MARGIN, 20, _TEXT),
        {"kind": "text", "x": _MARGIN + 22, "y": 32, "s": name, "size": 17,
         "weight": 700, "fill": _TEXT},
        {"kind": "circle", "cx": CARD_WIDTH - _MARGIN - 6, "cy": 26, "r": 6,
         "fill": _DOT_DOCKED if docked else _DOT_TRANSIT},
        {"kind": "text", "x": _MARGIN, "y": 56, "s": data.get("status") or "",
         "size": 13, "fill": _MUTED},
        {"kind": "text", "x": _MARGIN, "y": 76,
         "s": f"Mission: {data.get('mission') or ''}", "size": 13, "fill": _MUTED},
        {"kind": "line", "x1": _MARGIN, "y1": 92, "x2": CARD_WIDTH - _MARGIN,
         "y2": 92, "stroke": _DIVIDER, "stroke_width": 1},
    ]
    return header + marks, {"width": CARD_WIDTH, "height": height}


def _frame(height: int) -> dict:
    """The rounded card body every card sits inside."""
    return {"kind": "rect", "x": 1, "y": 1, "w": CARD_WIDTH - 2, "h": height - 2,
            "rx": 16, "fill": _BG, "stroke": _BORDER, "stroke_width": 2}


def _error_marks(message: str) -> tuple:
    """The one shape show_organization can return besides a real org:
    {"error": ...} on a bad or unowned org_id."""
    return [
        _frame(_ERROR_HEIGHT),
        {"kind": "text", "x": _MARGIN, "y": _ERROR_HEIGHT / 2, "s": message,
         "size": 13, "fill": _MUTED},
    ], {"width": CARD_WIDTH, "height": _ERROR_HEIGHT}


def _num(value) -> str:
    """Numbers as short as they read: 20 not 20.0, 12.5 kept."""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.1f}"
    return str(value)


def _attrs(pairs) -> str:
    return "".join(f' {k}="{v}"' for k, v in pairs if v is not None)


def emit_svg(marks: list, dims: dict) -> str:
    """
    Draw a mark list as a complete standalone SVG document (string).

    One branch per mark kind and nothing else: no geometry, no knowledge of
    what a card is. Text is escaped here, so a mark carries a plain string.
    """
    width, height = dims["width"], dims["height"]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" '
           f'viewBox="0 0 {_num(width)} {_num(height)}" width="{_num(width)}" '
           f'height="{_num(height)}" font-family="{_FONT_STACK}">']
    for m in marks:
        kind = m["kind"]
        if kind == "rect":
            out.append("<rect" + _attrs([
                ("x", _num(m["x"])), ("y", _num(m["y"])),
                ("width", _num(m["w"])), ("height", _num(m["h"])),
                ("rx", _num(m["rx"]) if "rx" in m else None),
                ("fill", m["fill"]), ("stroke", m.get("stroke")),
                ("stroke-width", _num(m["stroke_width"]) if "stroke_width" in m else None),
            ]) + "/>")
        elif kind == "text":
            out.append("<text" + _attrs([
                ("x", _num(m["x"])), ("y", _num(m["y"])),
                ("font-size", _num(m["size"])),
                ("font-weight", _num(m["weight"]) if "weight" in m else None),
                ("fill", m["fill"]),
                ("text-anchor", m.get("anchor")),
            ]) + f">{escape(m['s'])}</text>")
        elif kind == "circle":
            out.append("<circle" + _attrs([
                ("cx", _num(m["cx"])), ("cy", _num(m["cy"])),
                ("r", _num(m["r"])), ("fill", m["fill"]),
            ]) + "/>")
        elif kind == "polygon":
            points = " ".join(f"{_num(px)},{_num(py)}" for px, py in m["points"])
            out.append(f'<polygon points="{points}" fill="{m["fill"]}"/>')
        elif kind == "line":
            out.append("<line" + _attrs([
                ("x1", _num(m["x1"])), ("y1", _num(m["y1"])),
                ("x2", _num(m["x2"])), ("y2", _num(m["y2"])),
                ("stroke", m["stroke"]),
                ("stroke-width", _num(m["stroke_width"])),
            ]) + "/>")
        else:
            raise ValueError(f"unknown mark kind: {kind!r}")
    out.append("</svg>")
    return "\n".join(out)


def render_org_card_svg(data: dict) -> str:
    """One show_organization() result as a standalone SVG document."""
    return emit_svg(*layout_org_card(data))
