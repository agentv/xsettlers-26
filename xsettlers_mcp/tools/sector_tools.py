from xsettlers_mcp.tools.registry import mcp_tool
from db.connection import get_connection
from db.sectors import TURNS_TO_BLINK_OUT
from db.sightings import sightings_by_sector
from engine.scanning import aim_label, scanners_on
from engine.turn import get_next_tick_at, TURN_LIMIT
from views.format import in_thousands, turn_header
from xsettlers_mcp.tools.session import player_tool

# --- Neighborhood viewport ---------------------------------------------------
# Radius 4 gives a 9x9 bounding square, which is about as wide as a markdown
# table stays readable on a phone -- the target client. MAX_ is a guard against
# a caller asking for a viewport whose cell count explodes the response.
NEIGHBORHOOD_RADIUS = 4
MAX_NEIGHBORHOOD_RADIUS = 10

# Cell vocabulary. Every marker is <= 3 characters so the grid stays narrow.
UNKNOWN_CELL = "·"   # in range, never seen -- i.e. the scan-me list
SEEN_CELL = "*"           # seen and still current; nothing of anyone's there
EMPTY_CELL = ""           # outside the scan radius; renders as a blank cell
AIM_CELL = ">"            # suffix: something of yours is scanning this sector

AIM_LEGEND = (f"{AIM_CELL} = under scan this turn by one of your organizations "
              f"(which ones, in scan_aims)")

CELL_LEGEND = [
    "S3 = 3 of your ships    C = your colony    S3C = both",
    "R = rival there now    S3! = rival alongside your orgs",
    "r = rival seen there by an earlier scan, not necessarily still there",
    f"{SEEN_CELL} = seen, nothing there    {UNKNOWN_CELL} = in range, never seen",
    "(blank) = outside range",
    f"Sectors blink out {TURNS_TO_BLINK_OUT} turns after they were last seen.",
    "Rivals and confidence per marked sector are in the table below.",
]


def _cell_marker(known: bool, ships: int, colonies: int, rivals: int,
                 remembered: int = 0) -> str:
    """
    Render one grid cell in at most 3 characters: your own presence, else a
    rival's, else a bare SEEN_CELL.

    Live rivals ("R") and remembered ones ("r") are different characters
    because they are different claims. "R" means a rival is there now, and is
    only ever shown for a sector you occupy. "r" means a scan saw one there on
    some turn, which may be long past -- the turn itself is in the table, since
    a grid cell has no room to date itself."""
    if not known:
        return UNKNOWN_CELL
    if ships and colonies:
        marker = f"S{ships}C" if ships < 10 and colonies == 1 else "S*C"
    elif ships:
        marker = f"S{ships}" if ships < 100 else "S9+"
    elif colonies:
        marker = "C" if colonies == 1 else (f"C{colonies}" if colonies < 100 else "C9+")
    elif rivals:
        return "R"
    elif remembered:
        return "r"
    else:
        return SEEN_CELL
    return marker[:2] + "!" if rivals else marker


# --- Shared viewport mechanics -----------------------------------------------
# Both neighborhood reports -- who is where, and what the ground holds -- draw
# the same lattice around the same center at the same radius. Resolving a
# center, deciding which sectors are in range, and laying cells out as rows
# happen here once, so the two maps cannot come to disagree about what "the
# neighborhood" is. What differs between them is only what a cell says, which
# each report builds itself.

def _resolve_center(sess, org_id, center_x, center_y, center_z) -> dict:
    """
    Where a viewport is centered: {"x", "y", "z", "label"}, or {"error": ...}."""
    if org_id is not None:
        origin = sess.own_org(org_id, columns="name, sector_id")
        origin_sector = sess.cur.execute("""SELECT coord_x, coord_y, coord_z FROM sectors
            WHERE id=?""", (origin["sector_id"],)).fetchone() if origin else None
        if not origin or origin["sector_id"] == -1 or not origin_sector:
            return {"error": "Organization not found, not owned by player, or currently in transit"}
        return {"x": origin_sector["coord_x"], "y": origin_sector["coord_y"],
                "z": origin_sector["coord_z"], "label": origin["name"]}
    if None not in (center_x, center_y, center_z):
        return {"x": center_x, "y": center_y, "z": center_z,
                "label": f"({center_x},{center_y},{center_z})"}
    return {"error": "Must supply either org_id or (center_x, center_y, center_z)"}


