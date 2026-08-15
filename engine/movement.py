import json, math
from db.events import record_event_direct, record_dispatch_failure
from db.orgs import org_position
from engine.pod_tasking import apply_set_pod_task, offset_from_params

ABSOLUTE_KEYS = ("dest_x", "dest_y", "dest_z")
RELATIVE_KEYS = ("d_x", "d_y", "d_z")
NEGATIVE_DEST = "Destination coordinates cannot be negative -- space has no negative indices"

def move_params_error(params: dict) -> str | None:
    """
    Why a move's params could never work, or None if they are well-formed.

    A move is addressed one of two ways and never both: absolute
    dest_x/dest_y/dest_z, or d_x/d_y/d_z relative to wherever the org is
    standing when the order fires. The relative form is what makes an authored
    order portable between starting positions, and it is why the
    negative-coordinate guard only applies to the absolute form here -- a
    relative move's real destination isn't knowable until it fires, so
    resolve_move_destination checks it then.

    Returns a bare sentence so callers can frame it: a tool returns it as
    {"error": ...}, a program validator prefixes it with which step failed.
    """
    absolute = [k for k in ABSOLUTE_KEYS if params.get(k) is not None]
    relative = [k for k in RELATIVE_KEYS if params.get(k) is not None]
    if absolute and relative:
        return ("a move takes dest_x/dest_y/dest_z (absolute) or d_x/d_y/d_z "
                "(relative to wherever the org is when the order fires), not both")
    if not absolute and not relative:
        return "a move needs a destination -- dest_x/dest_y/dest_z or d_x/d_y/d_z"
    missing = [k for k in (ABSOLUTE_KEYS if absolute else RELATIVE_KEYS)
               if params.get(k) is None]
    if missing:
        return f"a move needs all three coordinates -- missing {', '.join(missing)}"
    if absolute and any(params[k] < 0 for k in ABSOLUTE_KEYS):
        return NEGATIVE_DEST
    return None

def resolve_move_destination(cur, org_id: int, params: dict):
    """
    A move's absolute destination, from either params form: (dest, None), or
    (None, error) if it cannot be resolved.

    The relative form resolves against where the org is standing at the moment
    this runs -- which is the whole point of that form, and why it cannot be
    resolved when the order is given. Both the ship's log firing a queued move
    and an NPC program dispatching an immediate one land here, so "three
    further out the way I'm heading" means the same thing either way.

    Returns the error rather than raising, because the two callers want
    opposite things with it: engine/ship_log.py raises so its handler guard
    logs alert.queued_command_failed without stopping the turn, while a
    program collects it into the profile's error list.
    """
    if params.get("dest_x") is not None:
        return (params["dest_x"], params["dest_y"], params["dest_z"]), None
    org = org_position(cur, org_id)
    if not org:
        return None, f"org {org_id} is in transit -- no position to apply a relative move from"
    dest = (org["coord_x"] + params["d_x"], org["coord_y"] + params["d_y"],
            org["coord_z"] + params["d_z"])
    if any(c < 0 for c in dest):
        return None, (f"relative move from ({org['coord_x']},{org['coord_y']},"
                      f"{org['coord_z']}) lands at {dest} -- space has no negative indices")
    return dest, None

def plan_move(origin, dest, jump_range_per_turn: int, current_turn: int) -> dict:
    """
    What a move would cost, as pure arithmetic: straight-line distance, whole
    turns needed, and the turn the org is free to act again. No DB, no writes.

    Both halves of the two-step move flow call this -- preview_move to quote a
    move, apply_confirm_move to commit one -- so a quote and the move that
    follows it cannot disagree about travel time. Computed independently in two
    places, they could.

    arrival_turn is the turn the org becomes free to act, one turn AFTER the
    end_of_turn() pass that performs the landing (engine/turn.py's arrival
    resolution query is offset to match). A player reading it can plan that
    turn's orders directly rather than accounting for a hidden lag.

    turns_needed floors at 1: even a move to an adjacent sector -- or to the
    org's own -- takes a turn.
    """
    distance = math.dist(origin, dest)
    turns_needed = max(1, math.ceil(distance / jump_range_per_turn))
    return {"distance": distance, "turns_needed": turns_needed,
            "arrival_turn": current_turn + turns_needed + 1}

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
    org = org_position(cur, org_id)
    if not org:
        return {"error": "Organization not found or already in transit"}
    origin_sector_id = org["sector_id"]
    plan = plan_move((org["coord_x"], org["coord_y"], org["coord_z"]),
                     (dest_x, dest_y, dest_z), jump_range_per_turn, current_turn)
    turns_needed, arrival_turn = plan["turns_needed"], plan["arrival_turn"]
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
            # Guarded for the same reason as engine/ship_log.py's sweep: a
            # malformed row must not take down the caller. This path is
            # reached from confirm_move as well as from the turn engine, so an
            # unguarded raise would fail a player's move tool call too, not
            # just the tick.
            try:
                params = json.loads(row["params"] or "{}")
                apply_set_pod_task(cur, params["pod_id"], org_id, player_id,
                                   params["task"], offset_from_params(params),
                                   current_turn)
            except Exception as exc:
                record_dispatch_failure(cur, current_turn, row["id"], org_id,
                                        player_id, row["action"], repr(exc))
        cur.execute("DELETE FROM org_command_queue WHERE id=?", (row["id"],))
