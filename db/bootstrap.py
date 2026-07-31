# Closed roster rule: The player set for a game is fixed at bootstrap time.
# The roster comes from the chosen scenario's participants list, resolved
# against config/game_config.yaml's player directory (see
# config/loader.py's resolve_seats) -- so player count is a property of the
# scenario, not of the service. There is no runtime player-join path; to
# change who is playing, bootstrap a new database. roster_override (below)
# is an explicit escape hatch for a future caller (e.g. a lobby) that
# assembles seats dynamically instead of reading a scenario file; nothing
# calls it yet.
#
# Which scenario gets bootstrapped is chosen at runtime via
# xsettlers_mcp/game_select.py's select_scenario() -- see scenario_file/scenario_name/
# selected_by below. The games table records that choice.

from db.connection import get_connection
from config.loader import load_config, Seat
from db.sectors import reveal_sector
from engine.production import RESOURCE_STORAGE_COLUMN, RESOURCE_PRODUCING_TASK

def _starting_cargo_for_task(task: str, capacity: float, fill: float) -> dict:
    """
    Seed a freshly bootstrapped pod with `fill` (0.0-1.0) of its capacity,
    in whatever resource matches its task at creation time
    (energy_stored/food_stored/goods_stored are independent of current
    task from then on -- see engine/turn.py). Non-producing tasks
    (idle, scan) start with nothing stored regardless of fill, having no
    matching resource.

    `fill` comes from the scenario, not from here -- how rich a game starts
    is a scenario characteristic (see config/loader.py's starting_fill).
    It used to be hardcoded at 100%, which had two costs: every scenario was
    equally rich whatever its intent, and a fleet at capacity cannot
    accumulate, so production was pure waste and only consumption could move
    the score.

    Note the floor this trades against: production costs other resources to
    run (engine/production.py's POD_CONSUMPTION_RECIPE -- produce_goods costs
    energy, and so on), so a scenario that starts at or near 0.0 deadlocks its
    own economy at bootstrap with nothing to spend. Scarcity is a dial, not a
    switch.
    """
    stored = {"energy_stored": 0.0, "food_stored": 0.0, "goods_stored": 0.0}
    for resource, producing_task in RESOURCE_PRODUCING_TASK.items():
        if producing_task == task:
            stored[RESOURCE_STORAGE_COLUMN[resource]] = capacity * fill
    return stored

