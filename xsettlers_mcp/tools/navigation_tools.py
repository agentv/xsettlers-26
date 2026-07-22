from db.connection import get_connection
from db.events import record_event
from engine.turn import get_current_turn
import math

def get_organizations_in_range(slack_user_id: str, ship_id: int, jump_range: int) -> list:
    """Return all sectors within Euclidean jump range of the given ship."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE slack_user_id=?", (slack_user_id,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""SELECT s1.coord_x,s1.coord_y,s1.coord_z FROM organizations o
        JOIN sectors s1 ON s1.id=o.sector_id
        WHERE o.id=? AND o.player_id=? AND o.sector_id!=-1""", (ship_id, player["id"]))
    origin = cur.fetchone()
    if not origin:
        conn.close(); return {"error": "Ship not found, not owned by player, or currently in transit"}
    r2 = jump_range ** 2
    cur.execute("""SELECT s2.id,s2.coord_x,s2.coord_y,s2.coord_z FROM sectors s2
        WHERE s2.id!=-1 AND (
            (s2.coord_x-?)*(s2.coord_x-?) +
            (s2.coord_y-?)*(s2.coord_y-?) +
            (s2.coord_z-?)*(s2.coord_z-?)
        ) <= ?""", (
        origin["coord_x"], origin["coord_x"],
        origin["coord_y"], origin["coord_y"],
        origin["coord_z"], origin["coord_z"], r2))
    sectors = [dict(r) for r in cur.fetchall()]; conn.close()
    return sectors

def preview_move(slack_user_id: str, ship_id: int,
                 dest_sector_id: int, jump_range_per_turn: int = 1) -> dict:
    """
    Pure read — calculates travel time WITHOUT committing anything.
    Returns turns_needed and arrival_turn. No DB writes, no event logged.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE slack_user_id=?", (slack_user_id,))
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
    cur.execute("SELECT coord_x,coord_y,coord_z FROM sectors WHERE id=?", (dest_sector_id,))
    dest = cur.fetchone()
    if not dest:
        conn.close(); return {"error": "Destination sector not found"}
    distance = math.sqrt(
        (dest["coord_x"]-ship["coord_x"])**2 +
        (dest["coord_y"]-ship["coord_y"])**2 +
        (dest["coord_z"]-ship["coord_z"])**2)
    turns_needed = max(1, math.ceil(distance / jump_range_per_turn))
    current_turn = get_current_turn()
    conn.close()
    return {"preview": True, "ship_id": ship_id, "from_sector_id": ship["sector_id"],
            "dest_sector_id": dest_sector_id, "turns_needed": turns_needed,
            "arrival_turn": current_turn + turns_needed}

def confirm_move(slack_user_id: str, ship_id: int,
                 dest_sector_id: int, jump_range_per_turn: int = 1) -> dict:
    """
    Commit a previewed move:
    1. Write-ahead: log ship.move_confirmed BEFORE mutating state
    2. Park ship at sentinel sector (-1)
    3. Insert arrival_queue row with origin_sector_id for rubber-band cancel support
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE slack_user_id=?", (slack_user_id,))
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
    cur.execute("SELECT coord_x,coord_y,coord_z FROM sectors WHERE id=?", (dest_sector_id,))
    dest = cur.fetchone()
    if not dest:
        conn.close(); return {"error": "Destination sector not found"}
    origin_sector_id = ship["sector_id"]
    distance = math.sqrt(
        (dest["coord_x"]-ship["coord_x"])**2 +
        (dest["coord_y"]-ship["coord_y"])**2 +
        (dest["coord_z"]-ship["coord_z"])**2)
    turns_needed = max(1, math.ceil(distance / jump_range_per_turn))
    current_turn = get_current_turn()
    arrival_turn = current_turn + turns_needed
    # Write-ahead: log BEFORE mutating state
    record_event(
        event_type="ship.move_confirmed",
        payload={"org_id": ship_id, "from_sector_id": origin_sector_id,
                 "to_sector_id": dest_sector_id, "arrival_turn": arrival_turn},
        actor_id=player["id"], subject_id=ship_id, subject_type="organization")
    # Park at sentinel, locking the org against reassignment until arrival/cancel
    cur.execute("""UPDATE organizations SET sector_id=-1, mission='move', mission_params=?,
        is_mobile=0 WHERE id=?""",
        (f'{{"dest_sector_id":{dest_sector_id},"arrival_turn":{arrival_turn}}}', ship_id))
    # Queue arrival
    cur.execute("""INSERT OR REPLACE INTO arrival_queue
        (arrival_turn,org_id,dest_sector_id,origin_sector_id) VALUES (?,?,?,?)""",
        (arrival_turn, ship_id, dest_sector_id, origin_sector_id))
    conn.commit(); conn.close()
    return {"confirmed": True, "ship_id": ship_id, "from_sector_id": origin_sector_id,
            "dest_sector_id": dest_sector_id, "arrival_turn": arrival_turn,
            "turns_needed": turns_needed}

def cancel_move(slack_user_id: str, ship_id: int) -> dict:
    """
    Cancel a move in progress. Rubber-bands the ship to its origin_sector_id.
    Logs ship.move_cancelled (write-ahead) before mutating state.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE slack_user_id=?", (slack_user_id,))
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
