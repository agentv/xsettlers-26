import json
from db.connection import get_connection

def record_event(event_type, payload, actor_id=None,
                 subject_id=None, subject_type=None, game_id=None,
                 resolve_at_turn=None) -> int:
    """Write-ahead: log BEFORE applying state changes. Returns event id.

    resolve_at_turn is set for scheduled/future events (e.g. colonize_complete)
    that a later end_of_turn() pass must pick up once current_turn reaches it.
    """
    # Imported here, not at module level: engine.turn now imports
    # record_event_direct (below) from this module, so an eager import here
    # would be circular. Lazy import mirrors the existing pattern engine/
    # turn.py already uses for engine.npc, for the same reason.
    from engine.turn import get_current_turn
    conn = get_connection()
    cur  = conn.cursor()
    turn = get_current_turn()
    cur.execute("SELECT COALESCE(MAX(seq),-1)+1 FROM events WHERE turn=?", (turn,))
    seq  = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO events (game_id,turn,seq,event_type,actor_id,subject_id,subject_type,
                            resolve_at_turn,payload)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (game_id,turn,seq,event_type,actor_id,subject_id,subject_type,
          resolve_at_turn,json.dumps(payload)))
    event_id = cur.lastrowid
    conn.commit(); conn.close()
    return event_id

def record_event_direct(cur, turn: int, event_type: str, actor_id=None,
                        subject_id=None, subject_type=None, payload=None):
    """
    Write an event directly against an already-open cur/transaction, rather
    than opening a fresh connection like record_event does. Needed by any
    caller that must log an event without leaving its own transaction (e.g.
    engine/turn.py's end_of_turn(), engine/movement.py's apply_confirm_move)
    -- db/connection.py sets no busy_timeout and uses the default
    rollback-journal isolation, so a second connection attempting a write
    while the first holds an open write transaction fails immediately
    ("database is locked") rather than blocking. Takes `turn` as an explicit
    parameter (unlike record_event, which calls get_current_turn() itself)
    so this module never needs to import engine.turn -- avoids the circular
    import that motivated keeping this logic out of here until now.
    """
    cur.execute("SELECT COALESCE(MAX(seq),-1)+1 FROM events WHERE turn=?", (turn,))
    seq = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO events (game_id,turn,seq,event_type,actor_id,subject_id,subject_type,payload)
        VALUES (NULL,?,?,?,?,?,?,?)
    """, (turn, seq, event_type, actor_id, subject_id, subject_type, json.dumps(payload or {})))

DISPATCH_FAILURE_EVENT = "alert.queued_command_failed"

def record_dispatch_failure(cur, turn: int, command_id: int, org_id: int,
                            player_id: int, action: str, error: str):
    """
    Log a queued command that raised when the engine tried to run it.

    Containment, not diagnosis: queue_command validates its params up front
    (see organization_tools._normalize_queued_params), so nothing reaching a
    dispatcher should fail any more. This exists for the cases that check
    cannot cover -- rows queued before that validation existed, rows written
    directly, and whatever a future action learns to fail at. The rule this
    upholds is that one player's bad order must never stop the turn for
    everyone else, which is exactly what used to happen: the exception escaped
    end_of_turn(), the offending row was never deleted (deletion happens after
    the handler returns), and it re-fired on every subsequent tick.

    Lives here rather than in either dispatcher because both need it and they
    cannot import from each other -- engine/ship_log.py already imports
    engine/movement.py for the 'move' action.
    """
    record_event_direct(cur, turn, DISPATCH_FAILURE_EVENT,
        actor_id=player_id, subject_id=org_id, subject_type="organization",
        payload={"command_id": command_id, "org_id": org_id,
                 "action": action, "error": error})

# Removed 2026-08-11 (complexity audit): record_turn_snapshot(),
# get_events_since_turn() and get_last_snapshot(). All three had zero callers
# and zero tests since the initial commit -- speculative replay/recovery
# scaffolding for a feature that was never built.
#
# record_turn_snapshot() was worse than merely unused. It wrote event_type
# "turn.snapshot" -- the SAME type engine/turn.py's _snapshot_holdings() writes
# live, every turn, for every player -- but with an incompatible payload (a
# full dump of players/organizations/pods/player_sectors, versus the ledger's
# per-player holdings+score+waste row). get_last_snapshot() read that type
# back and would have handed a caller a ledger row while calling it a recovery
# checkpoint. Rebuild both sides together, against one agreed payload, if
# replay is ever actually wanted.
