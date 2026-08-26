"""
A graphic neighborhood map -- the SVG counterpart to views/render.py's
`render_map()`, and a sibling of `layout_org_card`.

Not a replacement for the markdown table. That is what a text client gets, and
it is what this falls back to at every tier. Both are drawn from the dict
`show_sector_neighborhood()` already returns, and neither imports the engine or
touches a DB.

**One lattice, three channels.** `sector_tools._draw_grid` exists so the two
markdown maps cannot come to disagree about what "the neighborhood" is. This
keeps that promise in the graphic: frame, range disc, axes, centre mark and
the never-seen dots are drawn once by shared code, and the channel decides
only what a *known* node says. Adding a fourth channel is a colour ramp and a
glyph, never a second map.

The channel arrives in `display.channel` rather than as an argument, because
`server.py` dispatches SVG as `renderer(result)` -- one callable per tool, one
argument. So `show_sector_neighborhood(channel=...)` is how a client asks, the
same way it asks for a radius.

**Every engine number is read from `display.scales`, never restated here.**
Sector energy bounds and the confidence decay rate belong to `db/sectors.py`,
this module cannot import it -- `views/` reaching into `db/` is what stops a
rasterizer or a Block Kit consumer using a layout -- and a copy of a tunable
is a copy that goes stale silently. `../xsettlers-designer` exists to tune
exactly those numbers. Constants below are fallbacks for a payload predating
the scales block, not a second source of truth.

Where the numbers come from:
  * the **lattice** -- which cells are in range, and their coordinates -- is
    read off `display.grid`, because the tool already decided it and a second
    Euclidean test here is a second place to be wrong;
  * the **magnitudes** come off the structured `sectors` rows, because a
    heat ramp needs `energy_capacity`, not the string "2.20".

So `display.grid` stays the markdown contract and the graphic re-renders
beside it rather than parsing it.
"""
import math
import random

from views.format import in_thousands
from views.svg_renderer import centered_icon_marks, emit_svg

# --- Palette -----------------------------------------------------------------
# Descended from views/svg_renderer.py's card palette, one step darker: a card
# is a lit panel, this is the sky behind it. Anything a captain sees in both
# places (own orgs, the alarm colour, the scan grey) is the same hex in both.
_SPACE = "#080b14"        # outside the range disc -- the void
_FIELD = "#0e1424"        # inside it -- your neighborhood, faintly lit
_BORDER = "#2e3757"       # card border, unchanged
_TEXT = "#eef1fa"
_MUTED = "#93a0c2"
_DIM = "#5a678d"
_AXIS = "#3d4970"

_UNKNOWN = "#39456b"      # in range, never seen -- the scan-me list
_SEEN_EMPTY = "#66759e"   # seen, and nothing of anyone's there
_OWN = _TEXT              # your orgs, in the same white the card draws them
_OWN_RING = "#69759b"     # the occupancy ring: present, and quieter than its sigil
_COUNT_INK = "#c3cce2"    # the count is a readout, so it reads
# A rival is drawn with the SAME sigil as one of your own units -- a chevron
# for a ship, a roofed block for a colony -- in red instead of white. Shape
# says what it is; colour says whose; brightness and size say how long ago you
# looked. That is one vocabulary for every unit on the map, where a ring was a
# second one that only rivals spoke.
_RIVAL = "#ff4d4d"        # just seen: hot, and meant to be
_RIVAL_COLD = "#5c262e"   # last look: a bulb about to go out
_RIVAL_RING = "#7c4048"   # the rival ring is chrome, not data -- it does not fade
_RIVAL_COUNT = "#f0a8ac"  # nor does the count riding on it
_SCAN = "#c0c0c8"         # card scan colour
_BEAM = "#5d6c8c"
# Energy ramp endpoints. Open space rolls 500..1000 (db/sectors.py), so the
# COLOUR ramp spans exactly that and saturates there. Area does not saturate:
# it keeps growing past the ceiling, which is how a sector richer than open
# space can roll shows up as the biggest, brightest thing in view.
#
# It shows up, and it is never named. "Hotspot" is a scenario-authoring
# concept (config/loader.HotspotDef), not a thing a player is told about --
# they infer where the good ground is from their own scan results, which is
# the discovery the mechanic exists to create. A badge on the map would hand
# them the answer and make the inference pointless. So: no halo, no legend
# term, no vocabulary. Just a star that is obviously bigger.
# Fallbacks only, for a payload predating display.scales. The live values are
# db/sectors.MIN_SECTOR_ENERGY and MAX_SECTOR_ENERGY, and they arrive as data.
_ENERGY_FLOOR = 500.0
_ENERGY_CEIL = 1000.0
_CONFIDENCE_MAX = 100.0
_RAMP_LOW = "#24406e"
_RAMP_HIGH = "#dcecff"

_FONT_STACK = "system-ui, sans-serif"

# --- Geometry ----------------------------------------------------------------
# A cell is a fixed size and the canvas grows to hold the lattice, rather than
# a fixed canvas squeezing whatever lattice it is given.
#
# That is the whole responsive model: a phone asks for radius 4 and gets a
# 9x9 map at full size; a desktop asks for radius 8 or 10 and gets *more
# neighborhood* at the same cell size, not the same neighborhood enlarged.
# Radius is a tool parameter already, so the client picks it from the viewport
# and the server draws it -- nothing here has to guess how wide the screen is.
#
# 34px is sized off the tightest case: radius 4 on a 360px phone viewport, the
# 9x9 map that has to work first.
TARGET_CELL = 34
PHONE_RADIUS = 4          # what a 360px viewport should ask for

