import pytest
from db.schema import init_schema
from db.connection import get_connection

@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    init_schema()
    seed_active_game()

def seed_active_game(scenario_name="test-scenario"):
    """
    Most tests are about gameplay already in progress, not the
    scenario-selection flow itself -- engine/turn.py's end_of_turn() no-ops
    if the games table is empty (see xsettlers_mcp/game_select.select_scenario), so
    fresh_db seeds a default active-game row automatically. Tests that
    specifically exercise "no scenario chosen yet" (see test_game_select.py)
    should DELETE FROM games to opt back out.
    """
    conn = get_connection()
    conn.execute("""INSERT OR REPLACE INTO games (id,scenario_name,scenario_file,selected_by)
        VALUES (1,?,?,?)""", (scenario_name, f"config/{scenario_name}.yaml", None))
    conn.commit(); conn.close()

def seed_player(email="player@test.com", player_token="U_P1", display_name="Player One"):
    conn = get_connection()
    conn.execute("INSERT INTO players (email,display_name,player_token) VALUES (?,?,?)",
                 (email,display_name,player_token))
    conn.commit()
    pid = conn.execute("SELECT id FROM players WHERE player_token=?",
                       (player_token,)).fetchone()["id"]
    conn.close(); return pid

def seed_sector(x=0, y=0, z=0, energy=50.0, food=50.0, goods=50.0):
    conn = get_connection()
    conn.execute("""INSERT OR IGNORE INTO sectors
        (coord_x,coord_y,coord_z,energy_capacity,food_capacity,goods_capacity)
        VALUES (?,?,?,?,?,?)""", (x,y,z,energy,food,goods))
    conn.commit()
    sid = conn.execute("SELECT id FROM sectors WHERE coord_x=? AND coord_y=? AND coord_z=?",
                       (x,y,z)).fetchone()["id"]
    conn.close(); return sid

def seed_ship(player_id, sector_id, name="Test Ship"):
    conn = get_connection()
    conn.execute("""INSERT INTO organizations
        (org_type,name,player_id,sector_id,is_mobile,mission) VALUES ('ship',?,?,?,1,'idle')""",
        (name,player_id,sector_id))
    conn.commit()
    oid = conn.execute("SELECT id FROM organizations WHERE name=? AND player_id=?",
                       (name,player_id)).fetchone()["id"]
    conn.close(); return oid

def seed_pod(org_id, task="idle", storage_capacity=100.0, storage_current=0.0):
    """
    storage_current seeds whichever typed column matches `task`
    (energy_stored for produce_energy, etc.) -- storage is generic per pod
    and independent of current task (see engine/turn.py), but seeding via
    the pod's own task is the natural convention for test setup. Tasks
    with no matching resource (idle, scan) default to energy_stored --
    arbitrary, since it doesn't functionally matter for a non-producing pod.
    """
    column = {"produce_energy": "energy_stored", "produce_food": "food_stored",
              "produce_goods": "goods_stored"}.get(task, "energy_stored")
    conn = get_connection()
    conn.execute(f"""INSERT INTO pods
        (task,org_id,storage_capacity,{column}) VALUES (?,?,?,?)""",
        (task,org_id,storage_capacity,storage_current))
    conn.commit()
    pid = conn.execute("SELECT id FROM pods WHERE org_id=? AND task=? ORDER BY id DESC LIMIT 1",
                       (org_id,task)).fetchone()["id"]
    conn.close(); return pid

def seed_player_sector(player_id, sector_id, confidence=100):
    conn = get_connection()
    conn.execute("""INSERT OR REPLACE INTO player_sectors (player_id,sector_id,confidence)
        VALUES (?,?,?)""", (player_id,sector_id,confidence))
    conn.commit(); conn.close()
