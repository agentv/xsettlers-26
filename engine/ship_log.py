"""
Ship's log: dispatch for org_command_queue's turn-based phases (upon_arrival,
at_turn), resolved by engine/turn.py at the same point arrivals
resolve (see that module's step 2.5, dispatch_due_commands called right after
arrival resolution and before production). upon_departure is a distinct,
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
from db.events import record_dispatch_failure, record_command_refused
from engine.movement import apply_confirm_move, resolve_move_destination
from engine.missions import apply_colonize
from engine.transfers import apply_transfer_order
from engine.scanning import apply_set_org_scan_bearing, offset_from_params
from engine.pod_tasking import apply_set_pod_task

def _dispatch_move(cur, org_id: int, player_id: int, params: dict, current_turn: int):
    dest_x, dest_y, dest_z = resolve_destination(cur, org_id, params)
    apply_confirm_move(cur, org_id, player_id, dest_x, dest_y, dest_z,
        params.get("jump_range_per_turn", 1), current_turn)

def _dispatch_set_pod_task(cur, org_id: int, player_id: int, params: dict, current_turn: int):
    apply_set_pod_task(cur, params["pod_id"], org_id, player_id,
        params["task"], offset_from_params(params), current_turn)

def _dispatch_colonize(cur, org_id: int, player_id: int, params: dict, current_turn: int):
    """
    Unlike every other action here this one can come back with an {"error":...}
    instead of raising or succeeding -- a ship that no longer has the energy is
    refused, not broken. Returning it lets dispatch_due_commands log it as a
    refusal rather than a failure.
    """
    org = cur.execute("SELECT sector_id FROM organizations WHERE id=?", (org_id,)).fetchone()
    return apply_colonize(cur, org_id, player_id, org["sector_id"], current_turn)

def _dispatch_aim_scan(cur, org_id: int, player_id: int, params: dict, current_turn: int):
    apply_set_org_scan_bearing(cur, org_id, player_id, offset_from_params(params),
                               current_turn, bearing=params.get("bearing"))

def _dispatch_transfer(cur, org_id: int, player_id: int, params: dict, current_turn: int):
    """
    Queue a transfer order from this org to another of the same player's orgs;
    it resolves at the next tick's step 2.3, one tick after this command
    fires. Like colonize, this can hand back {"error": ...} rather than
    raising -- co-location is rechecked here (the receiver may have moved off
    the sector this org just arrived at), and a giver that is no longer with
    its target is a refusal, not a fault.
    """
    return apply_transfer_order(cur, player_id, org_id, params["to_org_id"],
                                params["resource"], params["amount"], current_turn)

# The queued binding of engine/actions.py's vocabulary -- these run inside
# engine/turn.py's open transaction, so they dispatch into the engine-layer
# apply_* helpers rather than the self-connecting tool wrappers.
# npc/strategy.py holds the immediate binding of the same names.
ACTIONS = {"move": _dispatch_move, "set_pod_task": _dispatch_set_pod_task,
           "colonize": _dispatch_colonize, "aim_scan": _dispatch_aim_scan,
           "transfer": _dispatch_transfer}

def resolve_destination(cur, org_id: int, params: dict) -> tuple:
    """
    A queued move's absolute destination, from either form its params may
    carry: dest_x/y/z (absolute) or d_x/d_y/d_z (relative to wherever the org
    is standing when the command actually fires). queue_command guarantees
    exactly one of the two is present.

    The relative form is what makes an authored order portable -- the same
    "jump three further out" works from any starting sector, the same reason
    scan aims are stored as offsets rather than coordinates (see
    bearings.SCAN_BEARINGS). It cannot be resolved at queue time because
    the origin is whatever sector the org reaches, which is the point.

    Raises rather than returning an error: a relative move that lands on a
    negative coordinate, or an org with no position to offset from, is a
    malformed order, and dispatch_due_commands' handler guard turns the raise
    into an alert.queued_command_failed event without stopping the turn. That
    is the only thing this adds over engine.movement.resolve_move_destination,
    which an NPC program calls for the same answer and handles differently.
    """
    dest, err = resolve_move_destination(cur, org_id, params)
    if err:
        raise ValueError(err)
    return dest

def dispatch_due_commands(cur, current_turn: int):
    """
    One unified sweep per end_of_turn() call: every org_command_queue row
    whose resolve_turn is due (same <=current_turn+1 threshold arrival_queue's
    own query uses -- see engine/turn.py's step 2). Covers upon_arrival
    (resolve_turn == this turn's arrival_turn, dispatched right after that
    org's own arrival UPDATE lands, so a chained move sees the org's new
    location) and at_turn with one query, and with no per-org special-casing
    inside the arrival-resolution loop.
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
                # One malformed order must not stop the turn for everyone: an
                # exception escaping end_of_turn() would leave this row
                # undeleted (deletion is below, after the handler) to re-fire
                # every tick, and the game could never advance again.
                try:
                    outcome = handler(cur, row["org_id"], org["player_id"],
                                      json.loads(row["params"] or "{}"), current_turn)
                    # A handler that hands back an error declined the order on
                    # the game's terms rather than breaking on it -- logged as
                    # a refusal, not a failure (see db.events.record_command_refused).
                    if isinstance(outcome, dict) and "error" in outcome:
                        record_command_refused(cur, current_turn, row["id"], row["org_id"],
                                               org["player_id"], row["action"],
                                               outcome["error"])
                except Exception as exc:
                    record_dispatch_failure(cur, current_turn, row["id"], row["org_id"],
                                            org["player_id"], row["action"], repr(exc))
        cur.execute("DELETE FROM org_command_queue WHERE id=?", (row["id"],))