# Kept because a caller can still pin a width (see _Field), which is what the
# legibility ladder below is for. It is a fallback now, not the plan.
MAP_WIDTH = 360
_MARGIN = 16
_Y_GUTTER = 20            # left strip, for the y coordinate labels
_X_STRIP = 15             # top strip, for the x coordinate labels
_HEADER_H = 58
_LEGEND_ROW_H = 16
_FOOTER_H = 18
_PAD = 14

# The legibility ladder. A 9x9 map has room for a figure in every cell; a 21x21
# map on the same phone does not, and no amount of scaling invents the space.
# So detail drops out in tiers as the cell shrinks, and the detail table under
# the map -- which every markdown client already gets -- carries what falls off.
_TIER_FULL = 30           # >= this: numerals in cells
_TIER_GLYPH = 17          # >= this: glyphs, no numerals
# below _TIER_GLYPH: dots only


def _tier(cell: float) -> str:
    if cell >= _TIER_FULL:
        return "full"
    return "glyph" if cell >= _TIER_GLYPH else "dot"


# --- Small helpers -----------------------------------------------------------

def _scale(data: dict, name: str, key: str, fallback):
    """One number off `display.scales`, or the fallback when the payload
    predates the block. Not defensiveness: a captured JSON blob is a supported
    input to every renderer here (see the module docstring on views/ being a
    leaf), and an old capture should still draw."""
    scales = (data.get("display") or {}).get("scales") or {}
    value = (scales.get(name) or {}).get(key)
    return fallback if value is None else value


def _lerp_hex(low: str, high: str, t: float) -> str:
    """Blend two #rrggbb colours. Marks carry no opacity (see emit_svg), so
    every shade a channel wants is mixed here and arrives as a flat fill."""
    t = max(0.0, min(1.0, t))
    pair = [(int(low[i:i + 2], 16), int(high[i:i + 2], 16)) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(a + (b - a) * t):02x}" for a, b in pair)


# Intel decays 20 points a turn, so a sector goes unconfirmed and blinks out on
# the fifth turn (db/sectors.CONFIDENCE_DECAY_PER_TURN). Confidence is
# therefore never a continuum in practice -- it is exactly five drawn states,
# 100 / 80 / 60 / 40 / 20, and then the sector is gone from the map entirely.
#
# So the ramp is tuned across those five and no further. 20 is not "almost
# invisible": it is the last look you will ever get at that sector, and it has
# to still read. What shrinks and cools between 100 and 20 is the *claim*, not
# the visibility -- a hot, full-size sigil means eyes on you right now; a small
# cold one means someone was there and you have stopped being able to tell.
_RIVAL_SIZE_FLOOR = 0.27      # of a cell, at confidence 20
_RIVAL_SIZE_FRESH = 0.54      # of a cell, at confidence 100

# --- Occupancy rings ---------------------------------------------------------
# A ring around an occupied node, in that side's colour, and the occupant count
# riding on it. The ring is the thing the count hangs off -- which is what a
# bare sigil had nowhere to put.
#
# Position around the ring says which side a count belongs to, so the two never
# need a key to tell apart: yours always at half past four, theirs always at
# half past seven. A contested sector draws both rings concentrically, the red
# just inside the white.
_RING_OWN = 0.36              # of a cell
_RING_RIVAL_IN = 0.29         # of a cell, when contested -- inside the white
_COUNT_ORBIT = 0.36           # both counts ride the same radius, so they balance
_COUNT_PAD = 4.0
_OWN_CLOCK = 45.0             # degrees, measured down-right from centre
_RIVAL_CLOCK = 135.0          # down-left


def _count_label(count: int) -> str:
    """
    One character, always: nothing below two, the digit through nine, then "+".

    A sector holding ten organizations and a sector holding forty are the same
    fact at this zoom -- "more than you want to count" -- and the exact number
    is one tap away in the sector detail view. What a fixed single character
    buys is a cell that never changes width, so a column of them stays a column.
    """
    if not count or count < 2:
        return ""
    return str(count) if count <= 9 else "+"


def _count_marks(px: float, py: float, cell: float, count: int, degrees: float,
                 color: str, size: float = 9.5, pad: float = None) -> list:
    """The occupant count, parked on its side's clock position."""
    label = _count_label(count)
    if not label:
        return []
    d = cell * _COUNT_ORBIT + (_COUNT_PAD if pad is None else pad)
    rad = math.radians(degrees)
    # SVG's y grows downward, so a positive sine is already "below centre" --
    # both clock positions are in the lower half and differ only in cosine.
    return [_text(px + d * math.cos(rad), py + d * math.sin(rad) + size * 0.34,
                  label, size, color, anchor="middle")]


def _freshness(confidence, confidence_max: float) -> float:
    """Confidence as 0..1. `confidence_max` comes off display.scales, so the
    top of the ramp is whatever the engine calls certainty."""
    return max(0.0, min(1.0, (confidence or 0) / (confidence_max or 100.0)))


