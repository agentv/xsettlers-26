from db.connection import get_connection


def get_scan_range(org_id: int) -> int:
    """
    Returns the scan range for an org.
    POC: always returns 1.
    Future: sum range contributions from pods in scan mission.
    """
    return 1


# Note: `scan_sector` is no longer a player-callable action. Scanning is now
# executed by the engine at end of turn for all pods in `scan` mission with a
# valid target. See `engine/turn.py` for scan resolution logic.


def get_sector(slack_user_id: str, sector_id: int) -> dict:
    """Return sector info — only if the player has visibility (confidence > 0)."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE slack_user_id=?", (slack_user_id,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""SELECT s.*, ps.confidence FROM sectors s
        JOIN player_sectors ps ON ps.sector_id=s.id
        WHERE s.id=? AND ps.player_id=? AND ps.confidence>0""", (sector_id, player["id"]))
    sector = cur.fetchone(); conn.close()
    if not sector: return {"error": "Sector not visible or does not exist"}
    return dict(sector)

def get_sector_map(slack_user_id: str) -> list:
    """Return all sectors visible to this player, ordered by confidence."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE slack_user_id=?", (slack_user_id,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""SELECT s.id,s.coord_x,s.coord_y,s.coord_z,
               s.energy_capacity,s.food_capacity,s.goods_capacity,ps.confidence
        FROM sectors s JOIN player_sectors ps ON ps.sector_id=s.id
        WHERE ps.player_id=? AND ps.confidence>0 ORDER BY ps.confidence DESC""", (player["id"],))
    sectors = [dict(r) for r in cur.fetchall()]; conn.close()
    return sectors


def show_sector_neighborhood(
        slack_user_id: str,
        org_id: int = None,
        center_x: int = None, center_y: int = None, center_z: int = None,
        radius: int = 2) -> list:
    """
    Return all sectors within Euclidean distance <= radius of a center point,
    filtered by the player's fog-of-war (confidence > 0).
    Center is either resolved from org_id (uses org's current sector coords)
    or supplied directly as (center_x, center_y, center_z).
    Ships in transit (sector_id = -1) are not valid org_id centers.
    POC default radius: 2.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE slack_user_id=?", (slack_user_id,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    if org_id is not None:
        cur.execute("""SELECT s.coord_x, s.coord_y, s.coord_z
            FROM organizations o JOIN sectors s ON s.id = o.sector_id
            WHERE o.id=? AND o.player_id=? AND o.sector_id != -1""",
            (org_id, player["id"]))
        origin = cur.fetchone()
        if not origin:
            conn.close(); return {"error": "Organization not found, not owned by player, or currently in transit"}
        cx, cy, cz = origin["coord_x"], origin["coord_y"], origin["coord_z"]
    elif None not in (center_x, center_y, center_z):
        cx, cy, cz = center_x, center_y, center_z
    else:
        conn.close(); return {"error": "Must supply either org_id or (center_x, center_y, center_z)"}
    r2 = radius ** 2
    cur.execute("""
        SELECT s.id, s.coord_x, s.coord_y, s.coord_z,
               s.energy_capacity, s.food_capacity, s.goods_capacity, ps.confidence
        FROM sectors s
        JOIN player_sectors ps ON ps.sector_id = s.id
        WHERE ps.player_id = ? AND ps.confidence > 0
          AND s.id != -1
          AND (
            (s.coord_x - ?) * (s.coord_x - ?) +
            (s.coord_y - ?) * (s.coord_y - ?) +
            (s.coord_z - ?) * (s.coord_z - ?)
          ) <= ?
        ORDER BY ps.confidence DESC""",
        (player["id"], cx, cx, cy, cy, cz, cz, r2))
    sectors = [dict(r) for r in cur.fetchall()]
    conn.close()
    return sectors