def _sectors_in_range(cur, player_id: int, cx: int, cy: int, cz: int,
                      radius: int) -> list:
    """
    Every sector the player can currently see (confidence > 0) within `radius`
    of the center, ordered by confidence.

    Distance is 3D, so a known sector off the drawn z-plane is in range and is
    returned -- a report draws one plane but must not pretend the rest of the
    lattice isn't there. Reads only: no reveal_sector(), no confidence change.
    """
    cur.execute("""
        SELECT s.id, s.coord_x, s.coord_y, s.coord_z,
               s.energy_capacity, ps.confidence
        FROM sectors s
        JOIN player_sectors ps ON ps.sector_id = s.id
        WHERE ps.player_id = ? AND ps.confidence > 0
          AND s.id != -1
          AND (
            (s.coord_x - ?) * (s.coord_x - ?) +
            (s.coord_y - ?) * (s.coord_y - ?) +
            (s.coord_z - ?) * (s.coord_z - ?)
          ) <= ?
        ORDER BY ps.confidence DESC""",
        (player_id, cx, cx, cy, cy, cz, cz, radius ** 2))
    return [dict(r) for r in cur.fetchall()]


def _draw_grid(cx: int, cy: int, radius: int, by_coord: dict,
               cell_width: int = 0, aims: dict = None) -> tuple:
    """
    Lay the viewport out as grid rows, and count the cells in range nobody has
    ever seen.

    `by_coord` maps (x, y) to an in-plane sector carrying the "cell" string its
    report built for it. A cell in range with no sector reads UNKNOWN_CELL --
    that count is the scan-me list, and it is returned alongside because it is
    the most actionable number on either map. Out of range renders blank
    rather than unknown: the two are different claims.

    `aims` maps (x, y) to the scanners aimed there, and suffixes AIM_CELL onto
    whatever cell the report built. It is applied here rather than folded into
    the cell string because a scan is most often aimed at a sector nobody has
    seen -- which has no row for a report to decorate, only an UNKNOWN_CELL
    the lattice synthesizes. Aim is a layer over the viewport, not a property
    of a sector, and the two other layers already read that way."""
    x_labels = list(range(cx - radius, cx + radius + 1))
    r2 = radius ** 2
    rows, unknown_in_range = [], 0
    for y in range(cy - radius, cy + radius + 1):
        cells = []
        for x in x_labels:
            if (x - cx) ** 2 + (y - cy) ** 2 > r2:
                cells.append(EMPTY_CELL)
                continue
            known = by_coord.get((x, y))
            if known:
                cell = known["cell"]
            else:
                cell = UNKNOWN_CELL
                unknown_in_range += 1
            cells.append(cell + AIM_CELL if aims and (x, y) in aims else cell)
        rows.append({"label": str(y),
                     "cells": [c.rjust(cell_width) for c in cells]})
    grid = {"corner": "y/x", "x_labels": [str(x) for x in x_labels], "rows": rows}
    return grid, unknown_in_range


