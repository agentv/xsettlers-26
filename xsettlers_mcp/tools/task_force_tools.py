"""
Task forces: a player-named, explicitly managed roster of that player's own
ships. See docs/dev_history.md for the full design; docs/TODO.md carries what is
still open (a set_pod_task fan-out).

No new engine mechanics and no new turn-resolution step -- a stored roster
(task_forces + organizations.task_force_id) plus a tool-layer wrapper over
calls that already exist. Membership carries no co-location or org-state
requirement of its own; it changes only by direct action here, with one
exception -- a member leaves the instant it colonizes (engine/turn.py's
_handle_colonize clears task_force_id in the same statement that flips
org_type), since a task force cannot hold anything but a ship.

order_task_force is a fan-out, not a transaction: the same mission/params go
independently to every current member's own org id, through set_mission
itself -- so every per-org lock and rule set_mission already enforces applies
per ship, and one member unable to accept the order (in transit, mid-
colonization, ...) fails only for that member.
"""
from xsettlers_mcp.tools.registry import mcp_tool
from db.events import record_event_direct
from xsettlers_mcp.tools.session import player_tool, ORG_NOT_OWNED
from xsettlers_mcp.tools.organization_tools import set_mission, MAX_CALL_SIGN_LENGTH
from engine.turn import get_current_turn

TASK_FORCE_NOT_OWNED = "Task force not found or not owned by player"
NOT_A_SHIP = "Only ships can join a task force — colonies cannot"
# Matches organization_tools' MAX_CALL_SIGN_LENGTH -- both are player-chosen
# labels that have to fit the same report-column width.
MAX_TASK_FORCE_NAME_LENGTH = 24


def _own_ship(sess, org_id: int):
    """A ship (not colony) owned by this player, or (None, error dict)."""
    org = sess.cur.execute(
        "SELECT id, org_type, task_force_id FROM organizations WHERE id=? AND player_id=?",
        (org_id, sess.player_id)).fetchone()
    if not org:
        return None, {"error": ORG_NOT_OWNED}
    if org["org_type"] != "ship":
        return None, {"error": NOT_A_SHIP}
    return org, None


def _own_task_force(sess, task_force_id: int):
    return sess.cur.execute(
        "SELECT id, name, call_sign FROM task_forces WHERE id=? AND player_id=?",
        (task_force_id, sess.player_id)).fetchone()


@mcp_tool(
    "Give one of your own task forces a call sign -- your own name for it, "
    "up to 24 characters, which reports show in place of the name you "
    "created it with. Additional, not a rename: the task force keeps that "
    "name. Change it as often as you like during a game.")
@player_tool
def set_task_force_call_sign(sess, task_force_id: int, call_sign: str) -> dict:
    """
    Give a task force a call sign, on exactly the same terms an organization
    gets one (see organization_tools.set_call_sign): additional rather than a
    rename, bounded at MAX_CALL_SIGN_LENGTH, unique among this player's task
    forces, and preferred by reports over the created name.

    Uniqueness is scoped to task forces alone -- a task force and a ship
    sharing a call sign is not ambiguous, because no tool takes either by
    name.
    """
    call_sign = (call_sign or "").strip()
    if not call_sign:
        return {"error": "Call sign cannot be empty"}
    if len(call_sign) > MAX_CALL_SIGN_LENGTH:
        return {"error": f"Call sign is {len(call_sign)} characters; "
                         f"limit is {MAX_CALL_SIGN_LENGTH}"}
    cur = sess.cur
    tf = _own_task_force(sess, task_force_id)
    if not tf:
        return {"error": TASK_FORCE_NOT_OWNED}
    if cur.execute("""SELECT id FROM task_forces
        WHERE player_id=? AND id!=? AND (name=? COLLATE NOCASE
                                         OR call_sign=? COLLATE NOCASE)""",
        (sess.player_id, task_force_id, call_sign, call_sign)).fetchone():
        return {"error": f"'{call_sign}' is already taken by another of your task forces"}
    previous = tf["call_sign"]
    record_event_direct(cur, get_current_turn(), "task_force.call_sign_set",
        actor_id=sess.player_id, subject_id=task_force_id, subject_type="task_force",
        payload={"task_force_id": task_force_id, "name": tf["name"],
                 "from": previous, "to": call_sign})
    cur.execute("UPDATE task_forces SET call_sign=? WHERE id=?", (call_sign, task_force_id))
    return {"ok": True, "task_force_id": task_force_id, "name": tf["name"],
            "previous_call_sign": previous, "call_sign": call_sign}


