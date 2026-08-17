from db.connection import get_connection
from db.sectors import (reveal_sector, richness_multiplier, roll_sector_energy,
                        SECTOR_ENERGY_BASE,
                        SECTOR_ENERGY_DIE_SIDES, SECTOR_ENERGY_DIE_UNIT,
                        MIN_SECTOR_ENERGY, MAX_SECTOR_ENERGY)
from tests.conftest import seed_player, seed_sector


def place_hotspot(cur, center, radius, multiplier):
    cur.execute("""INSERT INTO map_hotspots
        (center_x,center_y,center_z,radius,multiplier) VALUES (?,?,?,?,?)""",
        (*center, radius, multiplier))

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
    # Energy is the only capacity a sector carries.
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

def test_richness_multiplier_is_1_in_open_space():
    """An empty map_hotspots table is the ordinary case -- a scenario with no
    `map:` block rolls open space everywhere."""
    conn = get_connection(); cur = conn.cursor()
    assert richness_multiplier(cur, 4, 4, 0) == 1.0
    conn.close()


def test_hotspot_radius_is_euclidean_and_inclusive():
    """radius 2 covers the sector 2 out and the diagonal at 1,1 (distance
    1.41), but not the one at 2,2 (2.83). Radius 0 covers the centre alone."""
    conn = get_connection(); cur = conn.cursor()
    place_hotspot(cur, (10, 10, 0), radius=2, multiplier=3.0)
    assert richness_multiplier(cur, 10, 10, 0) == 3.0      # centre
    assert richness_multiplier(cur, 12, 10, 0) == 3.0      # exactly 2 -- inclusive
    assert richness_multiplier(cur, 11, 11, 0) == 3.0      # 1.41
    assert richness_multiplier(cur, 12, 12, 0) == 1.0      # 2.83 -- outside
    assert richness_multiplier(cur, 13, 10, 0) == 1.0
    conn.close()


def test_overlapping_hotspots_take_the_max_not_the_product():
    """Two x2 regions that happen to touch must not produce a x4 sector
    nobody placed. A scenario's stated multipliers are the ceiling of what
    its map can roll."""
    conn = get_connection(); cur = conn.cursor()
    place_hotspot(cur, (20, 20, 0), radius=3, multiplier=2.0)
    place_hotspot(cur, (22, 20, 0), radius=3, multiplier=2.5)
    assert richness_multiplier(cur, 21, 20, 0) == 2.5      # inside both
    assert richness_multiplier(cur, 18, 20, 0) == 2.0      # inside the first only
    conn.close()


def test_hotspot_multiplier_scales_the_whole_roll_not_just_the_die():
    """x3 rolls 1500..3000 -- floor above open space's ceiling. Scaling only
    the die would give 700..2200 and make a hotspot a gamble instead of a
    better place, which is a different game."""
    rolled = {roll_sector_energy(3.0) for _ in range(400)}
    assert rolled == {1500, 1800, 2100, 2400, 2700, 3000}
    assert min(rolled) > MAX_SECTOR_ENERGY


def test_a_multiplier_below_1_marks_a_lean_region():
    """Nothing constrains a multiplier upward -- the same mechanism read the
    other way is how a scenario draws a desert."""
    rolled = {roll_sector_energy(0.5) for _ in range(400)}
    assert rolled == {250, 300, 350, 400, 450, 500}
    assert min(rolled) < MIN_SECTOR_ENERGY


def test_reveal_inside_a_hotspot_rolls_the_scaled_band():
    pid = seed_player()
    conn = get_connection(); cur = conn.cursor()
    place_hotspot(cur, (30, 30, 0), radius=1, multiplier=2.0)
    inside = reveal_sector(cur, pid, 30, 30, 0)
    outside = reveal_sector(cur, pid, 40, 40, 0)
    conn.commit()
    rich = cur.execute("SELECT energy_capacity AS e FROM sectors WHERE id=?",
                       (inside,)).fetchone()["e"]
    plain = cur.execute("SELECT energy_capacity AS e FROM sectors WHERE id=?",
                        (outside,)).fetchone()["e"]
    assert 2 * MIN_SECTOR_ENERGY <= rich <= 2 * MAX_SECTOR_ENERGY
    assert MIN_SECTOR_ENERGY <= plain <= MAX_SECTOR_ENERGY
    conn.close()


def test_a_hotspot_placed_after_discovery_does_not_re_roll_the_sector():
    """Richness is fixed at first reveal. A map is a fact about the game from
    bootstrap on -- which is why db/bootstrap.py seeds map_hotspots before the
    first reveal_sector call, and why it refuses to lay a second map over a
    database that already has one."""
    pid = seed_player()
    conn = get_connection(); cur = conn.cursor()
    sid = reveal_sector(cur, pid, 8, 8, 0)
    before = cur.execute("SELECT energy_capacity AS e FROM sectors WHERE id=?",
                         (sid,)).fetchone()["e"]
    place_hotspot(cur, (8, 8, 0), radius=5, multiplier=3.0)
    assert reveal_sector(cur, pid, 8, 8, 0) == sid
    conn.commit()
    after = cur.execute("SELECT energy_capacity AS e FROM sectors WHERE id=?",
                        (sid,)).fetchone()["e"]
    assert after == before
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
