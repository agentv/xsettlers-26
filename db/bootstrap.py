# Closed roster rule: The player set for a game is fixed at bootstrap time.
# game_config.yaml is the sole source of player identity by default, and
# there is no runtime player-join path -- to change the roster, bootstrap a
# new database. roster_override (below) is an explicit escape hatch for a
# future caller (e.g. a lobby) that assembles a roster dynamically instead
# of reading the YAML list; nothing calls it yet.
#
# Which scenario gets bootstrapped is chosen at runtime via
# xsettlers_mcp/game_select.py's select_scenario() -- see scenario_file/scenario_name/
# selected_by below. The games table records that choice.

from db.connection import get_connection
from config.loader import load_config

def bootstrap_game(config_path: str = None, scenario_file: str = None,
                   scenario_name: str = None, selected_by: str = None,
                   roster_override: list = None):
    """
    Initialize a fresh game. Safe to call repeatedly — guards against double-init.

    roster_override, if given, is a list of dicts (email, display_name,
    player_token, optional is_npc) used instead of reading players from
    config_path's players: list. Escape hatch for a future lobby that
    assembles a roster dynamically (real players + NPC fill-in) rather
    than reading a fixed YAML list. Not currently called by anything --
    xsettlers_mcp/game_select.py's select_scenario() still always uses the config
    file's roster.
    """
    cfg  = load_config(config_path, scenario_override=scenario_file) if config_path \
           else load_config(scenario_override=scenario_file)
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sectors WHERE coord_x != -1")
    if cur.fetchone()[0] > 0:
        print("Game already bootstrapped — skipping."); conn.close(); return
    print(f"Bootstrapping game: {cfg.game.name} (scenario: {cfg.starting_configuration.name})")

    # 1. Seed sectors
    sector_id_map = {}
    for s in cfg.sectors:
        x, y, z = s.coords
        cur.execute("""INSERT OR IGNORE INTO sectors
            (coord_x,coord_y,coord_z,energy_capacity,food_capacity,goods_capacity)
            VALUES (?,?,?,?,?,?)""", (x,y,z,s.energy_capacity,s.food_capacity,s.goods_capacity))
        cur.execute("""UPDATE sectors SET location=MakePointZ(?,?,?,-1)
            WHERE coord_x=? AND coord_y=? AND coord_z=?""", (x,y,z,x,y,z))
        sector_id_map[(x,y,z)] = cur.execute(
            "SELECT id FROM sectors WHERE coord_x=? AND coord_y=? AND coord_z=?",
            (x,y,z)).fetchone()["id"]
    print(f"  Created {len(cfg.sectors)} sectors.")

    # 2. Seed players
    player_id_list = []
    if roster_override is not None:
        if len(roster_override) > cfg.game.max_players:
            raise ValueError(
                f"roster_override has {len(roster_override)} players but "
                f"max_players={cfg.game.max_players}")
        for p in roster_override:
            cur.execute("""INSERT INTO players
                (email,display_name,player_token,is_npc) VALUES (?,?,?,?)""",
                (p["email"], p["display_name"], p["player_token"],
                 int(bool(p.get("is_npc", False)))))
            player_id_list.append(cur.lastrowid)
            print(f"  Created player: {p['display_name']}")
    else:
        for p in cfg.players:
            cur.execute("INSERT INTO players (email,display_name,player_token) VALUES (?,?,?)",
                        (p.email, p.display_name, p.player_token))
            player_id_list.append(cur.lastrowid)
            print(f"  Created player: {p.display_name}")

    # 3. Create starting ships for each player
    sc = cfg.starting_configuration
    for idx, player_id in enumerate(player_id_list):
        home_coords = tuple(sc.home_sector_by_player[idx])
        home_sector_id = sector_id_map.get(home_coords)
        if not home_sector_id:
            raise ValueError(f"Home sector {home_coords} for player {idx+1} not found in sectors")
        for ship_num in range(sc.ships_per_player):
            ship_name = f"Ship-P{idx+1}-{ship_num+1:02d}"
            cur.execute("""INSERT INTO organizations
                (org_type,name,player_id,sector_id,is_mobile,mission)
                VALUES ('ship',?,?,?,1,'idle')""",
                (ship_name, player_id, home_sector_id))
            org_id = cur.lastrowid
            # Expand pod templates: each template has a count
            for pod_tmpl in sc.pods_per_ship:
                for _ in range(pod_tmpl.count):
                    cur.execute("""INSERT INTO pods
                        (mission,org_id,storage_capacity,storage_current,
                         energy_consumption,food_consumption)
                        VALUES (?,?,?,0.0,?,?)""",
                        (pod_tmpl.mission, org_id, pod_tmpl.storage_capacity,
                         pod_tmpl.energy_consumption, pod_tmpl.food_consumption))
        # Stamp home sector as visible at confidence=100
        cur.execute("""INSERT OR REPLACE INTO player_sectors (player_id,sector_id,confidence)
            VALUES (?,?,100)""", (player_id, home_sector_id))
        print(f"  Created {sc.ships_per_player} ships for player {player_id}.")

    # 4. Optionally create a home colony -- same pod loadout as a ship (see
    #    docs/player_guide.md's Outbreak section: "every organization -- each
    #    ship and the home colony alike -- carries the same 18-pod loadout").
    if sc.home_colony:
        for idx, player_id in enumerate(player_id_list):
            home_coords = tuple(sc.home_sector_by_player[idx])
            home_sector_id = sector_id_map[home_coords]
            cur.execute("""INSERT INTO organizations
                (org_type,name,player_id,sector_id,is_mobile,mission)
                VALUES ('colony',?,?,?,0,'idle')""",
                (f"Colony-P{idx+1}", player_id, home_sector_id))
            colony_org_id = cur.lastrowid
            for pod_tmpl in sc.pods_per_ship:
                for _ in range(pod_tmpl.count):
                    cur.execute("""INSERT INTO pods
                        (mission,org_id,storage_capacity,storage_current,
                         energy_consumption,food_consumption)
                        VALUES (?,?,?,0.0,?,?)""",
                        (pod_tmpl.mission, colony_org_id, pod_tmpl.storage_capacity,
                         pod_tmpl.energy_consumption, pod_tmpl.food_consumption))

    cur.execute("INSERT OR IGNORE INTO game_state (id,current_turn) VALUES (1,0)")
    cur.execute("""INSERT OR IGNORE INTO games (id,scenario_name,scenario_file,selected_by)
        VALUES (1,?,?,?)""",
        (scenario_name or cfg.starting_configuration.name,
         scenario_file or "(default from game_config.yaml)",
         selected_by))
    conn.commit(); conn.close()
    print("Bootstrap complete.")
