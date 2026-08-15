from xsettlers_mcp.tools.registry import mcp_tool
from db.events import record_event
from engine.turn import get_current_turn
from engine.movement import apply_confirm_move, plan_move
from xsettlers_mcp.tools.session import player_tool

NOT_MOVABLE = "Ship not found, not owned by player, or already in transit"
LOCKED = "This organization is locked (colony or mid-colonization) and cannot move"
NEGATIVE_DEST = "Destination coordinates cannot be negative -- space has no negative indices"


def _departable_ship(sess, ship_id: int, dest):
    """
    The caller's ship, at a real sector, free to leave -- or (None, error).

    preview_move and confirm_move asked exactly the same three questions in
    the same order (do you own it and is it here, is it mobile, is the
    destination legal) and answered them with the same three error strings, so
    they ask once now. Returns the origin row including coordinates; the
    committing caller ignores them, the preview needs them to measure distance.
    """
    ship = sess.cur.execute("""SELECT o.id, o.is_mobile, s.id AS sector_id,
                                   s.coord_x, s.coord_y, s.coord_z
        FROM organizations o JOIN sectors s ON s.id = o.sector_id
        WHERE o.id=? AND o.player_id=? AND o.sector_id!=-1""",
        (ship_id, sess.player_id)).fetchone()
    if not ship:
        return None, {"error": NOT_MOVABLE}
    if not ship["is_mobile"]:
        return None, {"error": LOCKED}
    if any(c < 0 for c in dest):
        return None, {"error": NEGATIVE_DEST}
    return ship, None


@mcp_tool(
    "Preview a move: calculate travel time without committing")
@player_tool
def preview_move(sess, ship_id: int,
                 dest_x: int, dest_y: int, dest_z: int, jump_range_per_turn: int = 1) -> dict:
    """
    Quote a move without committing to it: same ownership, mobility and
    destination checks confirm_move applies, then the travel cost it would
    charge. No DB writes, no event logged.

    A separate tool rather than a flag on confirm_move, deliberately: the
    caller is usually a language model, and `preview_move` cannot launch a
    fleet however it is invoked, whereas one forgotten `dry_run=True` can. The
    arithmetic itself is shared (engine.movement.plan_move), so the quote and
    the commit cannot disagree about travel time.

    Worth quoting first because commitment is expensive: a moving ship
    produces no energy, and undoing a move (cancel_move) rubber-bands it back
    to where it started, having burned the turns for nothing.

    Destination is any coordinate triple -- sectors are lazily instantiated
    (see db/sectors.py), so it need not exist yet.
    """
    ship, err = _departable_ship(sess, ship_id, (dest_x, dest_y, dest_z))
    if err:
        return err
    plan = plan_move((ship["coord_x"], ship["coord_y"], ship["coord_z"]),
                     (dest_x, dest_y, dest_z), jump_range_per_turn, get_current_turn())
    return {"preview": True, "ship_id": ship_id, "from_sector_id": ship["sector_id"],
            "dest_x": dest_x, "dest_y": dest_y, "dest_z": dest_z,
            "turns_needed": plan["turns_needed"], "arrival_turn": plan["arrival_turn"]}

@mcp_tool(
    "Commit a previewed move. Ship enters transit until arrival turn.")
@player_tool
def confirm_move(sess, ship_id: int,
                 dest_x: int, dest_y: int, dest_z: int, jump_range_per_turn: int = 1) -> dict:
    """
    Commit a previewed move: validates ownership/mobility, then delegates the
    actual mutation (write-ahead event, park at sentinel sector, queue
    arrival) to engine.movement.apply_confirm_move -- the same core logic
    engine/turn.py's ship's-log dispatch uses to fire a chained move from
    inside its own open transaction (see that module's docstring for why the
    mutation logic had to be split out rather than called directly).
    Destination is any coordinate triple -- it's only revealed (get-or-created
    as a real sectors row, see db/sectors.py) once the ship actually arrives.
    """
    _, err = _departable_ship(sess, ship_id, (dest_x, dest_y, dest_z))
    if err:
        return err
    return apply_confirm_move(sess.cur, ship_id, sess.player_id, dest_x, dest_y, dest_z,
                              jump_range_per_turn, get_current_turn())

@mcp_tool(
    "Cancel a move in progress. Rubber-bands ship to origin sector.")
@player_tool
def cancel_move(sess, ship_id: int) -> dict:
    """
    Cancel a move in progress. Rubber-bands the ship to its origin_sector_id.
    Logs ship.move_cancelled (write-ahead) before mutating state.
    """
    if not sess.cur.execute("""SELECT id FROM organizations
            WHERE id=? AND player_id=? AND sector_id=-1""",
            (ship_id, sess.player_id)).fetchone():
        return {"error": "Ship not found, not owned by player, or not in transit"}
    row = sess.cur.execute("SELECT origin_sector_id FROM arrival_queue WHERE org_id=?",
                        (ship_id,)).fetchone()
    if not row:
        return {"error": "No arrival_queue entry found for this ship"}
    origin_sector_id = row["origin_sector_id"]
    # Write-ahead
    record_event(
        event_type="ship.move_cancelled",
        payload={"org_id": ship_id, "rubber_banded_to_sector_id": origin_sector_id},
        actor_id=sess.player_id, subject_id=ship_id, subject_type="organization")
    sess.cur.execute("DELETE FROM arrival_queue WHERE org_id=?", (ship_id,))
    sess.cur.execute("""UPDATE organizations SET sector_id=?, mission='idle', mission_params=NULL,
        is_mobile=1 WHERE id=?""", (origin_sector_id, ship_id))
    return {"cancelled": True, "ship_id": ship_id,
            "rubber_banded_to_sector_id": origin_sector_id}