def _rival_marks(org_type: str, px: float, py: float, cell: float,
                 confidence: float, size: float = None,
                 confidence_max: float = _CONFIDENCE_MAX) -> list:
    """One rival unit: your own sigil, in red, walked down its ramp.

    `confidence` covers both sources without branching -- a live rival only
    ever reports at 100 (you are standing in the sector) and a remembered one
    only ever below it, so the same number places both on the same ramp and
    the top of the ramp is exactly "seen just now"."""
    fresh = _freshness(confidence, confidence_max)
    span = _RIVAL_SIZE_FRESH - _RIVAL_SIZE_FLOOR
    return _glyph(org_type or "ship", px, py,
                  size if size is not None else cell * (_RIVAL_SIZE_FLOOR
                                                        + span * fresh),
                  _rival_heat(fresh))


# Size falls off linearly across the five states, but colour falls off on a
# curve: 80 and 60 stay recognisably hot -- they are still current enough to
# act on -- and 40 and 20 drop away fast, so the last look really does read as
# a bulb about to go out rather than a slightly dimmer bulb.
_HEAT_CURVE = 1.45


def _rival_heat(fresh: float) -> str:
    return _lerp_hex(_RIVAL_COLD, _RIVAL, fresh ** _HEAT_CURVE)


def _ring(cx: float, cy: float, r: float, thickness: float, color: str,
          inner: str) -> list:
    """A hollow circle as two filled ones, outer then inner.

    emit_svg's circle branch takes cx/cy/r/fill and nothing else, so there is
    no stroke to draw a ring with. Two marks per ring is the price; `inner` has
    to be whatever is actually behind it, which is why callers pass the ground
    colour rather than this guessing."""
    return [{"kind": "circle", "cx": cx, "cy": cy, "r": r, "fill": color},
            {"kind": "circle", "cx": cx, "cy": cy, "r": max(0.0, r - thickness),
             "fill": inner}]


# One ship outline and one colony outline for the whole codebase: the card
# draws them at 16px, a lattice node at 8-18, and a shape duplicated per caller
# is a shape that drifts. These two carry meaning -- "this is a colony" -- so
# drift would be a map and a card disagreeing about what the player owns.
_glyph = centered_icon_marks


def _text(x, y, s, size, fill, weight=None, anchor=None) -> dict:
    mark = {"kind": "text", "x": x, "y": y, "s": s, "size": size, "fill": fill}
    if weight:
        mark["weight"] = weight
    if anchor:
        mark["anchor"] = anchor
    return mark


# --- The shared field --------------------------------------------------------

class _Field:
    """Where the lattice sits on the canvas, and how to get from a sector
    coordinate to a point on it."""

    def __init__(self, grid: dict, center: dict, radius: int, top: float,
                 width: int = None, scales: dict = None):
        self.x_labels = [int(v) for v in grid["x_labels"]]
        self.y_labels = [int(r["label"]) for r in grid["rows"]]
        self.radius = radius
        self.cx, self.cy = center["x"], center["y"]
        self.left = _MARGIN + _Y_GUTTER
        self.top = top
        cells = max(1, len(self.x_labels))
        if width is None:
            # The normal path: cells keep their size and the card grows.
            self.cell = float(TARGET_CELL)
            self.size = cells * self.cell
            self.width = int(round(self.size + 2 * _MARGIN + _Y_GUTTER))
        else:
            # Pinned width: the lattice is squeezed and the ladder takes over.
            self.width = width
            self.size = width - 2 * _MARGIN - _Y_GUTTER
            self.cell = self.size / cells
        self.tier = _tier(self.cell)
        scales = scales or {}
        self.energy_floor = scales.get("energy_floor", _ENERGY_FLOOR)
        self.energy_ceil = scales.get("energy_ceil", _ENERGY_CEIL)
        self.confidence_max = scales.get("confidence_max", _CONFIDENCE_MAX)

    def point(self, x: int, y: int) -> tuple:
        col = x - self.x_labels[0]
        row = y - self.y_labels[0]
        return (self.left + (col + 0.5) * self.cell,
                self.top + (row + 0.5) * self.cell)

    @property
    def mid(self) -> tuple:
        return self.point(self.cx, self.cy)

    @property
    def disc_r(self) -> float:
        # Enclose the outermost in-range node centre plus half a cell, so the
        # edge reads as the boundary of the cells rather than cutting them.
        return (self.radius + 0.45) * self.cell

    @property
    def bottom(self) -> float:
        return self.top + self.size


def _starfield(field: _Field, seed: tuple) -> list:
    """
    Stars, drawn only where the map carries no data: the corners of the
    bounding square that fall outside the range disc.

    On a map anything that looks like a mark is read as a mark, so scattering
    dim dots among the sector nodes would be inventing sectors. Out beyond the
    disc there is nothing to confuse them with, and filling that band is what
    turns "the blank corners trace the disc" (docs/ui_and_rendering_design.md)
    from a thing a player has to notice into a picture.

    Seeded off the centre coordinate, so the same neighborhood has the same
    sky every turn and the map does not shimmer between refreshes.
    """
    rng = random.Random(hash(seed) & 0xffffffff)
    mx, my = field.mid
    inner = field.disc_r + 3
    marks = []
    for _ in range(70):
        x = field.left + rng.random() * field.size
        y = field.top + rng.random() * field.size
        if (x - mx) ** 2 + (y - my) ** 2 <= inner ** 2:
            continue
        shade = rng.random()
        marks.append({"kind": "circle", "cx": x, "cy": y,
                      "r": 0.5 + shade * 1.0,
                      "fill": _lerp_hex("#141a2c", "#4a5578", shade)})
    return marks


