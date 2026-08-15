"""
Reading an organization's place on the map.

An org's location is stored, not derived: `organizations.sector_id` is a plain
foreign key, written only when a ship departs, lands, or is rubber-banded back
by cancel_move. What needs a join is its *coordinates*, which belong to the
sector rather than to the org -- so this module owns that join, once, instead
of it being retyped wherever someone needs to know where a ship is.

Deliberately a leaf: it imports nothing and takes an open cursor, so both the
engine and the tool layer can use it without joining the circular-import
tangle those two navigate (see db/events.py and engine/turn.py's lazy imports).
"""

def org_position(cur, org_id: int):
    """
    Where an organization is right now: its sector id, that sector's
    coordinates, and its owner -- or None when it has no position at all.

    None covers both real cases a caller has to handle, deliberately without
    distinguishing them, because callers treat them identically: the org does
    not exist, or it is in transit. An in-transit org is parked at the sentinel
    sector (id -1), which is a real row with coordinates (-1,-1,-1) -- so
    filtering it out here is what stops a caller offsetting a scan aim, or a
    relative move, from a position that is a placeholder rather than a place.
    """
    return cur.execute(
        """SELECT o.id AS org_id, o.player_id, s.id AS sector_id,
                  s.coord_x, s.coord_y, s.coord_z
           FROM organizations o JOIN sectors s ON s.id = o.sector_id
           WHERE o.id = ? AND o.sector_id != -1""",
        (org_id,)).fetchone()
