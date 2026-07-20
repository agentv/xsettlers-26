import os, json
from db.connection import get_connection
from engine.production import get_production, get_consumption

CONFIDENCE_DECAY = float(os.getenv("CONFIDENCE_DECAY", 0.9))
TURN_LIMIT = int(os.getenv("TURN_LIMIT", 20))

def get_current_turn() -> int:
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT current_turn FROM game_state WHERE id=1")
    turn = cur.fetchone()[0]; conn.close(); return turn

def is_game_over() -> bool:
    return get_current_turn() >= TURN_LIMIT

def end_of_turn():
    if is_game_over():
        print("Turn limit reached — game over."); return
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM games WHERE id=1")
    if cur.fetchone()[0] == 0:
        # No scenario selected yet (mcp/game_select.py's select_scenario populates
        # this on bootstrap). Nothing to process -- don't burn turns on an empty game.
        conn.close(); return

    # 1. Reset declarations
    cur.execute("UPDATE players SET end_turn_declared=0")

    # 2. Resolve arrivals
    cur.execute("SELECT current_turn FROM game_state WHERE id=1")
    current_turn = cur.fetchone()[0]
    cur.execute("SELECT org_id,dest_sector_id FROM arrival_queue WHERE arrival_turn<=?",
                (current_turn,))
    for arrival in cur.fetchall():
        cur.execute("""UPDATE organizations SET sector_id=?,mission='idle',mission_params=NULL,
                       is_mobile=1 WHERE id=?""", (arrival["dest_sector_id"],arrival["org_id"]))
        org = cur.execute("SELECT player_id FROM organizations WHERE id=?",
                          (arrival["org_id"],)).fetchone()
        if org:
            cur.execute("""INSERT INTO player_sectors (player_id,sector_id,confidence) VALUES (?,?,100)
                ON CONFLICT(player_id,sector_id) DO UPDATE SET confidence=100""",
                (org["player_id"],arrival["dest_sector_id"]))
    cur.execute("DELETE FROM arrival_queue WHERE arrival_turn<=?", (current_turn,))

    # 3. Pod consumption then production.
    #    Order matters: consume first, then produce.
    #    Produce missions run for all pods regardless of transit state.
    #    Scan resolution runs only for stationary orgs (transit suppresses scan).
    cur.execute("SELECT p.id,p.mission,p.mission_params,"
                "p.storage_capacity,p.storage_current,p.org_id,"
                "p.energy_consumption,p.food_consumption FROM pods p")
    for pod in cur.fetchall():
        mission = pod["mission"]

        # 3a. Consumption — deduct energy and food before any production
        # TODO: deduct from org-level resource pool once pool is implemented
        consumption = get_consumption(pod)
        # placeholder: consumption logged but not yet deducted (deferred — see Known TODOs)

        # 3b. Production
        if mission in ("produce_energy", "produce_food", "produce_goods"):
            for resource, amount in get_production(mission).items():
                new_level = min(pod["storage_capacity"], pod["storage_current"] + amount)
                cur.execute("UPDATE pods SET storage_current=? WHERE id=?",
                            (new_level, pod["id"]))

        # 3c. Scan resolution (stationary orgs only)
        elif mission == "scan":
            org = cur.execute(
                "SELECT sector_id,player_id FROM organizations WHERE id=?",
                (pod["org_id"],)).fetchone()
            if org and org["sector_id"] != -1:
                params = json.loads(pod["mission_params"] or "{}")
                target_id = params.get("target_sector_id")
                if target_id:
                    # TODO: validate Euclidean range via get_scan_range()
                    cur.execute("""
                        INSERT INTO player_sectors (player_id,sector_id,confidence)
                        VALUES (?,?,100)
                        ON CONFLICT(player_id,sector_id) DO UPDATE SET confidence=100""",
                        (org["player_id"], target_id))
                    # TODO: emit pod.scanned event; detect rivals

    # 4. Colonization resolution — only orgs whose 3-turn colonize_complete event has matured.
    #    Scheduled by set_mission() via record_event(resolve_at_turn=...). Idempotent via the
    #    org_type='ship' filter: once resolved, org_type flips to 'colony' and this stops
    #    matching, so no separate "resolved" flag is needed.
    cur.execute("""
        SELECT o.id,o.org_type,o.player_id,o.sector_id,o.mission,o.mission_params
        FROM events e
        JOIN organizations o ON o.id = e.subject_id AND e.subject_type = 'organization'
        WHERE e.event_type = 'colonize_complete' AND e.resolve_at_turn <= ?
          AND o.org_type = 'ship' AND o.mission = 'colonize'
    """, (current_turn,))
    for org in cur.fetchall():
        _handle_colonize(cur, org, current_turn)

    # 5. Mission dispatch (defend/attack — stationary orgs only; stubs for now)
    cur.execute("""SELECT id,org_type,player_id,sector_id,mission,mission_params
        FROM organizations WHERE mission IN ('defend','attack') AND sector_id!=-1""")
    for org in cur.fetchall():
        params = json.loads(org["mission_params"] or "{}")
        {"defend": _handle_defend, "attack": _handle_attack}.get(org["mission"], lambda *a: None)(cur, org, params)

    # 5. Fog decay
    cur.execute("""SELECT ps.player_id,ps.sector_id,ps.confidence FROM player_sectors ps
        WHERE ps.confidence>0 AND NOT EXISTS (
            SELECT 1 FROM organizations o WHERE o.sector_id=ps.sector_id AND o.player_id=ps.player_id
        )""")
    for row in cur.fetchall():
        cur.execute("UPDATE player_sectors SET confidence=? WHERE player_id=? AND sector_id=?",
                    (max(0, round(row["confidence"] * CONFIDENCE_DECAY)),
                     row["player_id"], row["sector_id"]))

    # 6. Holdings snapshot — calculated AFTER all processing is complete
    #    This is the canonical end-state: production, arrivals, missions, fog all resolved.
    conn.commit()  # flush all mutations before snapshotting
    _snapshot_holdings(cur, current_turn)

    # 7. Increment turn
    cur.execute("UPDATE game_state SET current_turn=current_turn+1 WHERE id=1")
    conn.commit(); conn.close()
    print(f"End of turn {current_turn} complete.")

    # 8. Check for game over
    if is_game_over():
        print(f"Turn limit {TURN_LIMIT} reached — game over. Calculating final scores...")
        _calculate_final_scores()