def _axis_marks(field: _Field) -> list:
    """Absolute coordinates on both axes, thinned as the cell shrinks, plus a
    crosshair on the centre's row and column so a node can be traced back to
    its coordinate without counting cells."""
    stride = 1 if field.cell >= 22 else (2 if field.cell >= 14 else 3)
    size = 9 if field.cell >= 20 else 8
    marks = []
    for x in field.x_labels:
        if (x - field.cx) % stride:
            continue
        px, _ = field.point(x, field.y_labels[0])
        marks.append(_text(px, field.top - 4, str(x), size,
                           _MUTED if x == field.cx else _AXIS, anchor="middle"))
    for y in field.y_labels:
        if (y - field.cy) % stride:
            continue
        _, py = field.point(field.x_labels[0], y)
        marks.append(_text(_MARGIN + _Y_GUTTER - 5, py + 3, str(y), size,
                           _MUTED if y == field.cy else _AXIS, anchor="end"))
    mx, my = field.mid
    marks += [
        {"kind": "line", "x1": field.left, "y1": my, "x2": field.left + field.size,
         "y2": my, "stroke": "#1a2138", "stroke_width": 1},
        {"kind": "line", "x1": mx, "y1": field.top, "x2": mx,
         "y2": field.bottom, "stroke": "#1a2138", "stroke_width": 1},
    ]
    return marks


def _ground_marks(field: _Field, seed: tuple) -> list:
    """Everything under the data: the void, the range disc and its edge, the
    stars in the corners, and the axes."""
    mx, my = field.mid
    return [
        {"kind": "rect", "x": field.left, "y": field.top, "w": field.size,
         "h": field.size, "fill": _SPACE},
        *_starfield(field, seed),
        # The Euclidean radius, drawn. Distance is straight-line everywhere in
        # this game (docs/design_direction.md) and the markdown map can only
        # imply it with ragged corners; here it is a circle, and the day the
        # metric changes to Chebyshev this becomes a square and says so.
        {"kind": "circle", "cx": mx, "cy": my, "r": field.disc_r + 1, "fill": _BORDER},
        {"kind": "circle", "cx": mx, "cy": my, "r": field.disc_r, "fill": _FIELD},
        *_axis_marks(field),
    ]


def _unknown_marks(field: _Field, known: set) -> list:
    """A dot for every in-range cell nobody has ever seen. Small and cool: it
    is an absence, and it must not out-shout a sector that is really there."""
    marks = []
    r2 = field.radius ** 2
    for y in field.y_labels:
        for x in field.x_labels:
            if (x - field.cx) ** 2 + (y - field.cy) ** 2 > r2 or (x, y) in known:
                continue
            px, py = field.point(x, y)
            marks.append({"kind": "circle", "cx": px, "cy": py,
                          "r": max(0.9, field.cell * 0.055), "fill": _UNKNOWN})
    return marks


def _center_marks(field: _Field) -> list:
    """The sector the view is centred on, marked in every channel -- it is the
    one node whose meaning does not depend on which question is being asked."""
    mx, my = field.mid
    return _ring(mx, my, field.cell * 0.46, 1.2, _DIM, _FIELD)


# --- The three channels ------------------------------------------------------
# Each takes the field and the sector rows and returns (marks, legend). They
# share every helper above and touch nothing outside their own node.

def _energy_channel(field: _Field, sectors: list, aims: dict) -> tuple:
    """
    A sector's energy as a star's brightness and size.

    This is the one encoding the starfield actually earns: energy is the only
    thing a sector yields (db/sectors.py), and a richer sector reading as a
    brighter star needs no legend to be understood, only to be quantified.

    The ramp spans open space's roll, 500..1000, and stops there. A hotspot
    sector rolls above that whole band by construction -- "a set of places
    that are better, not a set of places where the gamble pays more often" --
    so it takes the top of the ramp plus a halo rather than a bluer blue. A
    sector drawn below the floor by harvesting clamps at the bottom, where it
    reads as the dim thing it now is.
    """
    marks = []
    span = (field.energy_ceil - field.energy_floor) or 1.0
    for s in sectors:
        px, py = field.point(s["coord_x"], s["coord_y"])
        energy = s["energy_capacity"]
        u = (energy - field.energy_floor) / span
        # Area, not radius, tracks the value -- a disc twice as wide reads as
        # four times as much, which is not what the number says. The second
        # term only engages above open space's ceiling, and is damped so a x3
        # region reads as clearly larger without swallowing its neighbours.
        r = field.cell * min(0.44, 0.13 + 0.19 * max(0.0, min(1.0, u)) ** 0.5
                             + 0.09 * max(0.0, u - 1.0) ** 0.5)
        marks.append({"kind": "circle", "cx": px, "cy": py, "r": r,
                      "fill": _lerp_hex(_RAMP_LOW, _RAMP_HIGH,
                                        max(0.0, min(1.0, u)))})
        if field.tier == "full":
            # Below the dot's own edge, not a fixed offset -- the dot's radius
            # is the encoding, so a fixed offset puts the figure inside the
            # brightest sectors and clear of the dimmest. The centre clears its
            # marker ring instead, which is wider than any dot.
            is_center = (s["coord_x"], s["coord_y"]) == (field.cx, field.cy)
            clearance = max(r, field.cell * 0.47) if is_center else r
            marks.append(_text(px, py + clearance + 8, s["energy_display"],
                               8, _MUTED, anchor="middle"))
    return marks