def _scan_coverage(sess, cx: int, cy: int, cz: int, radius: int) -> tuple:
    """
    Which sectors in this viewport are under scan right now, and by what:
    ({(x, y): [aim, ...]}, aims).

    Coverage is a question about the neighborhood, not about one org, so it is
    answered from the player's whole fleet. An aim is an offset from the
    scanner's OWN sector (see engine/bearings.py), so each org's targets
    resolve against where that org is standing -- not against the viewport
    center, which is only the same place for the org the map happens to be
    centered on. A ship two sectors outside the frame still covers cells
    inside it, and its coverage is real.

    An org in transit contributes nothing. It has no position to resolve an
    offset against, and end-of-turn suppresses its scans anyway (engine/turn.py
    step 3), so drawing its aim would promise coverage that will not happen.
    """
    r2 = radius ** 2
    by_coord, aims = {}, []
    # The whole fleet, not just orgs in the viewport -- scan range reaches
    # across the frame edge. A player's org count is small, so this costs less
    # than bounding the query (the same reasoning as the overlay queries in
    # show_sector_neighborhood).
    orgs = sess.cur.execute("""
        SELECT o.id, o.name,
               o.scan_offset_x, o.scan_offset_y, o.scan_offset_z,
               s.coord_x, s.coord_y, s.coord_z
        FROM organizations o
        JOIN sectors s ON s.id = o.sector_id
        WHERE o.player_id = ? AND o.sector_id != -1
        ORDER BY o.id""", (sess.player_id,)).fetchall()

    for org in orgs:
        for scanner in scanners_on(sess.cur, org):
            if scanner["offset"] is None:
                continue
            dx, dy, dz = scanner["offset"]
            tx, ty, tz = (org["coord_x"] + dx, org["coord_y"] + dy,
                          org["coord_z"] + dz)
            if tz != cz or (tx - cx) ** 2 + (ty - cy) ** 2 > r2:
                continue
            aim = {"org_id": org["id"], "org_name": org["name"],
                   "source": scanner["source"], "bearing": aim_label(scanner["offset"]),
                   "target_x": tx, "target_y": ty, "target_z": tz}
            aims.append(aim)
            by_coord.setdefault((tx, ty), []).append(aim)
    return by_coord, aims


def _turn_context(cur) -> tuple:
    """(current_turn, next_tick_at) for a report header. current_turn is None
    before a game exists, which is the one case a report must not crash on."""
    turn_row = cur.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()
    return (turn_row["current_turn"] if turn_row else None), get_next_tick_at()

# Scanning is not a player-callable action: the engine resolves it at end of
# turn for every org sensor and every pod on the `scan` task with a valid
# target. Range and the compass live in engine/bearings.py; see engine/turn.py
# for scan resolution logic.


@mcp_tool(
    "Get a specific sector (player-scoped visibility)")
@player_tool
def get_sector(sess, sector_id: int) -> dict:
    """Return sector info — only if the player has visibility (confidence > 0)."""
    sector = sess.cur.execute("""SELECT sec.*, ps.confidence FROM sectors sec
        JOIN player_sectors ps ON ps.sector_id=sec.id
        WHERE sec.id=? AND ps.player_id=? AND ps.confidence>0""",
        (sector_id, sess.player_id)).fetchone()
    if not sector:
        return {"error": "Sector not visible or does not exist"}
    return dict(sector)

@mcp_tool(
    "All sectors visible to the calling player")
@player_tool
def get_sector_map(sess) -> list:
    """Return all sectors visible to this player, ordered by confidence."""
    return [dict(r) for r in sess.cur.execute("""SELECT sec.id,sec.coord_x,sec.coord_y,sec.coord_z,
               sec.energy_capacity,ps.confidence
        FROM sectors sec JOIN player_sectors ps ON ps.sector_id=sec.id
        WHERE ps.player_id=? AND ps.confidence>0 ORDER BY ps.confidence DESC""",
        (sess.player_id,)).fetchall()]


@mcp_tool(
    "Map the neighborhood around one of your organizations (aka "
    "view/visualize the neighborhood). Center is either an org_id -- the "
    "normal way to call it -- or explicit (center_x, center_y, center_z) "
    "coordinates; a ship in transit has no location and can't be a center. "
    f"Default radius {NEIGHBORHOOD_RADIUS} (a 9x9 grid), max "
    f"{MAX_NEIGHBORHOOD_RADIUS}. Returns the complete "
    "lattice, not just known sectors: display.grid holds a ready-to-draw "
    "grid with absolute coordinates on both axes and one <=3-character "
    "marker per cell (your orgs, rival presence, '*' for seen-and-empty, "
    "'·' for never seen, or blank for out of range), plus display.legend. "
    "A '>' suffix marks a sector your fleet is scanning this turn; scan_aims "
    "says which organization and bearing covers each. "
    "Pure view -- reveals nothing, costs nothing, changes no confidence.")