@mcp_tool(
    "Create a named task force -- a roster of your own ships (never "
    "colonies) you can later order together. org_ids is the initial roster "
    "(may be empty); every id must be one of your own ships, not already in "
    "another task force. Name must be unique among your own task forces.")
@player_tool
def create_task_force(sess, name: str, org_ids: list = None) -> dict:
    """
    Create a task force with an initial roster.

    Validates every listed org before writing anything -- a bad id anywhere
    in org_ids refuses the whole call rather than creating a partial roster,
    the same all-or-nothing shape set_mission's colonize charge uses.
    """
    name = (name or "").strip()
    if not name:
        return {"error": "Name cannot be empty"}
    if len(name) > MAX_TASK_FORCE_NAME_LENGTH:
        return {"error": f"Name is {len(name)} characters; limit is {MAX_TASK_FORCE_NAME_LENGTH}"}
    cur = sess.cur
    if cur.execute("SELECT id FROM task_forces WHERE player_id=? AND name=? COLLATE NOCASE",
                   (sess.player_id, name)).fetchone():
        return {"error": f"You already have a task force named '{name}'"}
    org_ids = org_ids or []
    for org_id in org_ids:
        org, err = _own_ship(sess, org_id)
        if err:
            return err
        if org["task_force_id"] is not None:
            return {"error": f"Organization {org_id} is already in task force {org['task_force_id']}"}
    cur.execute("INSERT INTO task_forces (player_id, name) VALUES (?, ?)", (sess.player_id, name))
    task_force_id = cur.lastrowid
    # record_event_direct (same open cur/transaction), not record_event (a
    # separate connection): the roster INSERT above already holds this
    # transaction's write lock, and db/connection.py sets no busy_timeout, so
    # a second connection's write would fail immediately with "database is
    # locked" rather than wait for it.
    record_event_direct(cur, get_current_turn(), "task_force.created",
        actor_id=sess.player_id, subject_id=task_force_id, subject_type="task_force",
        payload={"task_force_id": task_force_id, "name": name, "org_ids": org_ids})
    for org_id in org_ids:
        cur.execute("UPDATE organizations SET task_force_id=? WHERE id=?", (task_force_id, org_id))
    return {"ok": True, "task_force_id": task_force_id, "name": name, "member_org_ids": org_ids}


@mcp_tool(
    "Add one of your own ships to one of your task forces by name. Refused "
    "if the ship is a colony, or already a member of any task force -- "
    "remove it from its current one first.")
@player_tool
def add_to_task_force(sess, task_force_id: int, org_id: int) -> dict:
    if not _own_task_force(sess, task_force_id):
        return {"error": TASK_FORCE_NOT_OWNED}
    org, err = _own_ship(sess, org_id)
    if err:
        return err
    if org["task_force_id"] is not None:
        if org["task_force_id"] == task_force_id:
            return {"error": "Already a member of this task force"}
        return {"error": f"Organization {org_id} is already in task force "
                         f"{org['task_force_id']} — remove it first"}
    record_event_direct(sess.cur, get_current_turn(), "task_force.member_added",
        actor_id=sess.player_id, subject_id=task_force_id, subject_type="task_force",
        payload={"task_force_id": task_force_id, "org_id": org_id})
    sess.cur.execute("UPDATE organizations SET task_force_id=? WHERE id=?", (task_force_id, org_id))
    return {"ok": True, "task_force_id": task_force_id, "org_id": org_id}


@mcp_tool(
    "Remove one of your ships from one of your task forces by name. "
    "Membership is the only thing this changes -- position, mission, and "
    "everything else about the ship stays exactly as it was.")
