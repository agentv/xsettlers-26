"""
What a player's scans have seen of other players' organizations.

Intel is tracked per sector and ages with that sector: a sighting is only ever
read through `player_sectors`, whose confidence decays on the ordinary fog-of-war
schedule, so when a sector blinks out at confidence 0 what you knew about its
occupants goes with it. There is no second expiry rule to keep in step with the
first.

Looking refreshes. Each scan replaces what the observer knew about that
sector's occupants with what it just detected, so a rival that has moved on
stops being reported the next time anyone looks. The turn of the last look is
kept alongside, since a sector at confidence 40 says the intel is old without
saying how old.
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

    A look is authoritative for the sector it looks at: whatever the observer
    previously believed about this sector's occupants is cleared first, then
    replaced by what was detected now. That is what makes an empty sector read
    as empty rather than as whatever was standing there last time.

    Note what that means once DETECTION_THRESHOLD drops below certainty: an
    organization that survives its roll is one the observer genuinely did not
    see, and their map showing nothing is the honest record of that. Missing
    something is supposed to cost you, which is the whole point of rolling.

    Own organizations are skipped -- a player already knows where their own
    fleet is, and a self-sighting would show up as a rival marker on their own
    sector. Organizations in transit are skipped by construction: they sit at
    the sentinel sector (id -1), which is nobody's scan target.

    Sightings key on (observer, org), so a ship detected somewhere new also
    stops being reported where it used to be -- one last-known position each,
    never two.
    """
    rows = cur.execute("""SELECT id, player_id, org_type FROM organizations
                          WHERE sector_id = ? AND player_id != ? ORDER BY id""",
                       (sector_id, observer_id)).fetchall()
    cur.execute("DELETE FROM org_sightings WHERE observer_id=? AND sector_id=?",
                (observer_id, sector_id))
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

    Keyed on where the org was seen, which is the only thing the observer
    actually knows. Callers read this through sectors they can still see, so a
    sighting inherits that sector's confidence and leaves the map with it.
    """
    return {row["sector_id"]: {"count": row["n"], "seen_at_turn": row["seen"]}
            for row in cur.execute("""
                SELECT sector_id, COUNT(*) AS n, MAX(seen_at_turn) AS seen
                FROM org_sightings WHERE observer_id = ?
                GROUP BY sector_id""", (observer_id,)).fetchall()}
