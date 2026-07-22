from db.connection import get_connection
from db.bootstrap import bootstrap_game

def _bootstrap(scenario_file="config/game0.yaml", scenario_name="game0"):
    bootstrap_game(scenario_file=scenario_file, scenario_name=scenario_name,
                    selected_by="test")

def test_bootstrap_creates_players_and_ships_no_colony():
    _bootstrap()
    conn = get_connection()
    players = conn.execute("SELECT id FROM players").fetchall()
    assert len(players) == 2
    for (player_id,) in [(p["id"],) for p in players]:
        orgs = conn.execute(
            "SELECT org_type FROM organizations WHERE player_id=?", (player_id,)
        ).fetchall()
        assert len(orgs) == 8
        assert all(o["org_type"] == "ship" for o in orgs)
    conn.close()

def test_bootstrap_home_colony_gets_same_pod_loadout_as_ship():
    """
    Regression test: db/bootstrap.py's home_colony step used to create the
    colony organization but never attach any pods to it (see
    docs/player_guide.md's Diaspora section, "Implementation note"). Colonies
    must get the same 18-pod loadout as ships, not zero pods.
    """
    _bootstrap(scenario_file="config/game1.yaml", scenario_name="game1")
    conn = get_connection()
    colonies = conn.execute(
        "SELECT id, player_id FROM organizations WHERE org_type='colony'"
    ).fetchall()
    assert len(colonies) == 2
    for colony in colonies:
        pods = conn.execute(
            "SELECT mission FROM pods WHERE org_id=?", (colony["id"],)
        ).fetchall()
        assert len(pods) == 18
        missions = [p["mission"] for p in pods]
        assert missions.count("produce_energy") == 6
        assert missions.count("produce_goods") == 6
        assert missions.count("produce_food") == 6
    conn.close()

def test_bootstrap_diaspora_ships_alongside_colony():
    _bootstrap(scenario_file="config/game1.yaml", scenario_name="game1")
    conn = get_connection()
    for (player_id,) in conn.execute("SELECT id FROM players").fetchall():
        ships = conn.execute(
            "SELECT id FROM organizations WHERE player_id=? AND org_type='ship'",
            (player_id,)).fetchall()
        assert len(ships) == 8
    conn.close()

def test_bootstrap_home_sectors_stamped_visible():
    _bootstrap()
    conn = get_connection()
    for (player_id,) in conn.execute("SELECT id FROM players").fetchall():
        org = conn.execute(
            "SELECT sector_id FROM organizations WHERE player_id=? LIMIT 1",
            (player_id,)).fetchone()
        ps = conn.execute(
            "SELECT confidence FROM player_sectors WHERE player_id=? AND sector_id=?",
            (player_id, org["sector_id"])).fetchone()
        assert ps["confidence"] == 100
    conn.close()

def test_bootstrap_idempotent():
    _bootstrap()
    conn = get_connection()
    before = conn.execute("SELECT COUNT(*) AS n FROM organizations").fetchone()["n"]
    conn.close()
    _bootstrap()
    conn = get_connection()
    after = conn.execute("SELECT COUNT(*) AS n FROM organizations").fetchone()["n"]
    conn.close()
    assert before == after

def test_bootstrap_sentinel_sector_exists():
    conn = get_connection()
    sentinel = conn.execute("SELECT id FROM sectors WHERE id=-1").fetchone()
    assert sentinel is not None
    conn.close()

def test_bootstrap_game_state_starts_at_turn_zero():
    _bootstrap()
    conn = get_connection()
    row = conn.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()
    assert row["current_turn"] == 0
    conn.close()