def bootstrap_game(config_path: str = None, scenario_file: str = None,
                   scenario_name: str = None, selected_by: str = None,
                   roster_override: list = None):
    """
    Initialize a fresh game. Safe to call repeatedly — guards against double-init.

    scenario_file (repo-root-relative, e.g. "config/game0.yaml") names which
    game in the library to bootstrap; it is required, since there is no
    default scenario. The scenario's participants, resolved against the
    player directory, decide both who plays and where each of them starts —
    so a solo scenario and a five-player scenario bootstrap identically with
    no branching here.

    roster_override, if given, is a list of dicts (email, display_name,
    player_token, home_sector, optional is_npc) used instead of the
    scenario's own participants. Escape hatch for a future lobby that
    assembles seats dynamically (real players + NPC fill-in) rather than
    reading a fixed YAML list. Not currently called by anything --
    xsettlers_mcp/game_select.py's select_scenario() always uses the
    scenario's participants.
    """
    cfg  = load_config(config_path, scenario_override=scenario_file) if config_path \
           else load_config(scenario_override=scenario_file)
    if cfg.starting_configuration is None:
        raise ValueError("bootstrap_game() requires a scenario_file — there is no default scenario")
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sectors WHERE coord_x != -1")
    if cur.fetchone()[0] > 0:
        print("Game already bootstrapped — skipping."); conn.close(); return
    print(f"Bootstrapping game: {cfg.game.name} (scenario: {cfg.starting_configuration.name})")

    # 1. Seed players from the resolved seats -- identity and starting
    #    position travel together, so nothing downstream pairs lists by index.
    seats = roster_override if roster_override is not None else cfg.seats
    if roster_override is not None:
        if len(roster_override) > cfg.game.max_players:
            raise ValueError(
                f"roster_override has {len(roster_override)} players but "
                f"max_players={cfg.game.max_players}")
        seats = [Seat(email=p["email"], display_name=p["display_name"],
                      player_token=p["player_token"],
                      home_sector=list(p["home_sector"]),
                      is_npc=bool(p.get("is_npc", False))) for p in roster_override]
    player_id_list = []
    for seat in seats:
        cur.execute("""INSERT INTO players
            (email,display_name,player_token,is_npc) VALUES (?,?,?,?)""",
            (seat.email, seat.display_name, seat.player_token, int(seat.is_npc)))
        player_id_list.append(cur.lastrowid)
        print(f"  Created player: {seat.display_name}")

    # 2. Create starting ships for each player
    sc = cfg.starting_configuration
    for idx, (player_id, seat) in enumerate(zip(player_id_list, seats)):
        home_sector_id = reveal_sector(cur, player_id, *tuple(seat.home_sector))
        for ship_num in range(sc.ships_per_player):
            # Short by default: a player says "S3", not "Ship-P1-03".
            # Unique per player, which is what makes a name referenceable.
            ship_name = f"S{ship_num+1}"
            cur.execute("""INSERT INTO organizations
                (org_type,name,player_id,sector_id,is_mobile,mission)
                VALUES ('ship',?,?,?,1,'idle')""",
                (ship_name, player_id, home_sector_id))
            org_id = cur.lastrowid
            # Expand pod templates: each template has a count
            for pod_tmpl in sc.pods_per_ship:
                for _ in range(pod_tmpl.count):
                    cargo = _starting_cargo_for_task(pod_tmpl.task,
                                                        pod_tmpl.storage_capacity,
                                                        pod_tmpl.starting_fill)
                    cur.execute("""INSERT INTO pods
                        (task,org_id,storage_capacity,energy_stored,food_stored,goods_stored)
                        VALUES (?,?,?,?,?,?)""",
                        (pod_tmpl.task, org_id, pod_tmpl.storage_capacity,
                         cargo["energy_stored"], cargo["food_stored"], cargo["goods_stored"]))
        print(f"  Created {sc.ships_per_player} ships for player {player_id}.")

    # 3. Optionally create a home colony -- same pod loadout as a ship (see
    #    docs/player_guide.md's Outbreak section: "every organization -- each
    #    ship and the home colony alike -- carries the same 6-pod loadout").
    if sc.home_colony:
        for idx, (player_id, seat) in enumerate(zip(player_id_list, seats)):
            home_sector_id = reveal_sector(cur, player_id, *tuple(seat.home_sector))
            cur.execute("""INSERT INTO organizations
                (org_type,name,player_id,sector_id,is_mobile,mission)
                VALUES ('colony',?,?,?,0,'idle')""",
                ("C1", player_id, home_sector_id))
            colony_org_id = cur.lastrowid
            for pod_tmpl in sc.pods_per_ship:
                for _ in range(pod_tmpl.count):
                    cargo = _starting_cargo_for_task(pod_tmpl.task,
                                                        pod_tmpl.storage_capacity,
                                                        pod_tmpl.starting_fill)
                    cur.execute("""INSERT INTO pods
                        (task,org_id,storage_capacity,energy_stored,food_stored,goods_stored)
                        VALUES (?,?,?,?,?,?)""",
                        (pod_tmpl.task, colony_org_id, pod_tmpl.storage_capacity,
                         cargo["energy_stored"], cargo["food_stored"], cargo["goods_stored"]))

    cur.execute("INSERT OR IGNORE INTO game_state (id,current_turn) VALUES (1,0)")
    cur.execute("""INSERT OR IGNORE INTO games (id,scenario_name,scenario_file,selected_by)
        VALUES (1,?,?,?)""",
        (scenario_name or cfg.starting_configuration.name,
         scenario_file or "(default from game_config.yaml)",
         selected_by))
    conn.commit(); conn.close()
    print("Bootstrap complete.")