def _occupancy_channel(field: _Field, sectors: list, aims: dict) -> tuple:
    """
    Who is standing where, in one vocabulary: every unit on the map is a sigil.

    A chevron is a ship and a roofed block is a colony wherever they appear --
    the same shapes the org card draws. White is yours, red is theirs. So shape
    answers "what is it", colour answers "whose", and the red's heat and size
    answer "how long ago did I look", which is the only one of the three that
    is ever in doubt.

    That replaces the hollow ring an earlier draft used for remembered
    sightings. The ring was a second vocabulary only rivals spoke, and it made
    a rival a different *kind* of thing from a ship rather than the same kind
    of thing belonging to someone else.
    """
    marks = []
    for s in sectors:
        px, py = field.point(s["coord_x"], s["coord_y"])
        ships, colonies = s["own_ships"] or 0, s["own_colonies"] or 0
        mine = ships + colonies
        rivals = (s["rival_orgs"] or 0) + (s["sighted_rivals"] or 0)
        if not (mine or rivals):
            marks.append({"kind": "circle", "cx": px, "cy": py,
                          "r": max(1.2, field.cell * 0.09), "fill": _SEEN_EMPTY})
            continue

        cell = field.cell
        fresh = _freshness(s.get("confidence"), field.confidence_max)
        rival_color = _rival_heat(fresh)
        # A colony is the standing claim: hold one in a sector and that is what
        # the sector is, however many ships are parked alongside. The ships are
        # in the count, and the whole roster is in the sector detail view.
        own_type = "colony" if colonies else "ship"

        if field.tier == "dot":
            marks.append({"kind": "circle", "cx": px, "cy": py, "r": cell * 0.24,
                          "fill": _OWN if mine else rival_color})
            continue

        if mine and rivals:
            # Concentric, red just inside white. Neither side is drawn smaller
            # than the other, which a side-by-side pair could not avoid -- the
            # two rings are the same node, contested, not two half-nodes.
            marks += _ring(px, py, cell * _RING_OWN, 1.35, _OWN_RING, _SPACE)
            marks += _ring(px, py, cell * _RING_RIVAL_IN, 1.35, _RIVAL_RING, _SPACE)
            sigil_type, sigil_color = _contested_sigil(s, own_type, rival_color)
            marks += _glyph(sigil_type, px, py, cell * 0.34, sigil_color)
        elif mine:
            marks += _ring(px, py, cell * _RING_OWN, 1.15, _OWN_RING, _SPACE)
            marks += _glyph(own_type, px, py, cell * 0.42, _OWN)
        else:
            # Rival alone. The ring holds its size and its colour whatever the
            # confidence: it is the container the count rides on, and what you
            # counted does not get less true as it ages -- only less current,
            # which is exactly what the sigil inside it is already saying. So
            # a plus-size rival sector still reads as one on the turn before it
            # blinks out.
            marks += _ring(px, py, cell * _RING_OWN, 1.15, _RIVAL_RING, _SPACE)
            marks += _rival_marks(_rival_type(s), px, py, cell,
                                  s.get("confidence"),
                                  confidence_max=field.confidence_max)

        if field.tier == "full":
            marks += _count_marks(px, py, cell, mine, _OWN_CLOCK, _COUNT_INK)
            marks += _count_marks(px, py, cell, rivals, _RIVAL_CLOCK, _RIVAL_COUNT)

    return marks


def _contested_sigil(sector: dict, own_type: str, rival_color: str) -> tuple:
    """
    (org_type, colour) for the one sigil a contested node draws in its centre.

    Normally it is your own dominant organization -- you know yours for certain,
    and a colony outranks a ship. The carve-out is a sector where the *rival*
    holds a colony and you do not: a colony is the standing claim on a sector,
    so if the only one there is theirs, that is the fact about the sector and
    it draws in red. Ships either way and it stays yours.
    """
    if sector.get("own_colonies"):
        return "colony", _OWN
    if _rival_type(sector) == "colony":
        return "colony", rival_color
    return own_type, _OWN


def _rival_type(sector: dict) -> str:
    """
    What kind of rival unit to draw.

    Neither rival source breaks down by org_type today: show_sector_neighborhood
    counts live rivals with a bare COUNT(*), and sightings_by_sector aggregates
    the org_type column it stores straight back out again. Its own-org query
    already groups by (sector_id, org_type), so the ask is only that the two
    rival queries do what that one does -- see the design record.

    Until then a rival reads as a ship, which is the honest default: colonies
    are rare, and a colony you cannot confirm is a colony you were told about
    by a scan that did not report what it was.
    """
    for key in ("rival_colonies", "sighted_colonies"):
        if sector.get(key):
            return "colony"
    return "ship"


