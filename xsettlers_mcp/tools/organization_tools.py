from db.connection import get_connection
from db.events import record_event
from engine.turn import get_current_turn
import json

VALID_ORG_MISSIONS = {"idle", "move", "colonize", "defend", "attack"}
VALID_POD_MISSIONS = {"idle", "produce_energy", "produce_food", "produce_goods", "scan"}

def set_mission(player_token: str, org_id: int, mission: str, params: dict = None) -> dict:
    """
    Set an organization's mission. Validates ownership and mission type, and
    enforces the three org-lock states (see Data Model & Storage Design):
      - in transit (sector_id == -1): locked entirely, must cancel_move first
      - colony: locked against 'move' only — defend/attack/idle remain assignable
      - mid-colonization (ship, is_mobile == 0, not in transit): locked entirely,
        committed for the 3-turn transition window
    Setting mission='colonize' flips is_mobile to 0 immediately and schedules a
    colonize_complete event 3 turns out for engine/turn.py to resolve.
    """
    if mission not in VALID_ORG_MISSIONS:
        return {"error": f"Invalid mission '{mission}'. Valid: {sorted(VALID_ORG_MISSIONS)}"}
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("SELECT id,is_mobile,org_type,sector_id FROM organizations WHERE id=? AND player_id=?",
                (org_id, player["id"]))
    org = cur.fetchone()
    if not org:
        conn.close(); return {"error": "Organization not found or not owned by player"}
    if org["sector_id"] == -1:
        conn.close()
        return {"error": "This organization is currently in transit — call cancel_move before assigning a new mission"}
    if org["org_type"] == "colony":
        if mission == "move":
            conn.close(); return {"error": "Colonies cannot move"}
    elif not org["is_mobile"]:
        conn.close()
        return {"error": "This ship is committed to colonizing and cannot be reassigned until the transition resolves"}
    record_event(
        event_type="mission.set",
        payload={"org_id": org_id, "mission": mission, "params": params or {}},
        actor_id=player["id"], subject_id=org_id, subject_type="organization")
    if mission == "colonize":
        resolve_turn = get_current_turn() + 3
        record_event(
            event_type="colonize_complete",
            payload={"org_id": org_id, "sector_id": org["sector_id"]},
            actor_id=player["id"], subject_id=org_id, subject_type="organization",
            resolve_at_turn=resolve_turn)
        cur.execute("UPDATE organizations SET mission=?, mission_params=?, is_mobile=0 WHERE id=?",
                    (mission, json.dumps(params) if params else None, org_id))
    else:
        cur.execute("UPDATE organizations SET mission=?, mission_params=? WHERE id=?",
                    (mission, json.dumps(params) if params else None, org_id))
    conn.commit(); conn.close()
    return {"ok": True, "org_id": org_id, "mission": mission}

