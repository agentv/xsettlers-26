from xsettlers_mcp.tools.registry import mcp_tool
from db.connection import get_connection
from db.sectors import TURNS_TO_BLINK_OUT
from db.sightings import sightings_by_sector
from engine.turn import get_next_tick_at, TURN_LIMIT
from views.format import turn_header
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
    a grid cell has no room to date itself.

    Confidence deliberately does NOT appear on the grid. It's a reporting
    number, not a thing to steer by: with a flat decay a sector is either still
    on your map or it has blinked off it (see db/sectors.py), so the readout
    that matters is binary and the exact figure belongs in `highlights`.

    A rival always wins the third character, truncating the own-org marker to
    make room ("S6C" + rival -> "S6!"): a contested sector is the most
    important cell on the board, and what it costs -- knowing a colony is also
    there -- is static information the player already has, whereas the rival is
    news. Exact composition of any marked cell is in `highlights` regardless.
    """
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
    Where a viewport is centered: {"x", "y", "z", "label"}, or {"error": ...}.

    Either an org's current sector -- the normal way to call a neighborhood
    report, "show me what's around this ship" -- or explicit coordinates. A
    ship in transit (sector_id = -1) has no location and is not a valid
    center. `label` is what the report header names the place: the org's name
    when there is one, else the coordinates.
    """
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


