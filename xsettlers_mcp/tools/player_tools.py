from db.connection import get_connection
from db.events import record_event
from engine.turn import check_consensus_acceleration

# Matches organization_tools.py's MAX_ORG_NAME_LENGTH -- same "has to fit a
# fleet-report/leaderboard column on a phone" constraint.
MAX_DISPLAY_NAME_LENGTH = 24

def get_player_state(player_token: str) -> dict:
    """Full state: player record, all organizations, all pods."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE player_token=?", (player_token,))
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

def declare_end_turn(player_token: str) -> dict:
    """Player declares they have no further moves this tick."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE players SET end_turn_declared=1 WHERE player_token=?", (player_token,))
    conn.commit(); conn.close()
    return {"declared": True, "clock_accelerated": check_consensus_acceleration()}

def rescind_end_turn(player_token: str) -> dict:
    """Player takes back their end turn declaration."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT end_turn_declared FROM players WHERE player_token=?", (player_token,))
    row = cur.fetchone()
    if not row:
        conn.close(); return {"error": "Player not found"}
    cur.execute("UPDATE players SET end_turn_declared=0 WHERE player_token=?", (player_token,))
    conn.commit(); conn.close()
    return {"rescinded": True}

def set_display_name(player_token: str, display_name: str) -> dict:
    """
    Choose your own in-game display name -- local to this xsettlers game,
    independent of whatever name GameHouse (or a bootstrap default like
    "Player 3") supplied at handoff. This is the only name other players
    ever see, shown on the shared leaderboard (show_civilization_status),
    so it's required to be unique game-wide, not just per player -- unlike
    rename_organization's per-player uniqueness, since orgs are never shown
    across players but display_name always is.

    Bounded at MAX_DISPLAY_NAME_LENGTH and stripped of surrounding
    whitespace, same reasoning as rename_organization. Empty names are
    rejected rather than silently restoring the bootstrap default.
    """
    display_name = (display_name or "").strip()
    if not display_name:
        return {"error": "Display name cannot be empty"}
    if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
        return {"error": f"Display name is {len(display_name)} characters; "
                          f"limit is {MAX_DISPLAY_NAME_LENGTH}"}
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id, display_name FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("SELECT id FROM players WHERE id!=? AND display_name=? COLLATE NOCASE",
                (player["id"], display_name))
    if cur.fetchone():
        conn.close(); return {"error": f"Display name '{display_name}' is already taken"}
    previous = player["display_name"]
    record_event(
        event_type="player.renamed",
        payload={"from": previous, "to": display_name},
        actor_id=player["id"], subject_id=player["id"], subject_type="player")
    cur.execute("UPDATE players SET display_name=? WHERE id=?", (display_name, player["id"]))
    conn.commit(); conn.close()
    return {"ok": True, "previous_display_name": previous, "display_name": display_name}