@player_tool
def show_sector_neighborhood(
        sess,
        org_id: int = None,
        center_x: int = None, center_y: int = None, center_z: int = None,
        radius: int = NEIGHBORHOOD_RADIUS) -> dict:
    """
    Render the neighborhood around a center point as a ready-to-draw grid,
    plus the underlying sector data.

    Center is either resolved from org_id (the org's current sector -- this is
    the normal way to call it: "show me what's around this ship") or supplied
    directly as (center_x, center_y, center_z). Ships in transit
    (sector_id = -1) have no location and are not valid org_id centers.

    Rival presence comes from two sources that are never mixed. Sectors at
    confidence 100 -- ones you currently occupy -- report rivals live from
    `organizations`, because standing there means seeing what is there now.
    Everywhere else reports remembered sightings from `org_sightings`, dated
    with the turn the scan was made (see db/sightings.py)."""
    if radius < 1 or radius > MAX_NEIGHBORHOOD_RADIUS:
        return {"error": f"radius must be between 1 and {MAX_NEIGHBORHOOD_RADIUS}"}
    cur = sess.cur
    player_id = sess.player_id

    center = _resolve_center(sess, org_id, center_x, center_y, center_z)
    if "error" in center:
        return center
    cx, cy, cz = center["x"], center["y"], center["z"]
    sectors = _sectors_in_range(cur, player_id, cx, cy, cz, radius)

    # Org overlays, keyed by sector. Both queries cover the whole board rather
    # than just the viewport -- a player's org count is small, and filtering by
    # a synthesized IN-list of sector ids would cost more than it saves.
    own = {}
    cur.execute("""SELECT sector_id, org_type, COUNT(*) AS n FROM organizations
        WHERE player_id = ? AND sector_id != -1 GROUP BY sector_id, org_type""", (player_id,))
    for row in cur.fetchall():
        entry = own.setdefault(row["sector_id"], {"ship": 0, "colony": 0})
        entry[row["org_type"]] = row["n"]
    rivals = {row["sector_id"]: row["n"] for row in cur.execute(
        """SELECT sector_id, COUNT(*) AS n FROM organizations
           WHERE player_id != ? AND sector_id != -1 GROUP BY sector_id""", (player_id,)).fetchall()}

    # Remembered sightings, for the sectors live occupancy does not cover.
    remembered = sightings_by_sector(cur, player_id)

    current_turn, next_tick_at = _turn_context(cur)

    by_coord = {}
    for s in sectors:
        orgs = own.get(s["id"], {})
        s["own_ships"] = orgs.get("ship", 0)
        s["own_colonies"] = orgs.get("colony", 0)
        # See docstring: live rival positions are only honest where you're standing.
        s["rival_orgs"] = rivals.get(s["id"], 0) if s["confidence"] >= 100 else 0
        # ...and a remembered sighting is only news where you are not, since
        # occupying the sector already reports the live truth about it.
        sighting = remembered.get(s["id"]) if s["confidence"] < 100 else None
        s["sighted_rivals"] = sighting["count"] if sighting else 0
        s["sighted_at_turn"] = sighting["seen_at_turn"] if sighting else None
        s["coords_display"] = f"({s['coord_x']},{s['coord_y']},{s['coord_z']})"
        # Rivals and confidence share a cell because neither means much alone.
        # The two sources are mutually exclusive by construction -- live
        # counts only at confidence 100, remembered ones only below it -- so
        # summing them is safe, and the confidence beside the number is what
        # says which kind it is: 100 means a rival is there now, anything less
        # means a scan saw one and the sector has been ageing since. A
        # remembered rival keeps its row until the sector blinks out, which
        # the confidence > 0 filter on the query above already guarantees.
        #
        # With no rival to age, the confidence has nothing to qualify and
        # reads "na" rather than a number -- a bare 100 next to a zero invites
        # being read as certainty about something, when the row is only there
        # because your own orgs are.
        rival_count = s["rival_orgs"] + s["sighted_rivals"]
        s["rivals_display"] = (f"{rival_count}/{s['confidence']}"
                               if rival_count else "0/NA")
        # Energy only: it is the sole resource a sector yields (see
        # db/sectors.py). Food and goods are manufactured from stock already
        # held, never harvested from the map.
        s["resources_display"] = f"E{s['energy_capacity']:.0f}"
        s["cell"] = _cell_marker(True, s["own_ships"], s["own_colonies"],
                                 s["rival_orgs"], s["sighted_rivals"])
        s["in_plane"] = s["coord_z"] == cz
        if s["in_plane"]:
            by_coord[(s["coord_x"], s["coord_y"])] = s

    # Coverage is a property of the viewport, not of how it was centered, so
    # it is drawn for a coordinate-centered map too.
    aim_coords, scan_aims = _scan_coverage(sess, cx, cy, cz, radius)

    grid, unknown_in_range = _draw_grid(cx, cy, radius, by_coord, aims=aim_coords)

    legend = list(CELL_LEGEND)
    if aim_coords:
        legend.append(AIM_LEGEND)

    highlights = [s for s in sectors
                  if s["own_ships"] or s["own_colonies"] or s["rival_orgs"]
                  or s["sighted_rivals"]]
    origin = center["label"]
    return {
        "center": {"x": cx, "y": cy, "z": cz},
        "radius": radius,
        "org_id": org_id,
        "turn": current_turn,
        "sectors": sectors,
        "highlights": highlights,
        "unknown_in_range": unknown_in_range,
        "off_plane_count": sum(1 for s in sectors if not s["in_plane"]),
        "scan_aims": scan_aims,
        "display": {
            "kind": "map",
            # Same turn-and-countdown line the status reports open with, from
            # the one helper, so a player reading two reports side by side
            # cannot be told two different things about the clock.
            "header": f"Neighborhood of {origin}"
                      + (f" — {turn_header(current_turn, TURN_LIMIT, next_tick_at)}"
                         if current_turn is not None else ""),
            "grid": grid,
            "legend": legend,
            "rows_key": "highlights",
            # No resources column: this report is about who is where. What a
            # sector holds stays on every `sectors` row for a client that
            # wants it.
            "columns": ["coords_display", "own_ships", "own_colonies",
                        "rivals_display"],
            "column_labels": {"coords_display": "Coords", "own_ships": "Ships",
                              "own_colonies": "Colonies",
                              "rivals_display": "Rivals/Confidence"},
        },
    }


