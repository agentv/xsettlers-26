import math
import os
import random

# --- Sector richness ----------------------------------------------------------
# Energy in a newly discovered sector is 400 guaranteed plus a d6 worth of
# hundreds: 500, 600, 700, 800, 900 or 1000, flat 1-in-6 each, mean 750.
# The frontier can be this lean only because home is not (see
# HOME_SECTOR_ENERGY in config/loader.py) -- the two go together, so a thin
# roll out in the field costs a player their expansion, not their footing.
#
# The floor matters as much as the spread. Every sector is worth taking
# -- 500 is a living, not a death sentence -- so a bad roll costs you upside
# rather than viability, and the decision a player faces is "is this one good
# enough to plant a colony on, or do I keep looking?" rather than "did I get
# a habitable one?". The ceiling is only 2x the floor for the same reason:
# discovery should reward a good find without making an unlucky start
# unrecoverable.
#
# Rolled once, at first discovery, inside reveal_sector() -- see its docstring
# for why that is safe with rivals in play.
SECTOR_ENERGY_BASE = 400.0
SECTOR_ENERGY_DIE_SIDES = 6        # a d6...
SECTOR_ENERGY_DIE_UNIT = 100.0     # ...counted in hundreds
MIN_SECTOR_ENERGY = SECTOR_ENERGY_BASE + SECTOR_ENERGY_DIE_UNIT
MAX_SECTOR_ENERGY = SECTOR_ENERGY_BASE + SECTOR_ENERGY_DIE_SIDES * SECTOR_ENERGY_DIE_UNIT

# Set SECTOR_ROLL_SEED to make discovery reproducible. Without it every game
# rolls its own map, which is the point; with it, an A/B experiment can hold
# the map fixed and vary only the strategy under test, and a failing test can
# be re-run on the same terrain.
_seed = os.getenv("SECTOR_ROLL_SEED")
_rng = random.Random(int(_seed)) if _seed is not None else random.Random()


def roll_sector_energy(multiplier: float = 1.0) -> float:
    """Energy for a newly discovered sector: 400 + d6 x 100, so 500..1000 in
    open space, scaled by the richness of wherever it sits.

    The multiplier scales the whole roll rather than the die alone, so a x3
    region rolls 1500..3000 -- its floor above open space's ceiling. That
    makes a region worth finding on its own account: a scenario's map is a
    set of places that are better, not a set of places where the gamble pays
    more often. See config/loader.HotspotDef.

    The die is rolled before scaling, and always, so the sequence of rolls
    across a game does not depend on where the sectors happened to be. Two
    games on the same seed reveal the same faces in the same order whatever
    map they are played on.

    Home sectors do not use this -- they are seeded flat by db/bootstrap.py
    from the scenario's own figure (see HOME_SECTOR_ENERGY).
    """
    roll = SECTOR_ENERGY_BASE + _rng.randint(1, SECTOR_ENERGY_DIE_SIDES) * SECTOR_ENERGY_DIE_UNIT
    return roll * multiplier


# Layout is drawn from its own generator, seeded off the same SECTOR_ROLL_SEED
# but never sharing a sequence with it. Sharing one would mean that adding a
# hotspot to a scenario silently changed every discovery roll after it, so two
# maps could not be compared on the same seed -- which is the entire reason
# the seed exists.
_MAP_LAYOUT_SALT = 0x5A17


def map_layout_rng() -> random.Random:
    """The generator db/bootstrap.py expands a scenario's `scatter:` rule
    with. Deterministic under SECTOR_ROLL_SEED, so a seeded run lays the same
    map down every time."""
    return random.Random(int(_seed) ^ _MAP_LAYOUT_SALT if _seed is not None else None)


def richness_multiplier(cur, coord_x: int, coord_y: int, coord_z: int) -> float:
    """
    How much richer than open space this coordinate rolls: the largest
    multiplier among the hotspots covering it, or 1.0 where none do.

    Overlapping regions take the maximum rather than the product. Product
    would make two ordinary x2 regions that happen to touch produce a x4
    sector nobody placed, and the overlap of three a x8 one -- the map's
    richest sector would be an accident of geometry. Max keeps a scenario's
    stated multipliers the actual ceiling of what it can produce.

    Read at every reveal from the DB rather than the scenario file, because a
    `scatter:` layout is drawn once at bootstrap and has to survive a restart
    (see db/schema.py's map_hotspots).
    """
    row = cur.execute("""
        SELECT MAX(multiplier) AS m FROM map_hotspots
        WHERE (center_x - :x) * (center_x - :x)
            + (center_y - :y) * (center_y - :y)
            + (center_z - :z) * (center_z - :z) <= radius * radius
    """, {"x": coord_x, "y": coord_y, "z": coord_z}).fetchone()
    return row["m"] if row and row["m"] is not None else 1.0

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

    How rich the roll is depends on where the sector sits: the scenario's map
    may declare regions that roll richer or leaner than open space (see
    richness_multiplier). A player learns a region exists by revealing into it
    and reading what it rolled -- the layout itself is never exposed, so a
    scenario can keep its map secret.

    An already-revealed sector's energy capacity is left untouched -- the
    richness roll happens once, on first reveal, whoever that reveal belongs
    to, and every later look (by any player, by any means) reads the
    established value. That is what makes a sector's richness a property of
    the sector rather than of who found it: this same function is the sole
    path for a scan, a ship arrival, and bootstrap placement alike, so two
    rivals discovering the same sector cannot be told two different things
    about it, and a rival arriving later cannot re-roll your find or refill
    what you have already drawn down. Returns the sector's id either way.

    Energy is the only capacity a sector carries: food and goods are
    manufactured from stock already held, never harvested from the map.
    """
    cur.execute("SELECT id FROM sectors WHERE coord_x=? AND coord_y=? AND coord_z=?",
                (coord_x, coord_y, coord_z))
    row = cur.fetchone()
    if row:
        sector_id = row["id"]
    else:
        cur.execute("""INSERT INTO sectors
            (coord_x,coord_y,coord_z,energy_capacity) VALUES (?,?,?,?)""",
            (coord_x, coord_y, coord_z, roll_sector_energy(
                richness_multiplier(cur, coord_x, coord_y, coord_z))))
        sector_id = cur.lastrowid
        cur.execute("UPDATE sectors SET location=MakePointZ(?,?,?,-1) WHERE id=?",
                    (coord_x, coord_y, coord_z, sector_id))
    cur.execute("""INSERT INTO player_sectors (player_id,sector_id,confidence) VALUES (?,?,100)
        ON CONFLICT(player_id,sector_id) DO UPDATE SET confidence=100""",
        (player_id, sector_id))
    return sector_id
