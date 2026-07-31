import math
import os

DEFAULT_SECTOR_RESOURCE_UNITS = 1000.0  # flat for now; TODO: randomize per-sector later

# --- Fog of war ---------------------------------------------------------------
# Confidence decays by a fixed number of points per turn, NOT by a fraction of
# whatever is left. It's a percentage of a fixed maximum (100), so proportional
# decay is the wrong model twice over: it never actually reaches 0 (at 10%/turn
# on an integer column, round(4 * 0.9) == 4 is a fixed point, so sectors linger
# forever at 4), and it stretches "forgetting" out over dozens of turns instead
# of a span a player can reason about. At 20 points/turn, a sector that goes
# unconfirmed blinks out of view on the 5th turn after it was last seen.
#
# Lives here rather than in engine/turn.py so both the decay step and the
# read-side views can name it -- engine/turn.py imports sector_tools, so the
# constant can't live on either of those without a cycle.
CONFIDENCE_DECAY_PER_TURN = int(os.getenv("CONFIDENCE_DECAY_PER_TURN", 20))
TURNS_TO_BLINK_OUT = math.ceil(100 / CONFIDENCE_DECAY_PER_TURN)


def reveal_sector(cur, player_id: int, coord_x: int, coord_y: int, coord_z: int) -> int:
    """
    Get-or-create the sector at (coord_x,coord_y,coord_z) and mark it visible
    to player_id at confidence=100. The single entry point for the lazy-reveal
    model (docs/data_model_and_storage_design.md): a sector row only exists
    once bootstrap placement, ship arrival, or a resolved scan reveals it.

    cur is an open cursor on the caller's connection/transaction -- this
    function does not commit; callers (bootstrap_game(), end_of_turn())
    commit as part of their own transaction.

    An already-revealed sector's resource capacities are left untouched --
    only the first reveal sets them. Returns the sector's id either way.
    """
    cur.execute("SELECT id FROM sectors WHERE coord_x=? AND coord_y=? AND coord_z=?",
                (coord_x, coord_y, coord_z))
    row = cur.fetchone()
    if row:
        sector_id = row["id"]
    else:
        cur.execute("""INSERT INTO sectors
            (coord_x,coord_y,coord_z,energy_capacity,food_capacity,goods_capacity)
            VALUES (?,?,?,?,?,?)""",
            (coord_x, coord_y, coord_z, DEFAULT_SECTOR_RESOURCE_UNITS,
             DEFAULT_SECTOR_RESOURCE_UNITS, DEFAULT_SECTOR_RESOURCE_UNITS))
        sector_id = cur.lastrowid
        cur.execute("UPDATE sectors SET location=MakePointZ(?,?,?,-1) WHERE id=?",
                    (coord_x, coord_y, coord_z, sector_id))
    cur.execute("""INSERT INTO player_sectors (player_id,sector_id,confidence) VALUES (?,?,100)
        ON CONFLICT(player_id,sector_id) DO UPDATE SET confidence=100""",
        (player_id, sector_id))
    return sector_id
