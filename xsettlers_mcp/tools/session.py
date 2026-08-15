"""
Authentication and connection handling for every gameplay tool.

The gate is per-tool: resolve player_token against `players`, which is empty
until a scenario is selected, so every tool naturally rejects before bootstrap
(see xsettlers_mcp/game_select.select_scenario and tests/test_gateway.py).
There is no central pre-flight wrapper deciding who may call what.

Because the token never reaches a tool body at all, a new tool cannot forget
the check or mistype it.
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
        One of THIS player's organizations, or None. `columns` is a literal
        from the calling code, never player input.
        """
        return self.cur.execute(
            f"SELECT {columns} FROM organizations WHERE id=? AND player_id=?",
            (org_id, self.player_id)).fetchone()

    def own_pod(self, pod_id: int, columns: str = "p.id, p.org_id"):
        """
        One of THIS player's pods, or None. Ownership is indirect -- a pod
        belongs to an organization, which belongs to a player -- so this is a
        join rather than a column check.
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
        locked".

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

    The wrapped function is written as fn(session, ...) but is CALLED as
    tool(player_token, ...) -- positionally or by keyword, which is what both
    the MCP dispatch in server.py (fn(**arguments)) and the tests rely on. On
    an unrecognized token the body never runs.

    Commits on the way out, so a tool that mutates state just mutates it. The
    write-ahead convention holds: record_event() opens its own connection, and
    every tool calls it before taking a write lock on this one.
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
    # Tells the MCP registry to describe this tool as taking player_token in
    # place of the session parameter it is written with (see
    # xsettlers_mcp/tools/registry.py's _schema_for).
    wrapper.takes_player_token = True
    return wrapper
