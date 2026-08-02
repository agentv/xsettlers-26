from db.connection import get_connection
from db.bootstrap import bootstrap_game
from db.sectors import MIN_SECTOR_ENERGY, MAX_SECTOR_ENERGY
from config.loader import HOME_SECTOR_ENERGY

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
            "SELECT task FROM pods WHERE org_id=?", (colony["id"],)
        ).fetchall()
        assert len(pods) == 6
        tasks = [p["task"] for p in pods]
        assert tasks.count("produce_energy") == 2
        assert tasks.count("produce_goods") == 2
        assert tasks.count("produce_food") == 2
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

def test_bootstrap_seeds_pods_at_the_scenario_starting_fill(tmp_path, monkeypatch):
    """The scenario decides how rich a game begins, not db/bootstrap.py."""
    lean = tmp_path / "game_lean.yaml"
    lean.write_text(
        'name: "Lean"\ndescription: "d"\nstarting_fill: 0.25\n'
        'participants:\n  - {player: "vincent@example.com", home_sector: [0, 0, 0]}\n'
        'ships_per_player: 1\n'
        'pods_per_ship:\n'
        '  - {task: produce_energy, count: 1, storage_capacity: 100.0}\n'
        '  - {task: produce_food, count: 1, storage_capacity: 100.0, starting_fill: 1.0}\n')
    bootstrap_game(scenario_file=str(lean), scenario_name="lean", selected_by="test")
    conn = get_connection()
    pods = conn.execute(
        "SELECT task, storage_capacity, energy_stored, food_stored FROM pods ORDER BY task"
    ).fetchall()
    conn.close()
    by_task = {p["task"]: p for p in pods}
    assert by_task["produce_energy"]["energy_stored"] == 25.0   # scenario fill
    assert by_task["produce_food"]["food_stored"] == 100.0      # template override
    assert all(p["storage_capacity"] == 100.0 for p in pods)       # capacity untouched

def test_bootstrap_requires_a_scenario():
    """There is no default scenario — the service is a library of games."""
    import pytest
    with pytest.raises(ValueError, match="no default scenario"):
        bootstrap_game(scenario_name="nothing", selected_by="test")

def test_bootstrap_seeds_only_home_sectors_not_full_grid():
    """Sectors are lazily instantiated (see db/sectors.py's reveal_sector) --
    bootstrap should only reveal the two players' home sectors, not a
    pre-seeded grid. Home sectors are exempt from the discovery roll and
    seeded flat and bottomless instead (HOME_SECTOR_ENERGY) -- a player's own
    footing should never be what runs out from under them."""
    _bootstrap()
    conn = get_connection()
    sectors = conn.execute("""SELECT coord_x,coord_y,coord_z,energy_capacity
        FROM sectors WHERE id != -1""").fetchall()
    conn.close()
    assert len(sectors) == 2
    for s in sectors:
        assert s["energy_capacity"] == HOME_SECTOR_ENERGY
        # Emphatically not a lucky roll: home is far above the richest
        # possible discovery, so this cannot pass by coincidence.
        assert s["energy_capacity"] > MAX_SECTOR_ENERGY


def test_home_sector_is_rich_but_the_transit_sentinel_stays_at_zero():
    """Two different things that are easy to conflate by name.

    The HOME sector is the scenario's starting coordinates, where the first
    colony sits; it is seeded bottomless so a player's footing never fails.
    The SENTINEL sector (id = -1) is the parking slot for ships in transit,
    and its 0 energy capacity is the entire mechanism suppressing energy
    harvesting mid-flight -- there is no other branch doing it. Enriching the
    sentinel by mistake would silently delete transit stress from the game.
    """
    _bootstrap()
    conn = get_connection()
    sentinel = conn.execute(
        "SELECT energy_capacity AS e FROM sectors WHERE id=-1").fetchone()
    homes = conn.execute(
        "SELECT energy_capacity AS e FROM sectors WHERE id!=-1").fetchall()
    conn.close()
    assert sentinel["e"] == 0.0
    assert all(h["e"] == HOME_SECTOR_ENERGY for h in homes)