def _scan_channel(field: _Field, sectors: list, aims: dict) -> tuple:
    """
    What the fleet is looking at this turn, drawn against what it still cannot
    see.

    **Dot size is how much you know**, the same on this channel as on every
    other: a sector you have intel on is a full dot, a sector nobody has ever
    seen is the same tiny mark it is everywhere else. An earlier draft inverted
    that here -- never-seen cells drawn bright and large, because they are what
    a scan is *for* -- and it read backwards. A big mark means a lot is known
    about that spot, and no amount of arguing about which cell is more
    actionable survives contact with that reading.

    What makes this the scan channel is what is layered over the dots: the
    rings on sectors under scan this turn, and the beams reaching them. The
    scan-me count still leads the footer, in text white, which is where a
    figure belongs -- it does not need the lattice shouting it too.

    A beam runs from the scanner to its target. The scanner is often outside
    the frame -- an aim is an offset from the org's own sector, and coverage
    reaches across the frame edge -- in which case the beam starts at the disc
    boundary, which is honest about the direction without inventing a source.
    """
    marks = []
    for s in sectors:
        px, py = field.point(s["coord_x"], s["coord_y"])
        marks.append({"kind": "circle", "cx": px, "cy": py,
                      "r": max(1.4, field.cell * 0.12), "fill": _SEEN_EMPTY})
    for aim in aims.get("beams", []):
        (sx, sy), (tx, ty) = aim["from"], aim["to"]
        marks.append({"kind": "line", "x1": sx, "y1": sy, "x2": tx, "y2": ty,
                      "stroke": _BEAM, "stroke_width": 1})
    # The scanner, where it is in frame. A beam with no visible source reads as
    # coverage arriving from nowhere, and which org is looking is the thing a
    # captain retasks.
    for (x, y) in aims.get("origins", []):
        px, py = field.point(x, y)
        marks += _glyph("ship", px, py, field.cell * 0.42, _SCAN)
    for (x, y) in aims.get("targets", []):
        px, py = field.point(x, y)
        marks += _ring(px, py, field.cell * 0.42, 1.6, _SCAN, _FIELD)
    return marks


_CHANNELS = {"energy": _energy_channel, "occupancy": _occupancy_channel,
             "scan": _scan_channel}

# Legends live here rather than inside the channels because they depend on the
# palette and never on the data -- which is what lets the height reserve below
# be the most rows ANY channel needs at a given width, instead of a guess one
# channel quietly overflows. Same motive as the fixed reserve itself: three
# views of one neighborhood have to be the same height.
def _legends(field: _Field) -> dict:
    """
    Every channel's legend, for a given field.

    A function rather than a table because the energy keys quote the scale they
    are keying -- and the scale is data now. A static "0.50 / 0.75 / 1.00"
    would describe a band the map had stopped drawing the moment anyone tuned
    the sector die.

    All three are built together because the height reserve below is the most
    rows ANY channel needs at this width: three views of one neighborhood have
    to be the same height, and no channel can know that alone.
    """
    floor, ceil = field.energy_floor, field.energy_ceil
    return {
        "energy": [
            ("dot:0.45", _lerp_hex(_RAMP_LOW, _RAMP_HIGH, 0.0), in_thousands(floor)),
            ("dot:0.75", _lerp_hex(_RAMP_LOW, _RAMP_HIGH, 0.5),
             in_thousands((floor + ceil) / 2)),
            ("dot:1.1", _lerp_hex(_RAMP_LOW, _RAMP_HIGH, 1.0),
             f"{in_thousands(ceil)} and up"),
            ("none", "", "×1k energy"),
            ("dot:0.5", _UNKNOWN, "never seen"),
        ],
        "occupancy": [
            ("glyph:ship", _OWN, "yours"),
            ("glyph:ship", _RIVAL, "rival, just seen"),
            ("glyph:ship:0.62", _lerp_hex(_RIVAL_COLD, _RIVAL, 0.2), "…cooling"),
            ("count:own", _OWN_RING, "occupants, yours"),
            ("count:rival", _RIVAL_RING, "theirs"),
            ("dot:0.8", _SEEN_EMPTY, "seen, empty"),
            ("dot:0.45", _UNKNOWN, "never seen"),
        ],
        "scan": [
            ("ring", _SCAN, "under scan this turn"),
            ("glyph:ship", _SCAN, "scanner"),
            ("line", _BEAM, "bearing"),
            ("dot:1.0", _SEEN_EMPTY, "you have intel"),
            ("dot:0.45", _UNKNOWN, "never seen"),
        ],
    }


_TITLE_VERB = {"energy": "Resources near", "occupancy": "Neighborhood of",
               "scan": "Scan coverage from"}


# --- Legend and footer -------------------------------------------------------
# The legend is flowed, not columned: entries differ in width per channel, and
# a fixed column that fits "rival seen earlier" wastes half a line on "0.50".

_SWATCH = 9


