from db.connection import get_connection
from xsettlers_mcp.tools.sector_tools import (
    show_sector_neighborhood,
    UNKNOWN_CELL, SEEN_CELL, EMPTY_CELL, MAX_NEIGHBORHOOD_RADIUS,
)
from tests.conftest import (
    seed_player, seed_sector, seed_ship, seed_player_sector,
)


# --- neighborhood viewport (see show_sector_neighborhood) ---

# Count of (dx,dy) with dx^2+dy^2 <= 16 over the 9x9 bounding square. The
# remaining 81-49 cells are the out-of-range corners that render blank.
IN_RANGE_CELLS_AT_R4 = 49

def _seed_colony(player_id, sector_id, name="Test Colony"):
    conn = get_connection()
    conn.execute("""INSERT INTO organizations
        (org_type,name,player_id,sector_id,is_mobile,mission)
        VALUES ('colony',?,?,?,0,'idle')""", (name, player_id, sector_id))
    conn.commit()
    oid = conn.execute("SELECT id FROM organizations WHERE name=? AND player_id=?",
                       (name, player_id)).fetchone()["id"]
    conn.close(); return oid

def _home(x=25, y=25, confidence=100):
    """Player with one ship at (x,y,0) and visibility of that sector."""
    pid = seed_player(); sid = seed_sector(x, y, 0)
    oid = seed_ship(pid, sid)
    seed_player_sector(pid, sid, confidence)
    return pid, sid, oid

def _cell_at(result, x, y):
    grid = result["display"]["grid"]
    row = next(r for r in grid["rows"] if r["label"] == str(y))
    return row["cells"][grid["x_labels"].index(str(x))]

def test_show_sector_neighborhood_returns_full_lattice_around_an_org():
    """The grid is synthesized from center+radius, not from known sectors --
    a player who has seen exactly one sector still gets a full 9x9."""
    pid, sid, oid = _home()
    result = show_sector_neighborhood("U_P1", org_id=oid)
    grid = result["display"]["grid"]
    assert result["center"] == {"x": 25, "y": 25, "z": 0}
    assert result["radius"] == 4
    assert len(grid["rows"]) == 9
    assert grid["x_labels"] == [str(x) for x in range(21, 30)]
    assert [r["label"] for r in grid["rows"]] == [str(y) for y in range(21, 30)]
    assert _cell_at(result, 25, 25) == "S1"
    assert result["unknown_in_range"] == IN_RANGE_CELLS_AT_R4 - 1

def test_show_sector_neighborhood_blanks_out_of_range_and_dots_the_unseen():
    """The two states a player most needs to tell apart: 'outside my range'
    and 'in range, never looked'. Blank vs UNKNOWN_CELL, never both blank."""
    pid, sid, oid = _home()
    result = show_sector_neighborhood("U_P1", org_id=oid)
    assert _cell_at(result, 21, 21) == EMPTY_CELL      # corner: 32 > 16
    assert _cell_at(result, 29, 29) == EMPTY_CELL
    assert _cell_at(result, 25, 21) == UNKNOWN_CELL    # straight up, exactly r=4
    assert _cell_at(result, 24, 21) == EMPTY_CELL      # one step over: 17 > 16

def test_show_sector_neighborhood_marks_a_seen_empty_cell_without_its_confidence():
    """Confidence is a reporting number, not grid content -- a sector is
    either still on your map or it has blinked off it, so the cell is binary
    and the figure lives in the sector data."""
    pid, sid, oid = _home()
    seen = seed_sector(27, 25, 0)
    seed_player_sector(pid, seen, 40)
    result = show_sector_neighborhood("U_P1", org_id=oid)
    assert _cell_at(result, 27, 25) == SEEN_CELL
    assert next(s for s in result["sectors"] if s["id"] == seen)["confidence"] == 40

def test_show_sector_neighborhood_marks_ship_and_colony_sharing_a_sector():
    pid, sid, oid = _home()
    seed_ship(pid, sid, name="Second Ship")
    _seed_colony(pid, sid)
    result = show_sector_neighborhood("U_P1", org_id=oid)
    assert _cell_at(result, 25, 25) == "S2C"

