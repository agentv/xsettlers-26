from db.connection import get_connection
from db.sectors import (reveal_sector, roll_sector_energy, SECTOR_ENERGY_BASE,
                        SECTOR_ENERGY_DIE_SIDES, SECTOR_ENERGY_DIE_UNIT,
                        MIN_SECTOR_ENERGY, MAX_SECTOR_ENERGY)
from tests.conftest import seed_player, seed_sector

def test_roll_sector_energy_covers_exactly_the_six_faces():
    """400 + d6 x 100 -- six discrete outcomes, 500 through 1000, nothing
    between and nothing outside. The floor is the load-bearing part: every
    sector is worth working, so a bad roll costs upside rather than
    viability. Home sectors are exempt entirely -- see
    tests/test_bootstrap.py."""
    faces = {SECTOR_ENERGY_BASE + n * SECTOR_ENERGY_DIE_UNIT
             for n in range(1, SECTOR_ENERGY_DIE_SIDES + 1)}
    assert faces == {500, 600, 700, 800, 900, 1000}
    assert (MIN_SECTOR_ENERGY, MAX_SECTOR_ENERGY) == (500, 1000)
    rolled = {roll_sector_energy() for _ in range(400)}
    assert rolled == faces      # every face reachable, no value off the die

def test_reveal_sector_rolls_energy_in_band_and_stamps_visibility():
    pid = seed_player()
    conn = get_connection(); cur = conn.cursor()
    sid = reveal_sector(cur, pid, 5, 5, 0)
    conn.commit()
    sector = cur.execute("SELECT * FROM sectors WHERE id=?", (sid,)).fetchone()
    assert MIN_SECTOR_ENERGY <= sector["energy_capacity"] <= MAX_SECTOR_ENERGY
    # Energy is the only capacity a sector carries (2026-08-02).
    assert "food_capacity" not in sector.keys()
    assert "goods_capacity" not in sector.keys()
    ps = cur.execute("SELECT confidence FROM player_sectors WHERE player_id=? AND sector_id=?",
                     (pid, sid)).fetchone()
    assert ps["confidence"] == 100
    conn.close()

def test_reveal_sector_leaves_existing_capacities_untouched():
    pid = seed_player()
    existing_sid = seed_sector(1, 1, 0, energy=99.0)
    conn = get_connection(); cur = conn.cursor()
    sid = reveal_sector(cur, pid, 1, 1, 0)
    conn.commit()
    assert sid == existing_sid
    sector = cur.execute("SELECT energy_capacity FROM sectors WHERE id=?", (sid,)).fetchone()
    assert sector["energy_capacity"] == 99.0
    conn.close()

def test_rival_reveal_reads_the_established_value_not_a_fresh_one():
    """A sector's richness belongs to the sector, not to whoever found it.

    Whichever player reveals a sector first establishes its capacity; every
    later look by anyone reads that same established value. This is what
    makes it safe to vary capacity per sector at reveal time -- two rivals
    discovering the same coordinates can never be told different things
    about it, and the second arrival cannot re-roll the first one's find.
    """
    finder = seed_player(email="finder@t.com", player_token="U_FINDER")
    rival = seed_player(email="rival@t.com", player_token="U_RIVAL")
    conn = get_connection(); cur = conn.cursor()
    sid = reveal_sector(cur, finder, 7, 7, 0)
    # The finder works the sector down before the rival ever sees it.
    cur.execute("UPDATE sectors SET energy_capacity=123.0 WHERE id=?", (sid,))
    rival_sid = reveal_sector(cur, rival, 7, 7, 0)
    conn.commit()
    assert rival_sid == sid                      # same row, not a second one
    remaining = cur.execute("SELECT energy_capacity AS e FROM sectors WHERE id=?",
                            (sid,)).fetchone()["e"]
    assert remaining == 123.0                    # depleted state survives the reveal
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
