"""
The opening five lines every gameplay tool used to write for itself.

Before this (2026-08-11) each of the 19 player-facing tools began by opening a
connection, resolving player_token against the players table, returning
{"error": "Player not found"} if it missed, and then closing that connection
by hand on every single return path -- 18 copies of the lookup, 11 of the
follow-up organization-ownership check, and 69 hand-placed conn.close() calls
across four modules.

That was not merely repetitive, it was already wrong in one place:
player_tools.declare_end_turn skipped the check entirely and answered
{"declared": True} to a token belonging to nobody, while every sibling
rejected it. A check that must be re-typed in full for each new tool is a
check that will eventually be mistyped or forgotten; this makes forgetting it
impossible, because the token never reaches the tool body at all.

This is NOT the central `gateway.py` pre-flight wrapper that
docs/mcp_server_layer_design.md once sketched and this codebase deliberately
did not build. The gate is still per-tool and it is still the same gate --
resolve the token against `players`, which is empty until a scenario is
selected, so every tool naturally rejects before bootstrap (see
xsettlers_mcp/game_select.select_scenario and tests/test_gateway.py). Only the
implementation moved; nothing about who may call what changed.
"""
import functools
from db.connection import get_connection

PLAYER_NOT_FOUND = "Player not found"
ORG_NOT_OWNED = "Organization not found or not owned by player"
POD_NOT_OWNED = "Pod not found or not owned by player"


class PlayerSession:
    """
    An authenticated player plus the open cursor their tool call runs on.

    `player` is the full players row (so a tool wanting display_name or the
    record itself needs no second query); `player_id` is the common case.
    """

    def __init__(self, conn, cur, player):
        self.conn = conn
        self.cur = cur
        self.player = player
        self.player_id = player["id"]
        self.released = False

    def own_org(self, org_id: int, columns: str = "id"):
        """
        One of THIS player's organizations, or None -- the ownership check
        that used to be copied into eleven tools. `columns` is a literal from
        the calling code, never player input.
        """
        return self.cur.execute(
            f"SELECT {columns} FROM organizations WHERE id=? AND player_id=?",
            (org_id, self.player_id)).fetchone()

    def own_pod(self, pod_id: int, columns: str = "p.id, p.org_id"):
        """
        One of THIS player's pods, or None. Ownership is indirect -- a pod
        belongs to an organization, which belongs to a player -- so this is a
        join rather than a column check, which is exactly why it was worth
        having in one place instead of two.
        """
        return self.cur.execute(
            f"""SELECT {columns} FROM pods p JOIN organizations o ON o.id = p.org_id
                WHERE p.id=? AND o.player_id=?""", (pod_id, self.player_id)).fetchone()

    def release(self):
        """
        Commit and close NOW, before the tool returns.

        Needed only by tools that hand off to code which opens its own
        connection and writes: set_mission delegating to confirm_move, and
        declare_end_turn triggering end_of_turn() via
        check_consensus_acceleration(). db/connection.py sets no busy_timeout
        and uses the default rollback-journal isolation, so a second writer
        does not block and wait -- it fails immediately with "database is
        locked". Both tools released their connection before delegating when
        they managed it by hand; this is how they keep doing that.

        Idempotent, and the decorator skips its own commit/close once set.
        """
        if not self.released:
            self.conn.commit()
            self.conn.close()
            self.released = True


def player_tool(fn):
    """
    Wrap a tool so it receives an authenticated PlayerSession instead of a raw
    player_token, and never manages a connection itself.

    The wrapped function is written as fn(session, ...) but is still CALLED as
    tool(player_token, ...) -- positionally or by keyword, which is what both
    the MCP dispatch in server.py (fn(**arguments)) and the existing tests
    rely on. On an unrecognized token the body never runs.

    Commits on the way out, so a tool that mutates state just mutates it. The
    write-ahead convention is unaffected: record_event() opens its own
    connection, and every tool here still calls it before taking a write lock
    on this one, exactly as before.
    """
    @functools.wraps(fn)
    def wrapper(player_token, *args, **kwargs):
        conn = get_connection()
        session = None
        try:
            cur = conn.cursor()
            player = cur.execute(
                "SELECT * FROM players WHERE player_token=?", (player_token,)).fetchone()
            if not player:
                return {"error": PLAYER_NOT_FOUND}
            session = PlayerSession(conn, cur, player)
            result = fn(session, *args, **kwargs)
            if not session.released:
                conn.commit()
            return result
        finally:
            if session is None or not session.released:
                conn.close()
    return wrapper