@player_tool
def remove_from_task_force(sess, task_force_id: int, org_id: int) -> dict:
    if not _own_task_force(sess, task_force_id):
        return {"error": TASK_FORCE_NOT_OWNED}
    cur = sess.cur
    org = cur.execute("SELECT id, task_force_id FROM organizations WHERE id=? AND player_id=?",
                      (org_id, sess.player_id)).fetchone()
    if not org:
        return {"error": ORG_NOT_OWNED}
    if org["task_force_id"] != task_force_id:
        return {"error": "Organization is not a member of this task force"}
    record_event_direct(cur, get_current_turn(), "task_force.member_removed",
        actor_id=sess.player_id, subject_id=task_force_id, subject_type="task_force",
        payload={"task_force_id": task_force_id, "org_id": org_id})
    cur.execute("UPDATE organizations SET task_force_id=NULL WHERE id=?", (org_id,))
    return {"ok": True, "task_force_id": task_force_id, "org_id": org_id}


@mcp_tool(
    "Disband one of your task forces. Every current member is simply freed "
    "-- membership is cleared, nothing else about the ships changes -- and "
    "the roster itself is deleted.")
@player_tool
def disband_task_force(sess, task_force_id: int) -> dict:
    tf = _own_task_force(sess, task_force_id)
    if not tf:
        return {"error": TASK_FORCE_NOT_OWNED}
    cur = sess.cur
    record_event_direct(cur, get_current_turn(), "task_force.disbanded",
        actor_id=sess.player_id, subject_id=task_force_id, subject_type="task_force",
        payload={"task_force_id": task_force_id, "name": tf["name"]})
    cur.execute("UPDATE organizations SET task_force_id=NULL WHERE task_force_id=?", (task_force_id,))
    cur.execute("DELETE FROM task_forces WHERE id=?", (task_force_id,))
    return {"ok": True, "task_force_id": task_force_id, "name": tf["name"]}


@mcp_tool(
    "List your task forces and their current membership.")
@player_tool
def list_task_forces(sess) -> dict:
    cur = sess.cur
    forces = cur.execute("SELECT id, name FROM task_forces WHERE player_id=? ORDER BY id",
                         (sess.player_id,)).fetchall()
    result = []
    for tf in forces:
        members = cur.execute(
            "SELECT id, name FROM organizations WHERE task_force_id=? ORDER BY id",
            (tf["id"],)).fetchall()
        result.append({"task_force_id": tf["id"], "name": tf["name"],
                       "members": [{"org_id": m["id"], "name": m["name"]} for m in members]})
    return {"ok": True, "task_forces": result}


@mcp_tool(
    "Order every current member of one of your task forces at once -- a "
    "fan-out, not a transaction. Same mission/params set_mission takes "
    "(e.g. mission='move' with dest_x/dest_y/dest_z), applied once per "
    "member org id through set_mission itself, so every per-ship lock and "
    "rule still applies. A member that cannot currently accept the order "
    "(in transit, mid-colonization, wrong state) fails for that member only "
    "and reports why -- the rest still go through. Nothing rolls back.")
@player_tool
def order_task_force(sess, task_force_id: int, mission: str, params: dict = None) -> dict:
    tf = _own_task_force(sess, task_force_id)
    if not tf:
        return {"error": TASK_FORCE_NOT_OWNED}
    member_ids = [r["id"] for r in sess.cur.execute(
        "SELECT id FROM organizations WHERE task_force_id=? ORDER BY id",
        (task_force_id,)).fetchall()]
    token = sess.player["player_token"]
    # set_mission is itself a @player_tool and opens its own connection per
    # call, so this one has to be out of the way first -- same reasoning
    # set_mission's own move delegation uses (see PlayerSession.release).
    sess.release()
    results = [{"org_id": org_id, **set_mission(token, org_id, mission, params)}
              for org_id in member_ids]
    return {"ok": True, "task_force_id": task_force_id, "name": tf["name"],
            "member_count": len(member_ids), "results": results}