def test_show_sector_neighborhood_rival_flag_truncates_a_full_own_marker():
    """A contested sector is the most important cell on the board, so the
    rival flag takes the third character from the own-org marker rather than
    being dropped. `highlights` still carries the exact composition."""
    pid, sid, oid = _home()
    seed_ship(pid, sid, name="Second Ship")
    _seed_colony(pid, sid)
    rival = seed_player(email="p2@test.com", player_token="U_P2", display_name="Player Two")
    seed_ship(rival, sid, name="Rival Ship")
    result = show_sector_neighborhood("U_P1", org_id=oid)
    assert _cell_at(result, 25, 25) == "S2!"
    home = result["highlights"][0]
    assert (home["own_ships"], home["own_colonies"], home["rival_orgs"]) == (2, 1, 1)

def test_show_sector_neighborhood_flags_rival_only_where_you_are_standing():
    """Rival positions are read live from organizations, so showing them on a
    stale cell would leak current intel about a sector last seen long ago.
    Occupied sector: flagged. Known-but-decayed sector: silent."""
    pid, sid, oid = _home()
    rival = seed_player(email="p2@test.com", player_token="U_P2", display_name="Player Two")
    seed_ship(rival, sid, name="Rival Ship")

    stale = seed_sector(27, 25, 0)
    seed_player_sector(pid, stale, 81)
    seed_ship(rival, stale, name="Hidden Rival")

    result = show_sector_neighborhood("U_P1", org_id=oid)
    assert _cell_at(result, 25, 25) == "S1!"
    assert _cell_at(result, 27, 25) == SEEN_CELL
    hidden = next(s for s in result["sectors"] if s["id"] == stale)
    assert hidden["rival_orgs"] == 0
    assert [h["coords_display"] for h in result["highlights"]] == ["(25,25,0)"]

def test_show_sector_neighborhood_counts_off_plane_sectors_without_drawing_them():
    """The grid is one z-plane. Known sectors in range but off-plane stay in
    `sectors` and get counted, rather than being silently flattened in."""
    pid, sid, oid = _home()
    upstairs = seed_sector(25, 25, 1)
    seed_player_sector(pid, upstairs, 100)
    result = show_sector_neighborhood("U_P1", org_id=oid)
    assert result["off_plane_count"] == 1
    assert _cell_at(result, 25, 25) == "S1"            # still the on-plane cell
    assert any(s["id"] == upstairs and not s["in_plane"] for s in result["sectors"])

def test_show_sector_neighborhood_accepts_explicit_coordinates():
    pid, sid, oid = _home()
    result = show_sector_neighborhood("U_P1", center_x=25, center_y=25, center_z=0, radius=1)
    assert result["center"] == {"x": 25, "y": 25, "z": 0}
    assert result["org_id"] is None
    assert len(result["display"]["grid"]["rows"]) == 3

def test_show_sector_neighborhood_is_a_pure_view():
    """Looking at a map must not be what creates it -- no sector rows, no
    confidence changes. reveal_sector() stays the only writer."""
    pid, sid, oid = _home()
    conn = get_connection()
    before = (conn.execute("SELECT COUNT(*) n FROM sectors").fetchone()["n"],
              conn.execute("SELECT COUNT(*) n FROM player_sectors").fetchone()["n"],
              conn.execute("SELECT SUM(confidence) s FROM player_sectors").fetchone()["s"])
    conn.close()
    show_sector_neighborhood("U_P1", org_id=oid)
    conn = get_connection()
    after = (conn.execute("SELECT COUNT(*) n FROM sectors").fetchone()["n"],
             conn.execute("SELECT COUNT(*) n FROM player_sectors").fetchone()["n"],
             conn.execute("SELECT SUM(confidence) s FROM player_sectors").fetchone()["s"])
    conn.close()
    assert before == after

def test_show_sector_neighborhood_rejects_in_transit_org():
    pid, sid, oid = _home()
    conn = get_connection()
    conn.execute("UPDATE organizations SET sector_id=-1 WHERE id=?", (oid,))
    conn.commit(); conn.close()
    assert "error" in show_sector_neighborhood("U_P1", org_id=oid)

def test_show_sector_neighborhood_rejects_missing_center_and_bad_radius():
    _home()
    assert "error" in show_sector_neighborhood("U_P1")
    assert "error" in show_sector_neighborhood("U_P1", center_x=1, center_y=1, center_z=0, radius=0)
    assert "error" in show_sector_neighborhood(
        "U_P1", center_x=1, center_y=1, center_z=0, radius=MAX_NEIGHBORHOOD_RADIUS + 1)

def test_show_sector_neighborhood_rejects_unknown_player():
    assert show_sector_neighborhood("U_NOBODY", center_x=0, center_y=0, center_z=0) == \
        {"error": "Player not found"}

