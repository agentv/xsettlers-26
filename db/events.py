import json
from db.connection import get_connection

def record_event(event_type, payload, actor_id=None,
                 subject_id=None, subject_type=None, game_id=None,
                 resolve_at_turn=None) -> int:
    """Write-ahead: log BEFORE applying state changes. Returns event id.

    resolve_at_turn is set for scheduled/future events (e.g. colonize_complete)
    that a later end_of_turn() pass must pick up once current_turn reaches it.
    """
    # Imported here, not at module level: engine.turn imports
    # record_event_direct (below) from this module, so an eager import here
    # would be circular. Same lazy-import pattern engine/turn.py uses for
    # engine.npc, for the same reason.
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
                        subject_id=None, subject_type=None, payload=None,
                        resolve_at_turn=None):
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
    so this module never needs to import engine.turn -- avoids a circular
    import.

    resolve_at_turn carries the same meaning it does in record_event -- a
    deferred event a later end_of_turn() pass picks up once current_turn
    reaches it. engine/missions.py's apply_colonize uses it to schedule
    colonize_complete from inside the turn transaction.
    """
    cur.execute("SELECT COALESCE(MAX(seq),-1)+1 FROM events WHERE turn=?", (turn,))
    seq = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO events (game_id,turn,seq,event_type,actor_id,subject_id,subject_type,
                            resolve_at_turn,payload)
        VALUES (NULL,?,?,?,?,?,?,?,?)
    """, (turn, seq, event_type, actor_id, subject_id, subject_type,
          resolve_at_turn, json.dumps(payload or {})))

DISPATCH_FAILURE_EVENT = "alert.queued_command_failed"
COMMAND_REFUSED_EVENT = "alert.queued_command_refused"

def record_command_refused(cur, turn: int, command_id: int, org_id: int,
                           player_id: int, action: str, error: str):
    """
    Log a queued command the engine ran and the game legitimately declined.

    Deliberately a different event type from record_dispatch_failure below.
    That one means something was *wrong* -- a malformed order that raised, a
    bug to go and fix. This one means the order was well-formed and valid when
    it was given, and the world simply moved on: a queued 'colonize' whose ship
    spent its energy before the command fired is the motivating case (see
    engine/missions.apply_colonize). Folding the two together would bury real
    defects in a stream of ordinary economic outcomes.
    """
    record_event_direct(cur, turn, COMMAND_REFUSED_EVENT,
        actor_id=player_id, subject_id=org_id, subject_type="organization",
        payload={"command_id": command_id, "org_id": org_id,
                 "action": action, "error": error})

def record_dispatch_failure(cur, turn: int, command_id: int, org_id: int,
                            player_id: int, action: str, error: str):
    """
    Log a queued command that raised when the engine tried to run it.

    Containment, not diagnosis: queue_command validates its params up front
    (see organization_tools._normalize_queued_params), so nothing reaching a
    dispatcher should fail. This exists for the cases that check cannot cover
    -- rows written directly, and whatever a future action learns to fail at.
    The rule it upholds: one player's bad order must never stop the turn for
    everyone else. An escaping exception would leave the offending row
    undeleted (deletion happens after the handler returns) to re-fire on every
    subsequent tick, and the game could never advance again.

    Lives here rather than in either dispatcher because both need it and they
    cannot import from each other -- engine/ship_log.py imports
    engine/movement.py for the 'move' action.
    """
    record_event_direct(cur, turn, DISPATCH_FAILURE_EVENT,
        actor_id=player_id, subject_id=org_id, subject_type="organization",
        payload={"command_id": command_id, "org_id": org_id,
                 "action": action, "error": error})

# There is no replay/disaster-recovery mechanism. The "turn.snapshot" event
# type belongs solely to engine/turn.py's _snapshot_holdings() -- a per-player
# ledger row (holdings + score + waste), not a full-state checkpoint. If
# replay is ever wanted, build the writer and the reader together against one
# agreed payload, and do not overload this event type to do both jobs.
