"""
What a player's scans have seen of other players' organizations.

The counterpart to db/sectors.py's fog of war, and deliberately a separate
one: `player_sectors` records knowledge of the ground, which stays true once
learned, while an organization moves. So a sighting is a dated observation,
never current intel, and every reader is expected to present it with the turn
it was made.
"""
import os
import random

# --- Detection ----------------------------------------------------------------
# Whether a scan actually notices an organization standing in the sector it
# reveals. Rolled per organization rather than per scan: two ships in one
# sector are two chances to be seen, and a later stealth or sensor rating
# attaches to the thing being detected, not to the sweep.
#
# THRESHOLD is the highest roll that still detects, so THRESHOLD == DIE_SIDES
# means certain detection -- which is what it is set to now. The die is rolled
# regardless, so lowering this is a one-line change that alters odds without
# changing when or how often anything is rolled, and the sequence of rolls in
# a seeded game does not shift when the number moves.
DETECTION_DIE_SIDES = 6
DETECTION_THRESHOLD = 6

# Detection draws from its own generator, salted off the same SECTOR_ROLL_SEED,
# for the reason map_layout_rng() has one: sharing a sequence would mean that
# adding a scan anywhere silently changed every later sector's richness, and
# two runs could no longer be compared on one seed.
_DETECTION_SALT = 0xD1CE
_seed = os.getenv("SECTOR_ROLL_SEED")
_rng = random.Random(int(_seed) ^ _DETECTION_SALT if _seed is not None else None)


def roll_detection() -> bool:
    """One organization's detection check: roll a die, detect on THRESHOLD or
    under. At 6 of 6 this is always True, and still consumes a roll."""
    return _rng.randint(1, DETECTION_DIE_SIDES) <= DETECTION_THRESHOLD


def record_sightings(cur, observer_id: int, sector_id: int, current_turn: int) -> list:
    """
    Note every rival organization standing in `sector_id` that this observer's
    scan notices, and return what was seen.

    Called from engine/turn.py's scan resolution, on the same cursor, so a
    sighting lands in the same transaction as the reveal that produced it.

    Own organizations are skipped -- a player already knows where their own
    fleet is, and a self-sighting would show up as a rival marker on their own
    sector. Organizations in transit are skipped by construction: they sit at
    the sentinel sector (id -1), which is nobody's scan target.

    Upserts on (observer, org), so re-sighting a ship moves its last-known
    position rather than appending to a track.
    """
    rows = cur.execute("""SELECT id, player_id, org_type FROM organizations
                          WHERE sector_id = ? AND player_id != ? ORDER BY id""",
                       (sector_id, observer_id)).fetchall()
    seen = []
    for row in rows:
        if not roll_detection():
            continue
        cur.execute("""INSERT INTO org_sightings
            (observer_id, org_id, owner_id, sector_id, org_type, seen_at_turn)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(observer_id, org_id) DO UPDATE SET
                sector_id=excluded.sector_id, seen_at_turn=excluded.seen_at_turn,
                org_type=excluded.org_type""",
            (observer_id, row["id"], row["player_id"], sector_id,
             row["org_type"], current_turn))
        seen.append({"org_id": row["id"], "owner_id": row["player_id"],
                     "org_type": row["org_type"]})
    return seen


def sightings_by_sector(cur, observer_id: int) -> dict:
    """
    This observer's remembered sightings, grouped by the sector they were made
    in: {sector_id: {"count": n, "seen_at_turn": most recent}}.

    Keyed on where the org was *seen*, not where it is now -- that is the whole
    point of the table. A ship that has since moved on still shows at the
    sector it was spotted in, dated, until a later scan finds it somewhere else.
    """
    return {row["sector_id"]: {"count": row["n"], "seen_at_turn": row["seen"]}
            for row in cur.execute("""
                SELECT sector_id, COUNT(*) AS n, MAX(seen_at_turn) AS seen
                FROM org_sightings WHERE observer_id = ?
                GROUP BY sector_id""", (observer_id,)).fetchall()}