def set_pod_mission(player_token: str, pod_id: int, mission: str,
                   target_sector_id: int = None) -> dict:
    """
    Set a pod's mission. Validates ownership and mission type.
    If mission='scan' and target_sector_id is provided, sets the scan target in the same call.
    If mission='scan' and target_sector_id is omitted, the pod enters scan mission with no
    target — set_pod_scan_target must be called separately before end of turn.
    """
    if mission not in VALID_POD_MISSIONS:
        return {"error": f"Invalid pod mission '{mission}'. Valid: {sorted(VALID_POD_MISSIONS)}"}
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""SELECT p.id FROM pods p JOIN organizations o ON o.id=p.org_id
        WHERE p.id=? AND o.player_id=?""", (pod_id, player["id"]))
    pod = cur.fetchone()
    if not pod:
        conn.close(); return {"error": "Pod not found or not owned by player"}
    params = {}
    if mission == "scan" and target_sector_id is not None:
        params["target_sector_id"] = target_sector_id
    record_event(
        event_type="pod.mission_set",
        payload={"pod_id": pod_id, "mission": mission, "params": params},
        actor_id=player["id"], subject_id=pod_id, subject_type="pod")
    cur.execute("UPDATE pods SET mission=?, mission_params=? WHERE id=?",
                (mission, json.dumps(params) if params else None, pod_id))
    conn.commit(); conn.close()
    return {"ok": True, "pod_id": pod_id, "mission": mission,
            "target_sector_id": target_sector_id}

def set_pod_scan_target(player_token: str, pod_id: int, target_sector_id: int) -> dict:
    """
    Assign or change the scan target sector for a pod already in 'scan' mission.
    Range is validated at end-of-turn resolution, not here — an out-of-range target
    is accepted but will trigger an alert.scan_out_of_range event at resolution.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""SELECT p.id, p.mission FROM pods p JOIN organizations o ON o.id=p.org_id
        WHERE p.id=? AND o.player_id=?""", (pod_id, player["id"]))
    pod = cur.fetchone()
    if not pod:
        conn.close(); return {"error": "Pod not found or not owned by player"}
    if pod["mission"] != "scan":
        conn.close(); return {"error": "Pod is not in scan mission — set mission to 'scan' first"}
    params = {"target_sector_id": target_sector_id}
    record_event(
        event_type="pod.scan_target_set",
        payload={"pod_id": pod_id, "target_sector_id": target_sector_id},
        actor_id=player["id"], subject_id=pod_id, subject_type="pod")
    cur.execute("UPDATE pods SET mission_params=? WHERE id=?",
                (json.dumps(params), pod_id))
    conn.commit(); conn.close()
    return {"ok": True, "pod_id": pod_id, "target_sector_id": target_sector_id}

def show_organization(player_token: str, org_id: int) -> dict:
    """
    Return the complete properties of one of the player's own organizations:
    org record (type, mission, mission_params, is_mobile, sector location)
    plus all pods with their mission and mission_params.
    Ownership-gated — only the calling player's orgs are accessible.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""
        SELECT o.id, o.org_type, o.name, o.mission, o.mission_params,
               o.is_mobile, o.sector_id,
               s.coord_x, s.coord_y, s.coord_z
        FROM organizations o
        LEFT JOIN sectors s ON s.id = o.sector_id
        WHERE o.id = ? AND o.player_id = ?""", (org_id, player["id"]))
    org = cur.fetchone()
    if not org:
        conn.close(); return {"error": "Organization not found or not owned by player"}
    result = dict(org)
    cur.execute("SELECT id, mission, mission_params, storage_current, storage_capacity,"
                "       energy_consumption, food_consumption"
                " FROM pods WHERE org_id = ?", (org_id,))
    result["pods"] = [dict(p) for p in cur.fetchall()]
    conn.close()
    return result

def show_game_status(player_token: str) -> dict:
    """
    Return a player-scoped game status summary:
    - Turn context: current turn and turn limit
    - All player organizations (ships and colonies) with name, org_type, mission,
      and sector location. Ships in transit are marked with in_transit=True,
      destination sector, and expected arrival turn.
    - Accumulated assets: aggregate energy, food, goods, and total across all pods.
    Ownership-gated — only the calling player's data.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}

    # Turn context
    cur.execute("SELECT current_turn FROM game_state WHERE id=1")
    current_turn = cur.fetchone()["current_turn"]
    import os
    turn_limit = int(os.getenv("TURN_LIMIT", 20))

    # Organizations
    cur.execute("""
        SELECT o.id, o.name, o.org_type, o.mission, o.mission_params,
               o.sector_id, s.coord_x, s.coord_y, s.coord_z
        FROM organizations o
        LEFT JOIN sectors s ON s.id = o.sector_id
        WHERE o.player_id = ?
        ORDER BY o.org_type, o.name""", (player["id"],))
    orgs_raw = cur.fetchall()
    orgs = []
    for o in orgs_raw:
        entry = {
            "id": o["id"],
            "name": o["name"],
            "org_type": o["org_type"],
            "mission": o["mission"],
        }
        if o["sector_id"] == -1:
            # In transit — fetch destination and arrival turn from arrival_queue
            cur.execute("""
                SELECT dest_sector_id, arrival_turn,
                       s.coord_x, s.coord_y, s.coord_z
                FROM arrival_queue aq
                JOIN sectors s ON s.id = aq.dest_sector_id
                WHERE aq.org_id = ?""", (o["id"],))
            aq = cur.fetchone()
            entry["in_transit"] = True
            if aq:
                entry["dest_sector"] = {
                    "id": aq["dest_sector_id"],
                    "coords": [aq["coord_x"], aq["coord_y"], aq["coord_z"]]
                }
                entry["arrival_turn"] = aq["arrival_turn"]
        else:
            entry["in_transit"] = False
            entry["sector"] = {
                "id": o["sector_id"],
                "coords": [o["coord_x"], o["coord_y"], o["coord_z"]]
            }
        orgs.append(entry)

    # Accumulated assets — aggregate across all pods for this player
    cur.execute("""
        SELECT
            SUM(CASE WHEN p.mission = 'produce_energy' THEN p.storage_current ELSE 0 END) AS energy,
            SUM(CASE WHEN p.mission = 'produce_food'   THEN p.storage_current ELSE 0 END) AS food,
            SUM(CASE WHEN p.mission = 'produce_goods'  THEN p.storage_current ELSE 0 END) AS goods,
            SUM(p.storage_current) AS total
        FROM pods p
        JOIN organizations o ON o.id = p.org_id
        WHERE o.player_id = ?""", (player["id"],))
    assets_row = cur.fetchone()
    assets = {
        "energy": round(assets_row["energy"] or 0, 2),
        "food":   round(assets_row["food"]   or 0, 2),
        "goods":  round(assets_row["goods"]  or 0, 2),
        "total":  round(assets_row["total"]  or 0, 2),
    }

    conn.close()
    return {
        "turn": current_turn,
        "turn_limit": turn_limit,
        "organizations": orgs,
        "assets": assets,
    }
