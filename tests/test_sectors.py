from db.connection import get_connection
from db.sectors import reveal_sector, DEFAULT_SECTOR_RESOURCE_UNITS
from tests.conftest import seed_player, seed_sector

def test_reveal_sector_creates_with_flat_defaults_and_stamps_visibility():
    pid = seed_player()
    conn = get_connection(); cur = conn.cursor()
    sid = reveal_sector(cur, pid, 5, 5, 0)
    conn.commit()
    sector = cur.execute("SELECT * FROM sectors WHERE id=?", (sid,)).fetchone()
    assert sector["energy_capacity"] == DEFAULT_SECTOR_RESOURCE_UNITS
    assert sector["food_capacity"] == DEFAULT_SECTOR_RESOURCE_UNITS
    assert sector["goods_capacity"] == DEFAULT_SECTOR_RESOURCE_UNITS
    ps = cur.execute("SELECT confidence FROM player_sectors WHERE player_id=? AND sector_id=?",
                     (pid, sid)).fetchone()
    assert ps["confidence"] == 100
    conn.close()

def test_reveal_sector_leaves_existing_capacities_untouched():
    pid = seed_player()
    existing_sid = seed_sector(1, 1, 0, energy=99.0, food=99.0, goods=99.0)
    conn = get_connection(); cur = conn.cursor()
    sid = reveal_sector(cur, pid, 1, 1, 0)
    conn.commit()
    assert sid == existing_sid
    sector = cur.execute("SELECT energy_capacity FROM sectors WHERE id=?", (sid,)).fetchone()
    assert sector["energy_capacity"] == 99.0
    conn.close()

def test_reveal_sector_idempotent_for_same_player():
    pid = seed_player()
    conn = get_connection(); cur = conn.cursor()
    sid1 = reveal_sector(cur, pid, 3, 3, 0)
    sid2 = reveal_sector(cur, pid, 3, 3, 0)
    conn.commit()
    assert sid1 == sid2
    count = cur.execute("SELECT COUNT(*) AS n FROM player_sectors WHERE player_id=? AND sector_id=?",
                        (pid, sid1)).fetchone()["n"]
    assert count == 1
    conn.close()
