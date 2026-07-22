from db.connection import get_connection
from engine.turn import check_consensus_acceleration

def get_player_state(slack_user_id: str) -> dict:
    """Full state: player record, all organizations, all pods."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE slack_user_id=?", (slack_user_id,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("SELECT * FROM organizations WHERE player_id=?", (player["id"],))
    orgs = [dict(o) for o in cur.fetchall()]
    for org in orgs:
        cur.execute("SELECT * FROM pods WHERE org_id=?", (org["id"],))
        org["pods"] = [dict(p) for p in cur.fetchall()]
    conn.close()
    return {"player": dict(player), "organizations": orgs}

def declare_end_turn(slack_user_id: str) -> dict:
    """Player declares they have no further moves this tick."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE players SET end_turn_declared=1 WHERE slack_user_id=?", (slack_user_id,))
    conn.commit(); conn.close()
    return {"declared": True, "clock_accelerated": check_consensus_acceleration()}

def rescind_end_turn(slack_user_id: str) -> dict:
    """Player takes back their end turn declaration."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT end_turn_declared FROM players WHERE slack_user_id=?", (slack_user_id,))
    row = cur.fetchone()
    if not row:
        conn.close(); return {"error": "Player not found"}
    cur.execute("UPDATE players SET end_turn_declared=0 WHERE slack_user_id=?", (slack_user_id,))
    conn.commit(); conn.close()
    return {"rescinded": True}