def _draw_grid(cx: int, cy: int, radius: int, by_coord: dict) -> tuple:
    """
    Lay the viewport out as grid rows, and count the cells in range nobody has
    ever seen.

    `by_coord` maps (x, y) to an in-plane sector carrying the "cell" string its
    report built for it. A cell in range with no sector reads UNKNOWN_CELL --
    that count is the scan-me list, and it is returned alongside because it is
    the most actionable number on either map. Out of range renders blank
    rather than unknown: the two are different claims.

    Axis labels are absolute coordinates so anything read off the map can go
    straight into preview_move or set_pod_scan_bearing without arithmetic.
    """
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
                cells.append(known["cell"])
            else:
                cells.append(UNKNOWN_CELL)
                unknown_in_range += 1
        rows.append({"label": str(y), "cells": cells})
    grid = {"corner": "y/x", "x_labels": [str(x) for x in x_labels], "rows": rows}
    return grid, unknown_in_range


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

    Three things distinguish this from get_sector_map(), which returns a bare
    list of what you know:

    - It returns the *complete* lattice, not just known sectors. A cell you
      have never seen is the most actionable thing on the map (it's where to
      send a scan pod), and it has no `sectors` row at all under the lazy
      reveal model (db/sectors.py) -- so the grid is synthesized from the
      center and radius, and known sectors are overlaid onto it. Nothing here
      creates sector rows: this is a pure view, it never calls reveal_sector().
    - Cells carry your own orgs and rival presence, not just sector resources.
    - `display` carries a finished grid (see views/render.py's render_map),
      so every client draws the same map rather than each improvising one
      from a coordinate list.

    The grid is a single z-plane -- the center's. The model is 3D and distance
    is 3D everywhere else in the codebase, but no scenario has yet placed
    anything off z=0, so a plane is the whole picture today. Known sectors in
    range but off-plane are still returned in `sectors` and counted in
    `off_plane_count` rather than silently dropped, so the day z matters the
    view says so instead of quietly lying.

    Rival presence comes from two sources that are never mixed. Sectors at
    confidence 100 -- ones you currently occupy -- report rivals live from
    `organizations`, because standing there means seeing what is there now.
    Everywhere else reports remembered sightings from `org_sightings`, dated
    with the turn the scan was made (see db/sightings.py).

    Keeping them apart is what stops a stale cell from handing out current
    intel: a sector you scanned fifty turns ago shows what was there then, and
    says so, rather than quietly reporting who is there today.
    """
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

    grid, unknown_in_range = _draw_grid(cx, cy, radius, by_coord)

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
        "display": {
            "kind": "map",
            # Same turn-and-countdown line the status reports open with, from
            # the one helper, so a player reading two reports side by side
            # cannot be told two different things about the clock.
            "header": f"Neighborhood of {origin}"
                      + (f" — {turn_header(current_turn, TURN_LIMIT, next_tick_at)}"
                         if current_turn is not None else ""),
            "grid": grid,
            "legend": CELL_LEGEND,
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
CENTER_MARK = ("[", "]")     # brackets the cell you are centered on

# The grid already carries every figure, so the table below it is a shortlist,
# not a repeat: the richest sectors you can currently see, which is the
# question a resource map is opened to answer.
RICHEST_ROWS = 10

RESOURCE_LEGEND = [
    "Each cell is that sector's energy capacity -- the only resource the map itself yields.",
    f"{CENTER_MARK[0]}700{CENTER_MARK[1]} = the sector this view is centered on",
    f"{UNKNOWN_CELL} = in range, never seen    (blank) = outside range",
    f"Sectors blink out {TURNS_TO_BLINK_OUT} turns after they were last seen, "
    "taking their reading with them.",
    f"The table below lists the {RICHEST_ROWS} richest sectors you can currently see.",
]


def _energy_cell(energy: float, is_center: bool) -> str:
    """One grid cell: a sector's energy capacity as a whole number, bracketed
    when it is the sector the view is centered on.

    Nothing in the game yields a fraction of a resource, so a decimal point is
    noise in a cell this narrow. The center is marked because this map has no
    other anchor -- the who-is-where map shows your own ships, and a reader of
    a grid of bare numbers would otherwise have to count axis labels to find
    where they are standing."""
    figure = f"{energy:.0f}"
    return f"{CENTER_MARK[0]}{figure}{CENTER_MARK[1]}" if is_center else figure


@mcp_tool(
    "Map the resources in the local neighborhood (aka local neighborhood "
    "resources / resource map / what's nearby worth taking). Same center and "
    "range as show_sector_neighborhood -- an org_id, the normal way to call "
    "it, or explicit (center_x, center_y, center_z); a ship in transit has "
    f"no location and can't be a center; default radius {NEIGHBORHOOD_RADIUS} "
    f"(a 9x9 grid), max {MAX_NEIGHBORHOOD_RADIUS} -- but every cell holds "
    "what a sector is worth rather than who is standing in it. Energy is the "
    "only resource the map yields, so a known sector reads as its energy "
    "capacity, '·' means in range and never seen, and blank means out of "
    "range; display.grid is ready to draw and display.rows_key names the "
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

    The companion to show_sector_neighborhood, over exactly the same viewport:
    same center rules (an org's sector or explicit coordinates, never a ship
    in transit), same radius, same fog of war, same single z-plane with
    off-plane sectors counted rather than dropped. Both draw their lattice
    through the shared helpers above, so the two reports cannot come to
    disagree about which sectors are "nearby". What differs is the question a
    cell answers: there, who is standing in the sector; here, what the sector
    holds.

    Only energy today, because a sector yields nothing else -- food and goods
    are manufactured from stock a player already holds, never harvested from
    the map (see db/sectors.py). A cell is therefore a single figure rather
    than a slashed run; the day a sector has a second yield, the cell gains a
    slot and this report keeps its name.

    A figure is only as current as the sector it came from. Energy capacity
    doesn't change on its own, but your knowledge of it ages: a reading is
    from whenever you last saw the place, and when that sector blinks out at
    confidence 0 the reading leaves the map with it. `confidence` rides on
    every row for exactly that reason.

    `richest` is the shortlist the grid can't be: the RICHEST_ROWS best
    sectors in view, ranked. Ties break on coordinates so the same board
    always ranks the same way.

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
        s["energy_display"] = f"{s['energy_capacity']:.0f}"
        s["is_center"] = (s["coord_x"], s["coord_y"], s["coord_z"]) == (cx, cy, cz)
        s["cell"] = _energy_cell(s["energy_capacity"], s["is_center"])
        s["in_plane"] = s["coord_z"] == cz
        if s["in_plane"]:
            by_coord[(s["coord_x"], s["coord_y"])] = s

    grid, unknown_in_range = _draw_grid(cx, cy, radius, by_coord)
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
            "column_labels": {"coords_display": "Coords",
                              "energy_display": "Energy",
                              "confidence": "Confidence"},
        },
    }
