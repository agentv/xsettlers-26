import json, math
from db.events import record_event_direct
from engine.pod_tasking import apply_set_pod_task

def apply_confirm_move(cur, org_id: int, player_id: int,
                       dest_x: int, dest_y: int, dest_z: int,
                       jump_range_per_turn: int, current_turn: int) -> dict:
    """
    Core mutation logic behind confirm_move, operating on an already-open
    cur/transaction -- no connection management, no ownership check (the
    caller's job). Shared by xsettlers_mcp.tools.navigation_tools.confirm_move
    (thin wrapper: ownership check + its own connection/commit) and
    engine/ship_log.py's before_arrival/after_arrival dispatch, which runs
    inside engine/turn.py's own open transaction -- a second self-connecting
    call there would fail ("database is locked": db/connection.py sets no
    busy_timeout and uses the default rollback-journal isolation, so a second
    writer doesn't block-and-wait, it errors immediately).
    Takes current_turn as a parameter rather than calling
    engine.turn.get_current_turn() itself, so this module has nothing to
    import from engine.turn -- no circular import.
    """
    org = cur.execute("""SELECT s.coord_x,s.coord_y,s.coord_z,s.id AS sector_id
        FROM organizations o JOIN sectors s ON s.id=o.sector_id
        WHERE o.id=? AND o.sector_id!=-1""", (org_id,)).fetchone()
    if not org:
        return {"error": "Organization not found or already in transit"}
    origin_sector_id = org["sector_id"]
    distance = math.sqrt(
        (dest_x-org["coord_x"])**2 +
        (dest_y-org["coord_y"])**2 +
        (dest_z-org["coord_z"])**2)
    turns_needed = max(1, math.ceil(distance / jump_range_per_turn))
    # +1: arrival_turn names the turn the org is free to act, one turn after
    # the end_of_turn() pass that actually performs the landing (see
    # engine/turn.py's arrival resolution query, which is offset to match).
    arrival_turn = current_turn + turns_needed + 1
    record_event_direct(cur, current_turn, "ship.move_confirmed",
        payload={"org_id": org_id, "from_sector_id": origin_sector_id,
                 "to_x": dest_x, "to_y": dest_y, "to_z": dest_z, "arrival_turn": arrival_turn},
        actor_id=player_id, subject_id=org_id, subject_type="organization")
    cur.execute("""UPDATE organizations SET sector_id=-1, mission='move', mission_params=?,
        is_mobile=0 WHERE id=?""",
        (json.dumps({"dest_x": dest_x, "dest_y": dest_y, "dest_z": dest_z, "arrival_turn": arrival_turn}),
         org_id))
    cur.execute("""INSERT OR REPLACE INTO arrival_queue
        (arrival_turn,org_id,dest_x,dest_y,dest_z,origin_sector_id) VALUES (?,?,?,?,?,?)""",
        (arrival_turn, org_id, dest_x, dest_y, dest_z, origin_sector_id))
    _dispatch_during_transit(cur, org_id, player_id, current_turn)
    return {"confirmed": True, "ship_id": org_id, "from_sector_id": origin_sector_id,
            "dest_x": dest_x, "dest_y": dest_y, "dest_z": dest_z, "arrival_turn": arrival_turn,
            "turns_needed": turns_needed}

def _dispatch_during_transit(cur, org_id: int, player_id: int, current_turn: int):
    """
    Fires the instant this org enters transit -- the "during_transit" phase
    (see docs/TODO.md), which is event-triggered on departure rather than
    turn-based like before_arrival/after_arrival/at_turn, so it's dispatched
    here rather than through engine/turn.py's resolve_turn sweep
    (engine/ship_log.py's dispatch_due_commands). The only action this phase
    supports is set_pod_task -- pod tasking is the one thing NOT locked by an
    org entering transit (set_mission blocks the org itself entirely; a
    pod's task is independent state), which is the whole reason this phase
    exists. Not routed through engine.ship_log.ACTIONS to avoid a
    movement<->ship_log circular import (ship_log already imports this
    module for the 'move' action).
    """
    rows = cur.execute(
        """SELECT id,action,params FROM org_command_queue
           WHERE org_id=? AND trigger_phase='during_transit'""", (org_id,)).fetchall()
    for row in rows:
        if row["action"] == "set_pod_task":
            params = json.loads(row["params"] or "{}")
            offset = None
            if all(k in params for k in ("offset_x", "offset_y", "offset_z")):
                offset = (params["offset_x"], params["offset_y"], params["offset_z"])
            apply_set_pod_task(cur, params["pod_id"], org_id, player_id,
                               params["task"], offset, current_turn)
        cur.execute("DELETE FROM org_command_queue WHERE id=?", (row["id"],))