# --- Resource map ------------------------------------------------------------
# The same viewport, asked a different question: not who is standing where,
# but what the ground under them is worth.
#
# Energy is the whole map because energy is the only thing a sector yields
# (see db/sectors.py) -- food and goods are manufactured from stock already
# held, never harvested. When a sector grows a second yield this report gains
# a slot per cell rather than a second report.
CENTER_MARK = "@"            # suffixes the cell you are centered on

# What the figures are counted in, on the legend line and the table header --
# one label, so the two cannot come to say it differently.
ENERGY_UNIT_LABEL = "×1k"

# Five characters: a figure in thousands is four ("2.20"), and the center mark
# is the fifth. Every cell is padded to it -- blanks and unknowns included --
# because a column of figures only reads as a column when the decimal points
# line up, and the markdown source is ragged otherwise however well a client
# renders it. Wider than the who-is-where map's three, which carries tokens
# ("S3C") rather than numbers.
CELL_WIDTH = 5

# The grid already carries every figure, so the table below it is a shortlist,
# not a repeat: the richest sectors you can currently see, which is the
# question a resource map is opened to answer.
RICHEST_ROWS = 10

# One line per thing a reader has to decode, and nothing a reader can see for
# themselves. The unit line carries its own example because the example is what
# makes it land -- a cell reading "2.20" states the unit and the magnitude in
# the space a sentence about energy would have taken.
RESOURCE_LEGEND = [
    f"Energy ({ENERGY_UNIT_LABEL}): 2.20 = 2,200, 0.90 = 900.",
    f"{UNKNOWN_CELL} = in range, never seen",
    f"The table below lists the {RICHEST_ROWS} richest sectors you can currently see.",
]


def _energy_cell(energy: float, is_center: bool) -> str:
    """One grid cell: a sector's energy capacity in thousands (see
    views.format.in_thousands), marked when it is the sector the view is
    centered on, and right-padded to CELL_WIDTH."""
    figure = in_thousands(energy)
    return (figure + CENTER_MARK if is_center else figure).rjust(CELL_WIDTH)


