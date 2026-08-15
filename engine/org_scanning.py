"""
Core mutation logic for aiming an organization's innate sensors.

Same split, and the same reason, as engine/movement.py and engine/missions.py:
the tool wrapper (xsettlers_mcp.tools.organization_tools.set_org_scan_bearing)
owns the connection, the ownership check and the range check; this module owns
the state change and operates on an already-open cur/transaction, so
engine/ship_log.py can dispatch a queued 'aim_scan' from inside end_of_turn()'s
own transaction without a second connection failing on "database is locked".

Range is deliberately NOT re-checked here: aim_status() needs the org's
current sector, and by design the aim is a relative offset whose reach never
changes with position (see bearings.SCAN_BEARINGS -- "north is -y", and an
offset's range is fixed), so an aim validated when the order was given is
still in range wherever the ship ends up. queue_command validates it at queue
time for exactly that reason.
"""
from db.events import record_event_direct

def apply_set_org_scan_bearing(cur, org_id: int, player_id: int, offset,
                               current_turn: int, bearing: str = None):
    """
    Point an org's sensors at `offset` (a relative x/y/z triple), or clear the
    aim entirely when offset is None. No ownership check, no range check (the
    caller's job).

    Clearing writes no event, matching what the tool has always done -- there
    is no organization.scan_bearing_cleared type and this is not the place to
    invent one. `bearing` is the compass name for the offset when the caller
    happens to know it (set_org_scan_bearing resolves one via aim_status);
    it is payload decoration only, never used to compute the aim.
    """
    if offset is None:
        cur.execute("""UPDATE organizations
            SET scan_offset_x=NULL, scan_offset_y=NULL, scan_offset_z=NULL WHERE id=?""",
            (org_id,))
        return
    record_event_direct(cur, current_turn, "organization.scan_bearing_set",
        payload={"org_id": org_id, "offset_x": offset[0], "offset_y": offset[1],
                 "offset_z": offset[2], "bearing": bearing},
        actor_id=player_id, subject_id=org_id, subject_type="organization")
    cur.execute("""UPDATE organizations
        SET scan_offset_x=?, scan_offset_y=?, scan_offset_z=? WHERE id=?""",
        (offset[0], offset[1], offset[2], org_id))
