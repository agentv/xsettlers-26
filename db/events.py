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

def record_turn_snapshot(turn: int) -> int:
    """Full-state snapshot at end of turn — the recovery checkpoint."""
    conn = get_connection(); cur = conn.cursor()
    payload = {
        "turn": turn,
        "players":       [dict(r) for r in cur.execute("SELECT * FROM players").fetchall()],
        "organizations": [dict(r) for r in cur.execute("SELECT * FROM organizations").fetchall()],
        "pods":          [dict(r) for r in cur.execute("SELECT * FROM pods").fetchall()],
        "player_sectors":[dict(r) for r in cur.execute("SELECT * FROM player_sectors").fetchall()],
    }
    conn.close()
    return record_event("turn.snapshot", payload)

def get_events_since_turn(since_turn: int) -> list:
    """All events after since_turn, ordered for replay."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM events WHERE turn>? ORDER BY turn,seq", (since_turn,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close(); return rows

def get_last_snapshot() -> dict | None:
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""SELECT * FROM events WHERE event_type='turn.snapshot'
                   ORDER BY turn DESC,seq DESC LIMIT 1""")
    row = cur.fetchone(); conn.close()
    if not row: return None
    result = dict(row); result["payload"] = json.loads(result["payload"])
    return result
