from db.connection import get_connection
from db.events import record_event
from engine.turn import get_current_turn
import math

def preview_move(player_token: str, ship_id: int,
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
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""SELECT o.id,o.is_mobile,s.coord_x,s.coord_y,s.coord_z,s.id AS sector_id
        FROM organizations o JOIN sectors s ON s.id=o.sector_id
        WHERE o.id=? AND o.player_id=? AND o.sector_id!=-1""", (ship_id, player["id"]))
    ship = cur.fetchone()
    if not ship:
        conn.close(); return {"error": "Ship not found, not owned by player, or already in transit"}
    if not ship["is_mobile"]:
        conn.close(); return {"error": "This organization is locked (colony or mid-colonization) and cannot move"}
    if dest_x < 0 or dest_y < 0 or dest_z < 0:
        conn.close(); return {"error": "Destination coordinates cannot be negative -- space has no negative indices"}
    distance = math.sqrt(
        (dest_x-ship["coord_x"])**2 +
        (dest_y-ship["coord_y"])**2 +
        (dest_z-ship["coord_z"])**2)
    turns_needed = max(1, math.ceil(distance / jump_range_per_turn))
    current_turn = get_current_turn()
    conn.close()
    return {"preview": True, "ship_id": ship_id, "from_sector_id": ship["sector_id"],
            "dest_x": dest_x, "dest_y": dest_y, "dest_z": dest_z, "turns_needed": turns_needed,
            "arrival_turn": current_turn + turns_needed + 1}

def confirm_move(player_token: str, ship_id: int,
                 dest_x: int, dest_y: int, dest_z: int, jump_range_per_turn: int = 1) -> dict:
    """
    Commit a previewed move:
    1. Write-ahead: log ship.move_confirmed BEFORE mutating state
    2. Park ship at sentinel sector (-1)
    3. Insert arrival_queue row with origin_sector_id for rubber-band cancel support
    Destination is any coordinate triple -- it's only revealed (get-or-created
    as a real sectors row, see db/sectors.py) once the ship actually arrives.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""SELECT o.id,o.is_mobile,s.coord_x,s.coord_y,s.coord_z,s.id AS sector_id
        FROM organizations o JOIN sectors s ON s.id=o.sector_id
        WHERE o.id=? AND o.player_id=? AND o.sector_id!=-1""", (ship_id, player["id"]))
    ship = cur.fetchone()
    if not ship:
        conn.close(); return {"error": "Ship not found, not owned by player, or already in transit"}
    if not ship["is_mobile"]:
        conn.close(); return {"error": "This organization is locked (colony or mid-colonization) and cannot move"}
    if dest_x < 0 or dest_y < 0 or dest_z < 0:
        conn.close(); return {"error": "Destination coordinates cannot be negative -- space has no negative indices"}
    origin_sector_id = ship["sector_id"]
    distance = math.sqrt(
        (dest_x-ship["coord_x"])**2 +
        (dest_y-ship["coord_y"])**2 +
        (dest_z-ship["coord_z"])**2)
    turns_needed = max(1, math.ceil(distance / jump_range_per_turn))
    current_turn = get_current_turn()
    # +1: arrival_turn names the turn the ship is free to act, one turn after
    # the end_of_turn() pass that actually performs the landing (see
    # engine/turn.py's arrival resolution query, which is offset to match).
    arrival_turn = current_turn + turns_needed + 1
    # Write-ahead: log BEFORE mutating state
    record_event(
        event_type="ship.move_confirmed",
        payload={"org_id": ship_id, "from_sector_id": origin_sector_id,
                 "to_x": dest_x, "to_y": dest_y, "to_z": dest_z, "arrival_turn": arrival_turn},
        actor_id=player["id"], subject_id=ship_id, subject_type="organization")
    # Park at sentinel, locking the org against reassignment until arrival/cancel
    cur.execute("""UPDATE organizations SET sector_id=-1, mission='move', mission_params=?,
        is_mobile=0 WHERE id=?""",
        (f'{{"dest_x":{dest_x},"dest_y":{dest_y},"dest_z":{dest_z},"arrival_turn":{arrival_turn}}}', ship_id))
    # Queue arrival
    cur.execute("""INSERT OR REPLACE INTO arrival_queue
        (arrival_turn,org_id,dest_x,dest_y,dest_z,origin_sector_id) VALUES (?,?,?,?,?,?)""",
        (arrival_turn, ship_id, dest_x, dest_y, dest_z, origin_sector_id))
    conn.commit(); conn.close()
    return {"confirmed": True, "ship_id": ship_id, "from_sector_id": origin_sector_id,
            "dest_x": dest_x, "dest_y": dest_y, "dest_z": dest_z, "arrival_turn": arrival_turn,
            "turns_needed": turns_needed}

def cancel_move(player_token: str, ship_id: int) -> dict:
    """
    Cancel a move in progress. Rubber-bands the ship to its origin_sector_id.
    Logs ship.move_cancelled (write-ahead) before mutating state.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""SELECT id,mission FROM organizations
        WHERE id=? AND player_id=? AND sector_id=-1""", (ship_id, player["id"]))
    ship = cur.fetchone()
    if not ship:
        conn.close(); return {"error": "Ship not found, not owned by player, or not in transit"}
    cur.execute("SELECT origin_sector_id FROM arrival_queue WHERE org_id=?", (ship_id,))
    row = cur.fetchone()
    if not row:
        conn.close(); return {"error": "No arrival_queue entry found for this ship"}
    origin_sector_id = row["origin_sector_id"]
    # Write-ahead
    record_event(
        event_type="ship.move_cancelled",
        payload={"org_id": ship_id, "rubber_banded_to_sector_id": origin_sector_id},
        actor_id=player["id"], subject_id=ship_id, subject_type="organization")
    cur.execute("DELETE FROM arrival_queue WHERE org_id=?", (ship_id,))
    cur.execute("""UPDATE organizations SET sector_id=?, mission='idle', mission_params=NULL,
        is_mobile=1 WHERE id=?""", (origin_sector_id, ship_id))
    conn.commit(); conn.close()
    return {"cancelled": True, "ship_id": ship_id,
            "rubber_banded_to_sector_id": origin_sector_id}