def _swatch_marks(kind: str, color: str, x: float, y: float) -> list:
    """One legend key, drawn with the same primitive the map draws it with --
    a legend that redraws a mark in a different shape teaches the wrong key."""
    if kind.startswith("ring"):
        scale = float(kind.split(":")[1]) if ":" in kind else 1.0
        return _ring(x + _SWATCH / 2, y - 3, _SWATCH / 2 * scale, 1.3, color,
                     _SPACE)
    if kind == "line":
        return [{"kind": "line", "x1": x, "y1": y - 3, "x2": x + _SWATCH,
                 "y2": y - 3, "stroke": color, "stroke_width": 1}]
    if kind.startswith("glyph:"):
        bits = kind.split(":")
        scale = float(bits[2]) if len(bits) > 2 else 1.0
        return _glyph(bits[1], x + _SWATCH / 2, y - 3, _SWATCH * scale, color)
    if kind.startswith("count:"):
        side = kind.split(":")[1]
        # The swatch draws the real thing at legend scale: a ring with a count
        # on its clock position, so the key teaches the position rather than
        # describing it.
        degrees = _OWN_CLOCK if side == "own" else _RIVAL_CLOCK
        cx, cy = x + _SWATCH / 2, y - 3
        return (_ring(cx, cy, _SWATCH / 2, 1.1, color, _SPACE)
                + _count_marks(cx, cy, _SWATCH, 3, degrees,
                               _COUNT_INK if side == "own" else _RIVAL_COUNT,
                               size=6.5, pad=2.0))
    if kind == "none":
        return []
    scale = float(kind.split(":")[1]) if ":" in kind else 1.0
    return [{"kind": "circle", "cx": x + _SWATCH / 2, "cy": y - 3,
             "r": _SWATCH / 2.6 * scale, "fill": color}]


# Every channel reserves the same legend depth whether or not it fills it, so
# the three views of one neighborhood are the same height and a client that
# tabs between them does not jump. Same reasoning as the card drawing all five
# tasking bars: a fixed shape is what makes two readings comparable. The
# reserve is a function of width alone, so all three channels of one map agree
# on it without consulting each other.
def _entry_advance(kind: str, label: str) -> float:
    """How much width one key consumes, swatch plus label plus gutter."""
    lead = 0 if kind == "none" else _SWATCH + (8 if kind.startswith("count:") else 4)
    return lead + len(label) * 4.6 + 11


def _flow_rows(entries: list, width: int) -> int:
    """Rows this entry list wraps onto at `width`. The one place the wrap is
    computed, so the reserve and the drawing cannot disagree about it."""
    x, rows = _MARGIN, 1
    for kind, _color, label in entries:
        advance = _entry_advance(kind, label)
        if x > _MARGIN and x + advance > width - _MARGIN:
            x, rows = _MARGIN, rows + 1
        x += advance
    return rows


def _legend_rows(width: int, legends: dict) -> int:
    """The deepest legend any channel needs here -- every channel reserves it."""
    return max(_flow_rows(entries, width) for entries in legends.values())


def _legend_marks(entries: list, top: float, width: int, rows: int) -> tuple:
    """Flow legend entries across the width, wrapping. Returns (marks, y)."""
    marks, x, y = [], _MARGIN, top
    right = width - _MARGIN
    for kind, color, label in entries:
        lead = 0 if kind == "none" else _SWATCH + 4
        advance = _entry_advance(kind, label)
        if x > _MARGIN and x + advance > right:
            x, y = _MARGIN, y + _LEGEND_ROW_H
        marks += _swatch_marks(kind, color, x, y)
        marks.append(_text(x + lead, y, label, 8.5, _MUTED))
        x += advance
    return marks, top + rows * _LEGEND_ROW_H


def _footer_marks(data: dict, top: float, width: int) -> tuple:
    """
    The counts a player acts on. `unknown_in_range` leads and is the one figure
    on the card drawn in text white: it is the scan-me list, and it is the
    number that says whether looking again is worth a turn.
    """
    marks, y = [], top
    unknown = data.get("unknown_in_range") or 0
    marks.append(_text(_MARGIN, y, f"{unknown}", 12, _TEXT, weight=700))
    marks.append(_text(_MARGIN + 7 + len(str(unknown)) * 7, y,
                       "in range, never seen", 10, _MUTED))
    off_plane = data.get("off_plane_count") or 0
    if off_plane:
        marks.append(_text(width - _MARGIN, y,
                           f"+{off_plane} off this z-plane", 10, _DIM, anchor="end"))
    return marks, y + _FOOTER_H


# --- Scan aims ---------------------------------------------------------------

def _aim_geometry(field: _Field, scan_aims: list) -> dict:
    """
    Targets under scan, and the beams reaching them. A beam whose scanner
    stands outside the frame starts at the disc edge instead.

    The origin is read off the aim rather than backed out of its bearing: a
    compass table here would be a copy of engine/bearings.SCAN_BEARINGS, and
    that table is only correct while SCAN_RANGE == 2, which bearings.py says
    itself.
    """
    targets, beams, origins = set(), [], set()
    mx, my = field.mid
    for aim in scan_aims:
        tx, ty = aim["target_x"], aim["target_y"]
        targets.add((tx, ty))
        if aim.get("origin_x") is None:
            continue
        ox, oy = aim["origin_x"], aim["origin_y"]
        start, end = field.point(ox, oy), field.point(tx, ty)
        in_frame = (ox - field.cx) ** 2 + (oy - field.cy) ** 2 <= field.radius ** 2
        if in_frame:
            origins.add((ox, oy))
        else:
            # Off-frame scanner: walk the beam back to where it crosses the
            # disc, so the line says which way the look comes from and stops
            # short of claiming a source that is not drawn.
            dx, dy = start[0] - end[0], start[1] - end[1]
            length = (dx * dx + dy * dy) ** 0.5 or 1.0
            reach = field.disc_r - ((end[0] - mx) ** 2 + (end[1] - my) ** 2) ** 0.5
            scale = min(1.0, max(0.0, reach) / length)
            start = (end[0] + dx * scale, end[1] + dy * scale)
        beams.append({"from": start, "to": end})
    return {"targets": targets, "beams": beams, "origins": origins}


