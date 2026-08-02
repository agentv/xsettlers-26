from db.connection import get_connection
from xsettlers_mcp.tools.sector_tools import (
    get_scan_range, show_sector_neighborhood,
    UNKNOWN_CELL, SEEN_CELL, EMPTY_CELL, MAX_NEIGHBORHOOD_RADIUS,
)
from db.sectors import CONFIDENCE_DECAY_PER_TURN, TURNS_TO_BLINK_OUT
from xsettlers_mcp.tools.organization_tools import set_pod_task
from xsettlers_mcp.tools.navigation_tools import confirm_move
from engine.turn import end_of_turn
from tests.conftest import (
    seed_player, seed_sector, seed_ship, seed_pod, seed_player_sector,
)

# --- scan range enforcement at resolution (see engine/turn.py) ---

def test_scan_within_range_reveals_target():
    """Confidence is checked here too (not just sector existence): scan
    resolution (step 3c of end_of_turn()) stamps confidence=100, but fog
    decay (step 5) runs later in the same pass and immediately decays it
    since the scanned sector isn't occupied by any of the player's orgs --
    pre-existing engine behavior, not something reveal_sector changes."""
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, task="scan")
    seed_pod(sid, task="produce_food", storage_current=100.0)
    seed_pod(sid, task="produce_energy", storage_current=100.0)   # scanning costs energy
    r = get_scan_range(sid)
    set_pod_task("U_P1", pod, "scan", offset_x=r, offset_y=0, offset_z=0)
    end_of_turn()
    conn = get_connection()
    sector = conn.execute(
        "SELECT id FROM sectors WHERE coord_x=? AND coord_y=0 AND coord_z=0", (r,)).fetchone()
    assert sector is not None
    ps = conn.execute("SELECT confidence FROM player_sectors WHERE player_id=? AND sector_id=?",
                      (pid, sector["id"])).fetchone()
    assert ps["confidence"] == 100 - CONFIDENCE_DECAY_PER_TURN
    conn.close()

def test_scan_out_of_range_aim_is_rejected_at_set_time():
    """Aim is an offset, so its range is fixed and knowable when set -- an
    illegal aim can never become legal, so it is refused outright rather than
    accepted and left to fail silently at resolution."""
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, task="scan")
    seed_pod(sid, task="produce_food", storage_current=100.0)
    seed_pod(sid, task="produce_energy", storage_current=100.0)   # scanning costs energy
    out_of_range = get_scan_range(sid) + 5
    result = set_pod_task("U_P1", pod, "scan", offset_x=out_of_range, offset_y=0, offset_z=0)
    assert "error" in result and result["in_range"] is False
    end_of_turn()
    conn = get_connection()
    sector = conn.execute(
        "SELECT id FROM sectors WHERE coord_x=? AND coord_y=0 AND coord_z=0",
        (out_of_range,)).fetchone()
    conn.close()
    assert sector is None

def test_rescanning_known_sector_refreshes_confidence_without_altering_resources():
    """Re-scanning an already-revealed sector must not re-randomize its
    resources (reveal_sector is idempotent -- it's a fresh look at whatever
    is currently there, not a reset), and should refresh the player's
    confidence even if it had decayed to near-zero in the meantime."""
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, task="scan")
    seed_pod(sid, task="produce_food", storage_current=100.0)
    seed_pod(sid, task="produce_energy", storage_current=100.0)   # scanning costs energy
    set_pod_task("U_P1", pod, "scan", bearing="E")
    end_of_turn()  # first scan: reveals the sector, stamps then decays confidence

    conn = get_connection()
    sector = conn.execute(
        "SELECT id, energy_capacity FROM sectors WHERE coord_x=1 AND coord_y=0 AND coord_z=0").fetchone()
    original_capacity = sector["energy_capacity"]
    # simulate it having decayed further, e.g. several unoccupied turns since
    conn.execute("UPDATE player_sectors SET confidence=5 WHERE player_id=? AND sector_id=?",
                 (pid, sector["id"]))
    conn.commit(); conn.close()

    set_pod_task("U_P1", pod, "scan", bearing="E")  # re-scan same target
    end_of_turn()

    conn = get_connection()
    resector = conn.execute("SELECT energy_capacity FROM sectors WHERE id=?", (sector["id"],)).fetchone()
    ps = conn.execute("SELECT confidence FROM player_sectors WHERE player_id=? AND sector_id=?",
                      (pid, sector["id"])).fetchone()
    conn.close()
    assert resector["energy_capacity"] == original_capacity  # not re-randomized
    # stamped back to 100 by the re-scan, then decays once more within this
    # same end_of_turn() pass since the org still doesn't occupy it
    assert ps["confidence"] == 100 - CONFIDENCE_DECAY_PER_TURN

# --- neighborhood viewport (see show_sector_neighborhood) ---

# Count of (dx,dy) with dx^2+dy^2 <= 25 over the 11x11 bounding square. The
# remaining 121-81 cells are the out-of-range corners that render blank.
IN_RANGE_CELLS_AT_R5 = 81

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
    a player who has seen exactly one sector still gets a full 11x11."""
    pid, sid, oid = _home()
    result = show_sector_neighborhood("U_P1", org_id=oid)
    grid = result["display"]["grid"]
    assert result["center"] == {"x": 25, "y": 25, "z": 0}
    assert result["radius"] == 5
    assert len(grid["rows"]) == 11
    assert grid["x_labels"] == [str(x) for x in range(20, 31)]
    assert [r["label"] for r in grid["rows"]] == [str(y) for y in range(20, 31)]
    assert _cell_at(result, 25, 25) == "S1"
    assert result["unknown_in_range"] == IN_RANGE_CELLS_AT_R5 - 1

def test_show_sector_neighborhood_blanks_out_of_range_and_dots_the_unseen():
    """The two states a player most needs to tell apart: 'outside my range'
    and 'in range, never looked'. Blank vs UNKNOWN_CELL, never both blank."""
    pid, sid, oid = _home()
    result = show_sector_neighborhood("U_P1", org_id=oid)
    assert _cell_at(result, 20, 20) == EMPTY_CELL      # corner: 50 > 25
    assert _cell_at(result, 30, 30) == EMPTY_CELL
    assert _cell_at(result, 25, 20) == UNKNOWN_CELL    # straight up, exactly r=5
    assert _cell_at(result, 24, 20) == EMPTY_CELL      # one step over: 26 > 25

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

def test_scan_in_transit_still_costs_food_but_reveals_nothing():
    """Now that out-of-range aims are refused at set time, transit is the one
    remaining case where a scan pays and returns nothing: the reveal is
    suppressed while the ship has no position, but the food is still drawn.
    Tracked as a known gap in docs/TODO.md -- pinned here so a fix to the
    cost side is a deliberate change rather than an accident."""
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, task="scan")
    seed_pod(sid, task="produce_food", storage_current=100.0)
    seed_pod(sid, task="produce_energy", storage_current=100.0)   # scanning costs energy
    set_pod_task("U_P1", pod, "scan", bearing="E")
    confirm_move("U_P1", sid, 4, 0, 0)          # in transit at end of turn
    end_of_turn()
    conn = get_connection()
    food = conn.execute("SELECT SUM(food_stored) s FROM pods WHERE org_id=?", (sid,)).fetchone()["s"]
    revealed = conn.execute("SELECT COUNT(*) n FROM sectors WHERE id != -1").fetchone()["n"]
    conn.close()
    assert food < 100.0        # paid for it
    assert revealed == 1       # only the origin sector -- nothing was scanned
