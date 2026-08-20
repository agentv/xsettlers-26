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
    "Default radius 5 (an 11x11 grid), max 10. Returns the complete "
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

    label = None
    if org_id is not None:
        origin = sess.own_org(org_id, columns="name, sector_id")
        origin_sector = cur.execute("""SELECT coord_x, coord_y, coord_z FROM sectors
            WHERE id=?""", (origin["sector_id"],)).fetchone() if origin else None
        if not origin or origin["sector_id"] == -1 or not origin_sector:
            return {"error": "Organization not found, not owned by player, or currently in transit"}
        cx, cy, cz = origin_sector["coord_x"], origin_sector["coord_y"], origin_sector["coord_z"]
        label = origin["name"]
    elif None not in (center_x, center_y, center_z):
        cx, cy, cz = center_x, center_y, center_z
    else:
        return {"error": "Must supply either org_id or (center_x, center_y, center_z)"}

    r2 = radius ** 2
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
        (player_id, cx, cx, cy, cy, cz, cz, r2))
    sectors = [dict(r) for r in cur.fetchall()]

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

    cur.execute("SELECT current_turn FROM game_state WHERE id=1")
    turn_row = cur.fetchone()
    current_turn = turn_row["current_turn"] if turn_row else None
    next_tick_at = get_next_tick_at()

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

    x_range = list(range(cx - radius, cx + radius + 1))
    rows, unknown_in_range = [], 0
    for y in range(cy - radius, cy + radius + 1):
        cells = []
        for x in x_range:
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

    highlights = [s for s in sectors
                  if s["own_ships"] or s["own_colonies"] or s["rival_orgs"]
                  or s["sighted_rivals"]]
    origin = label or f"({cx},{cy},{cz})"
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
            "grid": {"corner": "y/x", "x_labels": [str(x) for x in x_range], "rows": rows},
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