# --- The layout --------------------------------------------------------------

def layout_neighborhood(data: dict, channel: str = None,
                        width: int = None) -> tuple:
    """
    One neighborhood payload into (marks, dims), for one of three channels.

    `data` is what show_sector_neighborhood() returns -- which carries every
    field all three channels need, energy_capacity included, so a client
    drawing three tabs of one neighborhood makes one call, not three.
    show_neighborhood_resources()'s payload also drives `channel="energy"`,
    since that is the subset it holds.

    Raises KeyError if `data` isn't a neighborhood payload: a blank map is a
    bug in whoever produced the data, not something to paper over. Same
    posture as layout_org_card.
    """
    if "error" in data:
        return _error_layout(data["error"])

    display = data["display"]
    # The payload names the channel; the argument is an override for a caller
    # holding a capture, and the default is what the tool's own default is.
    channel = channel or display.get("channel") or "occupancy"
    if channel not in _CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}")
    center, radius = data["center"], data["radius"]
    sectors = [s for s in data["sectors"] if s.get("in_plane")]
    field = _Field(display["grid"], center, radius, _HEADER_H + _X_STRIP, width,
                   scales={
                       "energy_floor": _scale(data, "energy", "floor", _ENERGY_FLOOR),
                       "energy_ceil": _scale(data, "energy", "ceil", _ENERGY_CEIL),
                       "confidence_max": _scale(data, "confidence", "max",
                                                _CONFIDENCE_MAX),
                   })

    # Every channel needs a figure the energy one formats; do it here rather
    # than requiring the resources payload, using the same helper the markdown
    # map's cells are built with so the two cannot state different units.
    for s in sectors:
        s.setdefault("energy_display", in_thousands(s["energy_capacity"]))
    legends = _legends(field)

    aims = _aim_geometry(field, data.get("scan_aims") or [])
    known = {(s["coord_x"], s["coord_y"]) for s in sectors}

    channel_marks = _CHANNELS[channel](field, sectors, aims)
    marks = [
        *_ground_marks(field, (center["x"], center["y"], center["z"], radius)),
        *_unknown_marks(field, known),
        *_center_marks(field),
        *channel_marks,
    ]

    y = field.bottom + 12
    block, y = _legend_marks(legends[channel], y, field.width,
                             _legend_rows(field.width, legends))
    marks += block
    y += 2
    block, y = _footer_marks(data, y, field.width)
    marks += block

    height = y + _PAD
    title, _, clock = (display.get("header") or "").partition(" — ")
    # The verb comes from the channel, not the payload: one payload can be
    # drawn three ways and its header only names the tool that built it.
    subject = title.split(" of ")[-1].split(" near ")[-1].split(" from ")[-1]
    header = [
        {"kind": "rect", "x": 1, "y": 1, "w": field.width - 2, "h": height - 2,
         "rx": 16, "fill": _SPACE, "stroke": _BORDER, "stroke_width": 2},
        _text(_MARGIN, 26, f"{_TITLE_VERB[channel]} {subject}", 15, _TEXT, weight=700),
        _text(_MARGIN, 43, clock or f"radius {radius}", 10, _MUTED),
        _text(field.width - _MARGIN, 43, f"r{radius} · {len(sectors)} known",
              10, _DIM, anchor="end"),
    ]
    return header + marks, {"width": field.width, "height": height}


def _error_layout(message: str) -> tuple:
    height, width = 96, MAP_WIDTH
    return [
        {"kind": "rect", "x": 1, "y": 1, "w": width - 2, "h": height - 2,
         "rx": 16, "fill": _SPACE, "stroke": _BORDER, "stroke_width": 2},
        _text(_MARGIN, height / 2, message, 12, _MUTED),
    ], {"width": width, "height": height}


def render_neighborhood_svg(data: dict) -> str:
    """
    One show_sector_neighborhood() result as a standalone SVG document.

    Single-argument on purpose: server.py's SVG_RENDERERS maps a tool name to
    exactly this shape, so the channel travels in the payload
    (display.channel) rather than as a parameter here.
    """
    return emit_svg(*layout_neighborhood(data))


# On responsiveness: emit_svg writes width and height as *attributes*, and a
# CSS rule beats an attribute -- so `svg { width:100%; height:auto }` wherever
# this is embedded makes it fit its container, with no change here. That is
# why the map sizes itself by growing the canvas per radius (see TARGET_CELL)
# rather than by trying to be scaled down: the cell is meant to arrive at
# roughly its logical size, and the CSS is a safety net, not the plan.
