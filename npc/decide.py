"""
What a strategy document is allowed to look at, and how it may rank what it
sees. Four registries -- gates, sources, rank fields, picks -- and nothing
else. A document names entries in them; it never carries an expression.

This is the whole reason a strategy can be inert data. The document says
*what* to look at and *how* to order it; the code here is the only thing that
knows *how to look*. That split is what makes a strategy safe to accept from
someone else: there is nothing in a document to evaluate, so authoring one
grants no ability a player doesn't already have.

**Fog of war is enforced here, structurally.** Every source requires a
`player_sectors` row at `confidence > 0`, so no strategy -- including one a
player writes -- can read a sector its owner has not seen. A Python strategy
could always have queried `sectors` directly and cheated; a document has no
way to express that, because the only way in is through these functions."""
from db.connection import read_all

# The one attribute a sector has that is worth comparing (db/schema.py's
# sectors table). Named as a set rather than assumed so an unknown rank_by is
# rejected at validation time instead of silently ranking everything equal.
RANK_FIELDS = {"energy_capacity"}

def _second_max(candidates: list, key):
    """
    The runner-up by `key`, or the only candidate if there is just one --
    falling back to `max` rather than raising, since a document author asking
    for "second richest" should still get a target on a two-ship scouting
    fleet that only ever finds one thing.
    """
    if len(candidates) < 2:
        return max(candidates, key=key)
    return sorted(candidates, key=key, reverse=True)[1]


PICKS = {"max": max, "min": min, "second_max": _second_max}


def _aimed_targets(player_id: int) -> list:
    """
    Every sector one of this player's ships currently has its scanner aimed
    at, paired with whether that aim has actually produced a reading.

    Aim is stored as an offset from the scanner's own sector rather than as
    absolute coordinates (see engine/bearings.py), so the target only exists
    while the ship has a position to offset from -- a ship in transit sits at
    the sentinel sector and is reported unresolved rather than being measured
    from (-1,-1,-1)."""
    rows = read_all("""
        SELECT o.id AS org_id, o.sector_id, o.mission,
               o.scan_offset_x, o.scan_offset_y, o.scan_offset_z,
               s.coord_x, s.coord_y, s.coord_z
        FROM organizations o
        LEFT JOIN sectors s ON s.id = o.sector_id
        WHERE o.player_id = ? AND o.scan_offset_x IS NOT NULL
        ORDER BY o.id
    """, (player_id,))

    targets = []
    for row in rows:
        in_transit = row["sector_id"] == -1 or row["coord_x"] is None
        if in_transit or row["mission"] != "idle":
            targets.append({"org_id": row["org_id"], "resolved": False, "sector": None})
            continue
        x = row["coord_x"] + row["scan_offset_x"]
        y = row["coord_y"] + row["scan_offset_y"]
        z = row["coord_z"] + row["scan_offset_z"]
        # confidence > 0 is the fog rule (db/sectors.py): a sector whose
        # confidence has decayed to zero has blinked out and must read as
        # unknown, not as a stale sighting.
        found = read_all("""
            SELECT s.id, s.coord_x, s.coord_y, s.coord_z, s.energy_capacity
            FROM sectors s JOIN player_sectors ps ON ps.sector_id = s.id
            WHERE s.coord_x=? AND s.coord_y=? AND s.coord_z=?
              AND ps.player_id=? AND ps.confidence > 0
        """, (x, y, z, player_id))
        if not found:
            targets.append({"org_id": row["org_id"], "resolved": False, "sector": None})
            continue
        sector = found[0]
        targets.append({"org_id": row["org_id"], "resolved": True,
                        "sector": {"x": sector["coord_x"], "y": sector["coord_y"],
                                   "z": sector["coord_z"],
                                   "energy_capacity": sector["energy_capacity"]}})
    return targets


def _all_scans_resolved(player_id: int) -> bool:
    """
    True once every aimed scanner has produced a reading. Vacuously true when
    nothing is aimed -- a document that gates on scans without ordering any
    proceeds rather than deadlocking, which is the more useful failure.
    """
    return all(t["resolved"] for t in _aimed_targets(player_id))


def _scan_targets(player_id: int) -> list:
    """
    The sectors this player's own scans have revealed -- the candidate set a
    scouting strategy chooses between.

    Deliberately NOT "every sector this player knows". A player's home sector
    is seeded with an enormous energy capacity so that home never depletes
    (config/loader.py's HOME_SECTOR_ENERGY), so ranking all known sectors by
    energy would pick home every time and no strategy could ever choose to go
    anywhere. What a scouting decision is actually about is what its scouts
    found, which is this.

    Deduplicated by coordinate: two scouts sharing a bearing confirm the same
    reading, and that is redundancy, not two candidates.
    """
    seen, candidates = set(), []
    for target in _aimed_targets(player_id):
        if not target["resolved"]:
            continue
        sector = target["sector"]
        key = (sector["x"], sector["y"], sector["z"])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(sector)
    return candidates


GATES = {"all_scans_resolved": _all_scans_resolved}
SOURCES = {"scan_targets": _scan_targets}


def evaluate(player_id: int, step: dict):
    """
    Run one `decide` step. Returns (value, None) when the gate passed and a
    candidate won, (None, reason) when the step should be retried next turn.

    A gate that has not opened and a source that came back empty are both
    "not yet", not errors: a scouting strategy spends its first several turns
    in exactly this state while its scouts travel.
    """
    gate = step.get("await")
    if gate and not GATES[gate](player_id):
        return None, f"gate '{gate}' not met"
    candidates = SOURCES[step["from"]](player_id)
    if not candidates:
        return None, f"source '{step['from']}' is empty"
    field = step["rank_by"]
    return PICKS[step["pick"]](candidates, key=lambda c: c[field]), None
