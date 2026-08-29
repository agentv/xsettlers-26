from xsettlers_mcp.tools.registry import mcp_tool
from db.events import record_event
from engine.turn import check_consensus_acceleration
from xsettlers_mcp.tools.session import player_tool

# Matches organization_tools.py's MAX_CALL_SIGN_LENGTH -- same "has to fit a
# fleet-report/leaderboard column on a phone" constraint.
MAX_DISPLAY_NAME_LENGTH = 24

@mcp_tool(
    "Dashboard: player record, all organizations, all pods")
@player_tool
def get_player_state(sess) -> dict:
    """Full state: player record, all organizations, all pods."""
    orgs = [dict(o) for o in sess.cur.execute(
        "SELECT * FROM organizations WHERE player_id=?", (sess.player_id,)).fetchall()]
    for org in orgs:
        org["pods"] = [dict(p) for p in sess.cur.execute(
            "SELECT * FROM pods WHERE org_id=?", (org["id"],)).fetchall()]
    return {"player": dict(sess.player), "organizations": orgs}

@mcp_tool(
    "Player declares no further moves this tick")
@player_tool
def declare_end_turn(sess) -> dict:
    """
    Player declares they have no further moves this tick.

    Authenticated like every sibling tool -- this one can end the turn for
    everyone via check_consensus_acceleration(), so an unrecognized token must
    not reach it.
    """
    sess.cur.execute("UPDATE players SET end_turn_declared=1 WHERE id=?", (sess.player_id,))
    # Released before consensus is evaluated: check_consensus_acceleration()
    # can run a whole end_of_turn() on its own connection, which cannot
    # proceed while this one holds the write lock (see PlayerSession.release).
    sess.release()
    return {"declared": True, "clock_accelerated": check_consensus_acceleration()}

@mcp_tool(
    "Player rescinds their end turn declaration")
@player_tool
def rescind_end_turn(sess) -> dict:
    """Player takes back their end turn declaration."""
    sess.cur.execute("UPDATE players SET end_turn_declared=0 WHERE id=?", (sess.player_id,))
    return {"rescinded": True}

@mcp_tool(
    "Choose your own in-game display name, shown to every player on the "
    "leaderboard -- independent of any name GameHouse or bootstrap "
    "supplied. Must be unique game-wide.")
@player_tool
def set_display_name(sess, display_name: str) -> dict:
    """
    Choose your own in-game display name -- local to this xsettlers game,
    independent of whatever name GameHouse (or a bootstrap default like
    "Player 3") supplied at handoff. This is the only name other players
    ever see, shown on the shared leaderboard (show_civilization_status),
    so it's required to be unique game-wide, not just per player -- unlike
    set_call_sign's per-player uniqueness, since orgs are never shown
    across players but display_name always is.

    Bounded at MAX_DISPLAY_NAME_LENGTH and stripped of surrounding
    whitespace, same reasoning as set_call_sign. Empty names are
    rejected rather than silently restoring the bootstrap default.
    """
    display_name = (display_name or "").strip()
    if not display_name:
        return {"error": "Display name cannot be empty"}
    if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
        return {"error": f"Display name is {len(display_name)} characters; "
                          f"limit is {MAX_DISPLAY_NAME_LENGTH}"}
    if sess.cur.execute("SELECT id FROM players WHERE id!=? AND display_name=? COLLATE NOCASE",
                     (sess.player_id, display_name)).fetchone():
        return {"error": f"Display name '{display_name}' is already taken"}
    previous = sess.player["display_name"]
    record_event(
        event_type="player.renamed",
        payload={"from": previous, "to": display_name},
        actor_id=sess.player_id, subject_id=sess.player_id, subject_type="player")
    sess.cur.execute("UPDATE players SET display_name=? WHERE id=?", (display_name, sess.player_id))
    return {"ok": True, "previous_display_name": previous, "display_name": display_name}
