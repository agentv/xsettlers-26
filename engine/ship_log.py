"""
Ship's log: dispatch for org_command_queue's turn-based phases (before_arrival,
after_arrival, at_turn), resolved by engine/turn.py at the same point arrivals
resolve (see that module's step 2.5, dispatch_due_commands called right after
arrival resolution and before production). during_transit is a distinct,
event-triggered phase -- not turn-based at all -- dispatched instead from
inside engine/movement.apply_confirm_move at the point sector_id is set to
-1 (this module isn't imported there, to avoid a movement<->ship_log
circular import; that hook only ever needs set_pod_task, so it calls
engine.pod_tasking.apply_set_pod_task directly).

Action whitelist dispatches via engine-layer core mutation helpers (not the
xsettlers_mcp.tools.* wrappers directly -- those are self-connecting and
would fail with "database is locked" if called from inside engine/turn.py's
already-open transaction; see engine/movement.py's docstring).
"""
import json
from db.events import record_dispatch_failure
from engine.movement import apply_confirm_move
from engine.pod_tasking import apply_set_pod_task

def _dispatch_move(cur, org_id: int, player_id: int, params: dict, current_turn: int):
    apply_confirm_move(cur, org_id, player_id,
        params["dest_x"], params["dest_y"], params["dest_z"],
        params.get("jump_range_per_turn", 1), current_turn)

def _dispatch_set_pod_task(cur, org_id: int, player_id: int, params: dict, current_turn: int):
    offset = None
    if all(k in params for k in ("offset_x", "offset_y", "offset_z")):
        offset = (params["offset_x"], params["offset_y"], params["offset_z"])
    apply_set_pod_task(cur, params["pod_id"], org_id, player_id,
        params["task"], offset, current_turn)

ACTIONS = {"move": _dispatch_move, "set_pod_task": _dispatch_set_pod_task}

def dispatch_due_commands(cur, current_turn: int):
    """
    One unified sweep per end_of_turn() call: every org_command_queue row
    whose resolve_turn is due (same <=current_turn+1 threshold arrival_queue's
    own query uses -- see engine/turn.py's step 2). Covers before_arrival
    (resolve_turn == this turn's arrival_turn, dispatched right after that
    org's own arrival UPDATE lands, so a chained move sees the org's new
    location) and after_arrival (resolve_turn == a prior turn's arrival_turn
    + 1) with one query -- no need to special-case per-org inside the
    arrival-resolution loop itself.
    Skips actually dispatching (but still deletes the row -- one-shot, never
    re-fires) if the org no longer exists or its mission is no longer 'idle':
    a player who already gave the org new orders shouldn't have them
    silently clobbered by a stale queued command.
    """
    rows = cur.execute(
        "SELECT id,org_id,action,params FROM org_command_queue WHERE resolve_turn<=?",
        (current_turn + 1,)).fetchall()
    for row in rows:
        org = cur.execute("SELECT player_id,mission FROM organizations WHERE id=?",
                          (row["org_id"],)).fetchone()
        if org and org["mission"] == "idle":
            handler = ACTIONS.get(row["action"])
            if handler:
                # One malformed order must not stop the turn for everyone. An
                # exception here used to escape end_of_turn() entirely, leaving
                # the row undeleted (deletion is below, after the handler) so it
                # re-fired every tick and the game could never advance again.
                try:
                    handler(cur, row["org_id"], org["player_id"],
                           json.loads(row["params"] or "{}"), current_turn)
                except Exception as exc:
                    record_dispatch_failure(cur, current_turn, row["id"], row["org_id"],
                                            org["player_id"], row["action"], repr(exc))
        cur.execute("DELETE FROM org_command_queue WHERE id=?", (row["id"],))
