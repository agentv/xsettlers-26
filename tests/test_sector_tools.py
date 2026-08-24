from db.connection import connection, get_connection
from xsettlers_mcp.tools.sector_tools import (
    show_sector_neighborhood, show_neighborhood_resources,
    UNKNOWN_CELL, SEEN_CELL, EMPTY_CELL, MAX_NEIGHBORHOOD_RADIUS, RICHEST_ROWS,
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

def _home(x=25, y=25, confidence=100, energy=50.0):
    """Player with one ship at (x,y,0) and visibility of that sector."""
    pid = seed_player(); sid = seed_sector(x, y, 0, energy=energy)
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
    with connection() as conn:
        before = (conn.execute("SELECT COUNT(*) n FROM sectors").fetchone()["n"],
                  conn.execute("SELECT COUNT(*) n FROM player_sectors").fetchone()["n"],
                  conn.execute("SELECT SUM(confidence) s FROM player_sectors").fetchone()["s"])
    show_sector_neighborhood("U_P1", org_id=oid)
    with connection() as conn:
        after = (conn.execute("SELECT COUNT(*) n FROM sectors").fetchone()["n"],
                 conn.execute("SELECT COUNT(*) n FROM player_sectors").fetchone()["n"],
                 conn.execute("SELECT SUM(confidence) s FROM player_sectors").fetchone()["s"])
    assert before == after

def test_show_sector_neighborhood_rejects_in_transit_org():
    pid, sid, oid = _home()
    with connection() as conn:
        conn.execute("UPDATE organizations SET sector_id=-1 WHERE id=?", (oid,))
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



def test_neighborhood_marks_a_remembered_sighting_apart_from_a_live_one():
    """"R" claims a rival is there now and is only shown where you stand; "r"
    claims a scan saw one there on some turn. Conflating them would let a
    sector you looked at fifty turns ago report today's traffic."""
    from db.sightings import record_sightings
    from xsettlers_mcp.tools.sector_tools import show_sector_neighborhood
    watcher = seed_player(email="w@t.com", player_token="U_W")
    rival = seed_player(email="r@t.com", player_token="U_R")
    here = seed_sector(0, 0, 0)
    scanned = seed_sector(2, 0, 0)
    seed_ship(watcher, here, name="Mine")
    rival_here = seed_ship(rival, here, name="Contested")
    rival_there = seed_ship(rival, scanned, name="Spotted")
    seed_player_sector(watcher, here, confidence=100)
    seed_player_sector(watcher, scanned, confidence=40)   # scanned a while back

    conn = get_connection(); cur = conn.cursor()
    record_sightings(cur, watcher, scanned, current_turn=3)
    conn.commit(); conn.close()

    result = show_sector_neighborhood("U_W", center_x=0, center_y=0, center_z=0)
    cells = {(s["coord_x"], s["coord_y"]): s for s in result["sectors"]}

    live = cells[(0, 0)]
    assert live["rival_orgs"] == 1 and live["sighted_rivals"] == 0
    assert live["cell"].endswith("!")           # rival alongside your own org

    remembered = cells[(2, 0)]
    assert remembered["rival_orgs"] == 0        # not reported live -- you aren't there
    assert remembered["sighted_rivals"] == 1
    assert remembered["sighted_at_turn"] == 3   # dated, so it reads as history
    assert remembered["cell"] == "r"

def test_a_remembered_rival_is_counted_in_the_highlights_table():
    """An "r" on the grid has to have a row under it: the count merges live
    and remembered rivals, and the confidence beside it is what says which
    kind it is. The row stands until the sector blinks out at confidence 0."""
    from db.sightings import record_sightings
    from xsettlers_mcp.tools.sector_tools import show_sector_neighborhood
    watcher = seed_player(email="w@t.com", player_token="U_W")
    rival = seed_player(email="r@t.com", player_token="U_R")
    here = seed_sector(0, 0, 0)
    scanned = seed_sector(2, 0, 0)
    seed_ship(watcher, here, name="Mine")
    seed_ship(rival, scanned, name="Spotted")
    seed_player_sector(watcher, here, confidence=100)
    seed_player_sector(watcher, scanned, confidence=40)

    conn = get_connection(); cur = conn.cursor()
    record_sightings(cur, watcher, scanned, current_turn=3)
    conn.commit(); conn.close()

    result = show_sector_neighborhood("U_W", center_x=0, center_y=0, center_z=0)
    highlights = {h["coords_display"]: h for h in result["highlights"]}
    assert highlights["(2,0,0)"]["rivals_display"] == "1/40"
    # No rival to age, so the confidence has nothing to qualify.
    assert highlights["(0,0,0)"]["rivals_display"] == "0/NA"

def test_a_live_rival_is_counted_in_the_highlights_table():
    """The same column carries a live rival, distinguishable only by the
    confidence of 100 beside it."""
    from xsettlers_mcp.tools.sector_tools import show_sector_neighborhood
    watcher = seed_player(email="w@t.com", player_token="U_W")
    rival = seed_player(email="r@t.com", player_token="U_R")
    here = seed_sector(0, 0, 0)
    seed_ship(watcher, here, name="Mine")
    seed_ship(rival, here, name="Contested")
    seed_player_sector(watcher, here, confidence=100)

    result = show_sector_neighborhood("U_W", center_x=0, center_y=0, center_z=0)
    assert result["highlights"][0]["rivals_display"] == "1/100"


# --- resource map (see show_neighborhood_resources) ---

def test_resource_map_reads_energy_in_thousands_and_marks_the_center():
    """Every cell is what that sector is worth, in thousands to two decimals
    so the figures line up, and the center is marked -- a grid of bare
    numbers has no other anchor to find yourself on."""
    pid, sid, oid = _home(energy=2200.0)
    rich = seed_sector(27, 25, 0, energy=900.0)
    seed_player_sector(pid, rich, 60)
    result = show_neighborhood_resources("U_P1", org_id=oid)
    assert _cell_at(result, 25, 25) == "2.20@"     # 2,200 energy, and you are here
    assert _cell_at(result, 27, 25) == " 0.90"     # 900, padded to the same width
    # The raw figure is untouched for a client that computes with it.
    assert next(s for s in result["sectors"]
                if s["id"] == rich)["energy_capacity"] == 900.0

def test_resource_map_dots_the_unseen_and_blanks_out_of_range():
    """Same three-state cell vocabulary as the neighborhood map: known,
    in range but never seen, and outside the radius entirely."""
    pid, sid, oid = _home()
    result = show_neighborhood_resources("U_P1", org_id=oid)
    assert _cell_at(result, 25, 21).strip() == UNKNOWN_CELL
    assert _cell_at(result, 21, 21).strip() == EMPTY_CELL
    assert result["unknown_in_range"] == IN_RANGE_CELLS_AT_R4 - 1

def test_resource_map_ranks_the_richest_sectors_it_can_see():
    """The shortlist the grid can't be: the question a resource map is opened
    to answer is where to go, and ties break on coordinates so the same board
    always ranks the same way."""
    pid, sid, oid = _home(energy=2200.0)
    for x, energy in ((26, 700.0), (27, 900.0), (24, 900.0)):
        seen = seed_sector(x, 25, 0, energy=energy)
        seed_player_sector(pid, seen, 80)
    result = show_neighborhood_resources("U_P1", org_id=oid)
    # Same unit as the grid: the table would otherwise report one quantity
    # two ways in one report.
    assert [(s["coords_display"], s["energy_display"]) for s in result["richest"]] == [
        ("(25,25,0)", "2.20"), ("(24,25,0)", "0.90"),
        ("(27,25,0)", "0.90"), ("(26,25,0)", "0.70")]
    assert result["display"]["rows_key"] == "richest"
    assert result["display"]["column_labels"]["energy_display"] == "Energy (×1k)"
    assert result["richest"][1]["confidence"] == 80

def test_resource_map_shortlist_is_capped():
    pid, sid, oid = _home()
    for x in range(21, 30):
        for y in (24, 26):
            seen = seed_sector(x, y, 0, energy=100.0 + x)
            seed_player_sector(pid, seen, 80)
    result = show_neighborhood_resources("U_P1", org_id=oid)
    assert len(result["sectors"]) > RICHEST_ROWS
    assert len(result["richest"]) == RICHEST_ROWS

def test_resource_map_shows_nothing_the_player_has_not_seen():
    """Fog of war is the same query both maps use: a sector that has blinked
    out (confidence 0) takes its reading off the map with it."""
    pid, sid, oid = _home()
    faded = seed_sector(26, 25, 0, energy=1000.0)
    seed_player_sector(pid, faded, 0)
    result = show_neighborhood_resources("U_P1", org_id=oid)
    assert _cell_at(result, 26, 25).strip() == UNKNOWN_CELL
    assert not any(s["id"] == faded for s in result["sectors"])

def test_resource_map_draws_the_same_viewport_as_the_neighborhood_map():
    """The two reports answer different questions about the same
    neighborhood, so they must never disagree about which sectors that is."""
    pid, sid, oid = _home()
    resources = show_neighborhood_resources("U_P1", org_id=oid)
    who = show_sector_neighborhood("U_P1", org_id=oid)
    assert resources["center"] == who["center"]
    assert resources["radius"] == who["radius"]
    assert resources["display"]["grid"]["x_labels"] == who["display"]["grid"]["x_labels"]
    assert ([r["label"] for r in resources["display"]["grid"]["rows"]]
            == [r["label"] for r in who["display"]["grid"]["rows"]])
    assert resources["unknown_in_range"] == who["unknown_in_range"]

def test_resource_map_counts_off_plane_sectors_without_drawing_them():
    pid, sid, oid = _home(energy=2200.0)
    upstairs = seed_sector(25, 25, 1, energy=3000.0)
    seed_player_sector(pid, upstairs, 100)
    result = show_neighborhood_resources("U_P1", org_id=oid)
    assert result["off_plane_count"] == 1
    assert _cell_at(result, 25, 25) == "2.20@"         # the plane's own sector, not the one above
    assert result["richest"][0]["coords_display"] == "(25,25,1)"   # still on the shortlist

def test_resource_map_accepts_explicit_coordinates():
    pid, sid, oid = _home()
    result = show_neighborhood_resources("U_P1", center_x=25, center_y=25, center_z=0, radius=1)
    assert result["center"] == {"x": 25, "y": 25, "z": 0}
    assert result["org_id"] is None
    assert len(result["display"]["grid"]["rows"]) == 3

def test_resource_map_is_a_pure_view():
    """Looking at what's nearby must not be what reveals it."""
    pid, sid, oid = _home()
    with connection() as conn:
        before = (conn.execute("SELECT COUNT(*) n FROM sectors").fetchone()["n"],
                  conn.execute("SELECT COUNT(*) n FROM player_sectors").fetchone()["n"],
                  conn.execute("SELECT SUM(confidence) s FROM player_sectors").fetchone()["s"])
    show_neighborhood_resources("U_P1", org_id=oid)
    with connection() as conn:
        after = (conn.execute("SELECT COUNT(*) n FROM sectors").fetchone()["n"],
                 conn.execute("SELECT COUNT(*) n FROM player_sectors").fetchone()["n"],
                 conn.execute("SELECT SUM(confidence) s FROM player_sectors").fetchone()["s"])
    assert before == after

def test_resource_map_rejects_in_transit_org():
    pid, sid, oid = _home()
    with connection() as conn:
        conn.execute("UPDATE organizations SET sector_id=-1 WHERE id=?", (oid,))
    assert "error" in show_neighborhood_resources("U_P1", org_id=oid)

def test_resource_map_rejects_missing_center_and_bad_radius():
    _home()
    assert "error" in show_neighborhood_resources("U_P1")
    assert "error" in show_neighborhood_resources("U_P1", center_x=1, center_y=1, center_z=0,
                                                  radius=0)
    assert "error" in show_neighborhood_resources(
        "U_P1", center_x=1, center_y=1, center_z=0, radius=MAX_NEIGHBORHOOD_RADIUS + 1)

def test_resource_map_rejects_unknown_player():
    assert show_neighborhood_resources("U_NOBODY", center_x=0, center_y=0, center_z=0) == \
        {"error": "Player not found"}
