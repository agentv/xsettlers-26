from db.connection import get_connection
from db.bootstrap import bootstrap_game
from db.sectors import DEFAULT_SECTOR_RESOURCE_UNITS

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
    docs/player_guide.md's Outbreak section, "Implementation note"). Colonies
    must get the same 6-pod loadout as ships, not zero pods.
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
        assert len(pods) == 6
        missions = [p["mission"] for p in pods]
        assert missions.count("produce_energy") == 2
        assert missions.count("produce_goods") == 2
        assert missions.count("produce_food") == 2
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

def test_bootstrap_game_state_starts_at_turn_zero():
    _bootstrap()
    conn = get_connection()
    row = conn.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()
    assert row["current_turn"] == 0
    conn.close()

def test_bootstrap_seats_players_at_their_scenario_declared_home_sectors():
    """Home sectors come from the participant entry that names the player, not
    from a separate positional list — so the two can't drift out of step."""
    _bootstrap()
    conn = get_connection()
    rows = conn.execute("""
        SELECT p.display_name, s.coord_x, s.coord_y, s.coord_z
        FROM players p
        JOIN organizations o ON o.player_id = p.id
        JOIN sectors s ON s.id = o.sector_id
        GROUP BY p.id ORDER BY p.id""").fetchall()
    conn.close()
    assert [(r["display_name"], r["coord_x"], r["coord_y"], r["coord_z"]) for r in rows] == \
           [("Vincent", 25, 25, 0), ("Player Two", 25, 50, 0)]

def test_bootstrap_solo_scenario_seeds_exactly_one_player():
    _bootstrap(scenario_file="config/game_solo.yaml", scenario_name="game_solo")
    conn = get_connection()
    players = conn.execute("SELECT id FROM players").fetchall()
    sectors = conn.execute("SELECT COUNT(*) AS n FROM sectors WHERE id != -1").fetchone()["n"]
    ships = conn.execute(
        "SELECT COUNT(*) AS n FROM organizations WHERE org_type='ship'").fetchone()["n"]
    colonies = conn.execute(
        "SELECT COUNT(*) AS n FROM organizations WHERE org_type='colony'").fetchone()["n"]
    conn.close()
    assert len(players) == 1
    assert sectors == 1        # one participant, one home sector revealed
    assert ships == 8
    assert colonies == 1       # game_solo sets home_colony: true

def test_bootstrap_requires_a_scenario():
    """There is no default scenario — the service is a library of games."""
    import pytest
    with pytest.raises(ValueError, match="no default scenario"):
        bootstrap_game(scenario_name="nothing", selected_by="test")

def test_bootstrap_seeds_only_home_sectors_not_full_grid():
    """Sectors are lazily instantiated (see db/sectors.py's reveal_sector) --
    bootstrap should only reveal the two players' home sectors, not a
    pre-seeded grid, and each should get the flat default resource units."""
    _bootstrap()
    conn = get_connection()
    sectors = conn.execute("""SELECT coord_x,coord_y,coord_z,energy_capacity,
        food_capacity,goods_capacity FROM sectors WHERE id != -1""").fetchall()
    conn.close()
    assert len(sectors) == 2
    for s in sectors:
        assert s["energy_capacity"] == DEFAULT_SECTOR_RESOURCE_UNITS
        assert s["food_capacity"] == DEFAULT_SECTOR_RESOURCE_UNITS
        assert s["goods_capacity"] == DEFAULT_SECTOR_RESOURCE_UNITS
