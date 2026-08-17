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
    Colonies must get the same pod loadout as ships, not zero pods -- see
    docs/player_guide.md's Outbreak section. A home_colony step that creates
    the organization without attaching pods is the failure this pins.
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


# --- the scenario's map ---

def _mapped_scenario(tmp_path, map_block: str):
    path = tmp_path / "game_mapped.yaml"
    path.write_text(
        'name: "Mapped"\ndescription: "d"\nhome_sector_energy: 2200\n'
        'participants:\n  - {player: "vincent@example.com", home_sector: [5, 5, 0]}\n'
        'ships_per_player: 1\n'
        'pods_per_ship:\n  - {task: produce_energy, count: 1, storage_capacity: 100.0}\n'
        + map_block)
    return str(path)


def test_bootstrap_places_the_scenarios_hotspots(tmp_path):
    bootstrap_game(scenario_file=_mapped_scenario(tmp_path,
        'map:\n  hotspots:\n    - {center: [30, 31, 0], radius: 2, multiplier: 3.0}\n'),
        scenario_name="mapped", selected_by="test")
    conn = get_connection()
    rows = conn.execute("SELECT * FROM map_hotspots").fetchall()
    conn.close()
    assert len(rows) == 1
    assert (rows[0]["center_x"], rows[0]["center_y"], rows[0]["center_z"]) == (30, 31, 0)
    assert (rows[0]["radius"], rows[0]["multiplier"]) == (2.0, 3.0)


def test_scatter_expands_deterministically_under_a_seed(monkeypatch):
    """One seed lays the same board down every time. Without that a scenario's
    map is not reproducible and a seeded tournament compares strategies across
    different maps -- which is the whole reason the seed exists."""
    import importlib, db.sectors, db.bootstrap
    from config.loader import ScatterDef
    scatter = ScatterDef(count=6, within_min=[0, 0, 0], within_max=[40, 40, 0],
                         radius=[1.0, 3.0], multiplier=[2.0, 3.0])

    def layout(seed):
        monkeypatch.setenv("SECTOR_ROLL_SEED", seed)
        importlib.reload(db.sectors); importlib.reload(db.bootstrap)
        return [(h.center, h.radius, h.multiplier)
                for h in db.bootstrap._scatter_hotspots(scatter)]

    first = layout("42")
    assert len(first) == 6
    assert layout("42") == first        # same seed, same board
    assert layout("99") != first        # different seed, different board
    for center, radius, multiplier in first:
        assert all(0 <= c <= 40 for c in center[:2]) and center[2] == 0
        assert 1.0 <= radius <= 3.0
        assert 2.0 <= multiplier <= 3.0
    monkeypatch.delenv("SECTOR_ROLL_SEED", raising=False)
    importlib.reload(db.sectors); importlib.reload(db.bootstrap)


def test_map_layout_does_not_consume_the_discovery_roll_sequence(monkeypatch):
    """Adding a hotspot to a scenario must not shift what every later sector
    rolls, or two maps cannot be compared on one seed."""
    import importlib, db.sectors, db.bootstrap
    from config.loader import ScatterDef
    monkeypatch.setenv("SECTOR_ROLL_SEED", "42")
    importlib.reload(db.sectors); importlib.reload(db.bootstrap)
    plain = [db.sectors.roll_sector_energy() for _ in range(10)]

    importlib.reload(db.sectors); importlib.reload(db.bootstrap)
    db.bootstrap._scatter_hotspots(ScatterDef(count=6, within_min=[0, 0, 0],
                                              within_max=[40, 40, 0],
                                              radius=[1.0, 3.0], multiplier=[2.0, 3.0]))
    assert [db.sectors.roll_sector_energy() for _ in range(10)] == plain
    monkeypatch.delenv("SECTOR_ROLL_SEED", raising=False)
    importlib.reload(db.sectors); importlib.reload(db.bootstrap)


def test_seed_map_does_not_lay_a_second_map_over_an_existing_one():
    """bootstrap_game is reachable against an existing database. A second pass
    must not add a map this game has already revealed sectors against."""
    from db.bootstrap import _seed_map
    from config.loader import HotspotDef, MapDef
    map_def = MapDef(hotspots=[HotspotDef(center=[1, 1, 0], radius=1, multiplier=2.0)])
    conn = get_connection(); cur = conn.cursor()
    _seed_map(cur, map_def)
    _seed_map(cur, MapDef(hotspots=[HotspotDef(center=[9, 9, 0], radius=5,
                                               multiplier=3.0)]))
    conn.commit()
    rows = cur.execute("SELECT center_x, multiplier FROM map_hotspots").fetchall()
    conn.close()
    assert [(r["center_x"], r["multiplier"]) for r in rows] == [(1, 2.0)]


def test_home_energy_is_the_scenarios_figure_not_a_hotspot_roll(tmp_path):
    """A starting position is a promise about a specific number, so
    home_sector_energy is written over whatever home rolled -- even when a
    hotspot covers it."""
    bootstrap_game(scenario_file=_mapped_scenario(tmp_path,
        'map:\n  hotspots:\n    - {center: [5, 5, 0], radius: 4, multiplier: 3.0}\n'),
        scenario_name="mapped", selected_by="test")
    conn = get_connection()
    energy = conn.execute(
        "SELECT energy_capacity AS e FROM sectors WHERE coord_x=5 AND coord_y=5"
    ).fetchone()["e"]
    conn.close()
    assert energy == 2200
