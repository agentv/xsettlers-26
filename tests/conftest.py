import pytest
from db.schema import init_schema
from db.connection import connection

@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    init_schema()
    seed_active_game()

def _insert(sql, params):
    """Run one INSERT and return the new row's id."""
    with connection() as conn:
        return conn.execute(sql, params).lastrowid

def seed_active_game(scenario_name="test-scenario"):
    """
    fresh_db seeds an active game by default, since most tests are about
    gameplay already in progress. Tests exercising "no scenario chosen yet"
    must DELETE FROM games to opt back out (see test_game_select.py).
    """
    _insert("""INSERT OR REPLACE INTO games (id,scenario_name,scenario_file,selected_by)
               VALUES (1,?,?,?)""", (scenario_name, f"config/{scenario_name}.yaml", None))

def seed_player(email="player@test.com", player_token="U_P1", display_name="Player One"):
    return _insert("INSERT INTO players (email,display_name,player_token) VALUES (?,?,?)",
                   (email, display_name, player_token))

def seed_sector(x=0, y=0, z=0, energy=50.0):
    # Energy is the only capacity a sector has -- food and goods are
    # manufactured from held stock, never harvested (see db/sectors.py).
    # Get-or-create, so the INSERT may be ignored and lastrowid mean nothing.
    with connection() as conn:
        conn.execute("""INSERT OR IGNORE INTO sectors
            (coord_x,coord_y,coord_z,energy_capacity) VALUES (?,?,?,?)""", (x, y, z, energy))
        return conn.execute("""SELECT id FROM sectors
            WHERE coord_x=? AND coord_y=? AND coord_z=?""", (x, y, z)).fetchone()["id"]

def seed_ship(player_id, sector_id, name="Test Ship"):
    return _insert("""INSERT INTO organizations (org_type,name,player_id,sector_id,is_mobile,mission)
                      VALUES ('ship',?,?,?,1,'idle')""", (name, player_id, sector_id))

def seed_pod(org_id, task="idle", storage_capacity=100.0, storage_current=0.0):
    """
    storage_current seeds whichever typed column matches `task`. Storage is
    generic per pod and independent of task; seeding via the pod's own task
    is just the natural convention for setup. Tasks with no matching resource
    (idle, scan) default to energy_stored.
    """
    column = {"produce_energy": "energy_stored", "produce_food": "food_stored",
              "produce_goods": "goods_stored"}.get(task, "energy_stored")
    return _insert(f"""INSERT INTO pods (task,org_id,storage_capacity,{column})
                       VALUES (?,?,?,?)""", (task, org_id, storage_capacity, storage_current))

def seed_player_sector(player_id, sector_id, confidence=100):
    _insert("""INSERT OR REPLACE INTO player_sectors (player_id,sector_id,confidence)
               VALUES (?,?,?)""", (player_id, sector_id, confidence))