@mcp_tool(
    "Map the resources in the local neighborhood (aka local neighborhood "
    "resources / resource map / what's nearby worth taking). Same center and "
    "range as show_sector_neighborhood -- an org_id, the normal way to call "
    "it, or explicit (center_x, center_y, center_z); a ship in transit has "
    f"no location and can't be a center; default radius {NEIGHBORHOOD_RADIUS} "
    f"(a 9x9 grid), max {MAX_NEIGHBORHOOD_RADIUS} -- but every cell holds "
    "what a sector is worth rather than who is standing in it. Energy is the "
    "only resource the map yields, so a known sector reads as its energy "
    "capacity in thousands to two decimals ('2.20' is 2,200 energy, and the "
    "legend says so), '·' means in range and never seen, and blank means out "
    "of range; display.grid is ready to draw and display.rows_key names the "
    "richest sectors in view. Pure view -- reveals nothing, costs nothing, "
    "changes no confidence.")
@player_tool
def show_neighborhood_resources(
        sess,
        org_id: int = None,
        center_x: int = None, center_y: int = None, center_z: int = None,
        radius: int = NEIGHBORHOOD_RADIUS) -> dict:
    """
    Render what the neighborhood around a center point is worth, as a
    ready-to-draw grid plus the underlying sector data.

    Only energy today, because a sector yields nothing else -- food and goods
    are manufactured from stock a player already holds, never harvested from
    the map (see db/sectors.py). A cell is therefore a single figure rather
    than a slashed run; the day a sector has a second yield, the cell gains a
    slot and this report keeps its name.

    Pure view -- it never calls reveal_sector(), and reading a resource map
    costs nothing and reveals nothing to anyone else.
    """
    if radius < 1 or radius > MAX_NEIGHBORHOOD_RADIUS:
        return {"error": f"radius must be between 1 and {MAX_NEIGHBORHOOD_RADIUS}"}
    cur = sess.cur

    center = _resolve_center(sess, org_id, center_x, center_y, center_z)
    if "error" in center:
        return center
    cx, cy, cz = center["x"], center["y"], center["z"]
    sectors = _sectors_in_range(cur, sess.player_id, cx, cy, cz, radius)
    current_turn, next_tick_at = _turn_context(cur)

    by_coord = {}
    for s in sectors:
        s["coords_display"] = f"({s['coord_x']},{s['coord_y']},{s['coord_z']})"
        s["energy_display"] = in_thousands(s["energy_capacity"])
        s["is_center"] = (s["coord_x"], s["coord_y"], s["coord_z"]) == (cx, cy, cz)
        s["cell"] = _energy_cell(s["energy_capacity"], s["is_center"])
        s["in_plane"] = s["coord_z"] == cz
        if s["in_plane"]:
            by_coord[(s["coord_x"], s["coord_y"])] = s

    grid, unknown_in_range = _draw_grid(cx, cy, radius, by_coord, CELL_WIDTH)
    richest = sorted(sectors,
                     key=lambda s: (-s["energy_capacity"], s["coord_x"],
                                    s["coord_y"], s["coord_z"]))[:RICHEST_ROWS]

    return {
        "center": {"x": cx, "y": cy, "z": cz},
        "radius": radius,
        "org_id": org_id,
        "turn": current_turn,
        "sectors": sectors,
        "richest": richest,
        "unknown_in_range": unknown_in_range,
        "off_plane_count": sum(1 for s in sectors if not s["in_plane"]),
        "display": {
            "kind": "map",
            # Same turn-and-countdown line every other report opens with, from
            # the one helper, so two reports read side by side cannot be told
            # different things about the clock.
            "header": f"Resources near {center['label']}"
                      + (f" — {turn_header(current_turn, TURN_LIMIT, next_tick_at)}"
                         if current_turn is not None else ""),
            "grid": grid,
            "legend": RESOURCE_LEGEND,
            "rows_key": "richest",
            # Confidence rides beside the figure because it says how old the
            # reading is -- the number itself never changes, but what you know
            # about it does.
            "columns": ["coords_display", "energy_display", "confidence"],
            # Same unit as the grid, stated in the header: two figures for
            # one quantity in one report is how a player misreads it.
            "column_labels": {"coords_display": "Coords",
                              "energy_display": f"Energy ({ENERGY_UNIT_LABEL})",
                              "confidence": "Confidence"},
        },
    }
