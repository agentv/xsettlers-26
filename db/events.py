import json
from db.connection import get_connection
from engine.turn import get_current_turn

def record_event(event_type, payload, actor_id=None,
                 subject_id=None, subject_type=None, game_id=None) -> int:
    """Write-ahead: log BEFORE applying state changes. Returns event id."""
    conn = get_connection()
    cur  = conn.cursor()
    turn = get_current_turn()
    cur.execute("SELECT COALESCE(MAX(seq),-1)+1 FROM events WHERE turn=?", (turn,))
    seq  = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO events (game_id,turn,seq,event_type,actor_id,subject_id,subject_type,payload)
        VALUES (?,?,?,?,?,?,?,?)
    """, (game_id,turn,seq,event_type,actor_id,subject_id,subject_type,json.dumps(payload)))
    event_id = cur.lastrowid
    conn.commit(); conn.close()
    return event_id

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
