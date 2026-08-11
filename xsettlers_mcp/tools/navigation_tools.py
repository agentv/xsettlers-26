from db.events import record_event
from engine.turn import get_current_turn
from engine.movement import apply_confirm_move
from xsettlers_mcp.tools.session import player_tool
import math

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


@player_tool
def preview_move(sess, ship_id: int,
                 dest_x: int, dest_y: int, dest_z: int, jump_range_per_turn: int = 1) -> dict:
    """
    Pure read — calculates travel time WITHOUT committing anything.
    Returns turns_needed and arrival_turn. No DB writes, no event logged.
    Destination is any coordinate triple -- sectors are lazily instantiated
    (see db/sectors.py), so the destination need not exist yet.

    arrival_turn is the turn number the ship is actually free to act again --
    not the turn whose end_of_turn() pass performs the landing. Landing
    happens one turn earlier than that (see engine/turn.py's arrival
    resolution, which fires a turn ahead of the stored value for exactly this
    reason), so a player reading "arrival_turn: 5" can plan the ship's turn-5
    orders directly instead of accounting for a hidden one-turn lag.
    """
    ship, err = _departable_ship(sess, ship_id, (dest_x, dest_y, dest_z))
    if err:
        return err
    distance = math.sqrt(
        (dest_x-ship["coord_x"])**2 +
        (dest_y-ship["coord_y"])**2 +
        (dest_z-ship["coord_z"])**2)
    turns_needed = max(1, math.ceil(distance / jump_range_per_turn))
    return {"preview": True, "ship_id": ship_id, "from_sector_id": ship["sector_id"],
            "dest_x": dest_x, "dest_y": dest_y, "dest_z": dest_z, "turns_needed": turns_needed,
            "arrival_turn": get_current_turn() + turns_needed + 1}

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