def _snapshot_holdings(cur, turn: int):
    """
    Record per-player resource totals at the end of the turn,
    after all pod execution, arrivals, and mission dispatch have resolved.
    Called as step 6 of end_of_turn() — never before processing is complete.
    """
    cur.execute("""
        SELECT o.player_id,
               SUM(CASE WHEN p.mission='produce_energy' THEN p.storage_current ELSE 0 END) AS energy,
               SUM(CASE WHEN p.mission='produce_food'   THEN p.storage_current ELSE 0 END) AS food,
               SUM(CASE WHEN p.mission='produce_goods'  THEN p.storage_current ELSE 0 END) AS goods,
               SUM(p.storage_current) AS total
        FROM pods p
        JOIN organizations o ON o.id = p.org_id
        GROUP BY o.player_id
    """)
    for row in cur.fetchall():
        print(f"  Holdings (turn {turn}) — player {row['player_id']}: "
              f"energy={row['energy']:.1f} food={row['food']:.1f} "
              f"goods={row['goods']:.1f} total={row['total']:.1f}")
    # TODO: emit turn.snapshot event here via record_event()

def _calculate_final_scores():
    """Sum storage_current across all pods per player. Highest score wins."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT o.player_id, p.display_name, SUM(pods.storage_current) AS score
        FROM pods
        JOIN organizations o ON o.id = pods.org_id
        JOIN players p ON p.id = o.player_id
        GROUP BY o.player_id
        ORDER BY score DESC
    """)
    for row in cur.fetchall():
        print(f"  {row['display_name']}: {row['score']:.1f} pts")
    conn.close()

def _handle_colonize(cur, org, current_turn):
    """
    Resolve a matured colonize_complete event: flip org_type to 'colony' and log
    ship.colonized. Written directly (not via db.events.record_event) to avoid a
    circular import — db/events.py imports get_current_turn from this module.
    """
    if org["org_type"] != "ship":
        return
    cur.execute("SELECT COALESCE(MAX(seq),-1)+1 FROM events WHERE turn=?", (current_turn,))
    seq = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO events (game_id,turn,seq,event_type,actor_id,subject_id,subject_type,payload)
        VALUES (NULL,?,?,'ship.colonized',NULL,?,'organization',?)
    """, (current_turn, seq, org["id"],
          json.dumps({"org_id": org["id"], "sector_id": org["sector_id"]})))
    cur.execute("""UPDATE organizations
        SET org_type='colony',is_mobile=0,mission='idle',mission_params=NULL WHERE id=?""",
        (org["id"],))

def _handle_defend(cur, org, params): pass   # stub
def _handle_attack(cur, org, params): pass   # stub

def check_consensus_acceleration():
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM players WHERE end_turn_declared=0")
    undeclared = cur.fetchone()[0]; conn.close()
    if undeclared == 0:
        print("All players declared end of turn — accelerating clock.")
        end_of_turn(); return True
    return False
