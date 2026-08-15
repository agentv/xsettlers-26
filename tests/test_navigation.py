from db.connection import get_connection
from engine.movement import plan_move
from engine.turn import get_current_turn
from xsettlers_mcp.tools.navigation_tools import preview_move, confirm_move, cancel_move
from tests.conftest import seed_player, seed_sector, seed_ship

# --- plan_move (the arithmetic preview_move quotes and confirm_move commits) ---

def test_plan_move_costs_whole_turns_and_floors_at_one():
    assert plan_move((0, 0, 0), (3, 0, 0), 1, 0)["turns_needed"] == 3
    # A diagonal is sqrt(8) ~= 2.83, rounded up to 3 whole turns.
    assert plan_move((0, 0, 0), (2, 2, 0), 1, 0)["turns_needed"] == 3
    # Jump range divides the distance.
    assert plan_move((0, 0, 0), (6, 0, 0), 3, 0)["turns_needed"] == 2
    # Even a zero-distance move takes a turn.
    assert plan_move((5, 5, 0), (5, 5, 0), 1, 0)["turns_needed"] == 1

def test_plan_move_arrival_turn_is_one_past_the_landing_pass():
    """arrival_turn names the turn the org is free to act, one turn after the
    end_of_turn() pass that performs the landing (see engine/turn.py)."""
    assert plan_move((0, 0, 0), (3, 0, 0), 1, 10)["arrival_turn"] == 10 + 3 + 1

def test_preview_and_confirm_quote_the_same_travel_time():
    """The quote and the move that follows it must agree -- they share
    plan_move precisely so they cannot drift apart."""
    pid = seed_player(); oid = seed_sector(0, 0, 0)
    sid = seed_ship(pid, oid)
    quoted = preview_move("U_P1", sid, 4, 3, 0, jump_range_per_turn=2)
    committed = confirm_move("U_P1", sid, 4, 3, 0, jump_range_per_turn=2)
    assert quoted["turns_needed"] == committed["turns_needed"]
    assert quoted["arrival_turn"] == committed["arrival_turn"]

# --- preview_move ---

def test_preview_move_returns_travel_time():
    """Distance 3 at jump_range_per_turn=1 should take 3 turns."""
    pid = seed_player(); oid = seed_sector(0,0,0)
    sid = seed_ship(pid, oid)
    result = preview_move("U_P1", sid, 3, 0, 0, jump_range_per_turn=1)
    assert result["preview"] is True
    assert result["turns_needed"] == 3
    # arrival_turn is the turn the ship is free to act, one turn after the
    # end_of_turn() pass that performs the landing (see engine/turn.py).
    assert result["arrival_turn"] == get_current_turn() + 3 + 1

def test_preview_move_no_db_write():
    """preview_move must not park the ship or create an arrival_queue row."""
    pid = seed_player(); oid = seed_sector(0,0,0)
    sid = seed_ship(pid, oid)
    preview_move("U_P1", sid, 2, 0, 0)
    conn = get_connection()
    org = conn.execute("SELECT sector_id FROM organizations WHERE id=?", (sid,)).fetchone()
    aq  = conn.execute("SELECT * FROM arrival_queue WHERE org_id=?", (sid,)).fetchone()
    conn.close()
    assert org["sector_id"] != -1
    assert aq is None

# --- confirm_move ---

def test_confirm_move_parks_ship_at_sentinel():
    pid = seed_player(); oid = seed_sector(0,0,0)
    sid = seed_ship(pid, oid)
    result = confirm_move("U_P1", sid, 3, 0, 0)
    assert result["confirmed"] is True
    conn = get_connection()
    org = conn.execute("SELECT sector_id,mission FROM organizations WHERE id=?", (sid,)).fetchone()
    conn.close()
    assert org["sector_id"] == -1
    assert org["mission"] == "move"

def test_confirm_move_inserts_arrival_queue_with_origin():
    pid = seed_player(); oid = seed_sector(0,0,0)
    sid = seed_ship(pid, oid)
    result = confirm_move("U_P1", sid, 1, 0, 0)
    conn = get_connection()
    row = conn.execute("SELECT * FROM arrival_queue WHERE org_id=?", (sid,)).fetchone()
    conn.close()
    assert row is not None
    assert row["dest_x"] == 1 and row["dest_y"] == 0 and row["dest_z"] == 0
    assert row["origin_sector_id"] == oid
    assert row["arrival_turn"] == result["arrival_turn"]

def test_confirm_move_rejects_negative_coordinates():
    """Space has no negative indices -- a ship can't be sent there."""
    pid = seed_player(); oid = seed_sector(0,0,0)
    sid = seed_ship(pid, oid)
    assert "error" in confirm_move("U_P1", sid, -1, 0, 0)
    conn = get_connection()
    org = conn.execute("SELECT sector_id FROM organizations WHERE id=?", (sid,)).fetchone()
    conn.close()
    assert org["sector_id"] == oid  # unchanged -- still docked, not parked at sentinel

def test_preview_move_rejects_negative_coordinates():
    pid = seed_player(); oid = seed_sector(0,0,0)
    sid = seed_ship(pid, oid)
    assert "error" in preview_move("U_P1", sid, 0, 0, -2)

def test_confirm_move_colony_rejected():
    pid = seed_player(); sid = seed_sector()
    conn = get_connection()
    conn.execute("INSERT INTO organizations (org_type,name,player_id,sector_id,is_mobile,mission)"
                 " VALUES ('colony','Base',?,?,0,'idle')", (pid, sid))
    conn.commit()
    cid = conn.execute("SELECT id FROM organizations WHERE name='Base'").fetchone()["id"]
    conn.close()
    assert "error" in confirm_move("U_P1", cid, 1, 0, 0)

# --- cancel_move ---

def test_cancel_move_rubber_bands_to_origin():
    pid = seed_player(); oid = seed_sector(0,0,0)
    sid = seed_ship(pid, oid)
    confirm_move("U_P1", sid, 3, 0, 0)
    result = cancel_move("U_P1", sid)
    assert result["cancelled"] is True
    assert result["rubber_banded_to_sector_id"] == oid
    conn = get_connection()
    org = conn.execute("SELECT sector_id,mission FROM organizations WHERE id=?", (sid,)).fetchone()
    aq  = conn.execute("SELECT * FROM arrival_queue WHERE org_id=?", (sid,)).fetchone()
    conn.close()
    assert org["sector_id"] == oid
    assert org["mission"] == "idle"
    assert aq is None

def test_cancel_move_not_in_transit():
    pid = seed_player(); oid = seed_sector(); sid = seed_ship(pid, oid)
    assert "error" in cancel_move("U_P1", sid)


def test_arrival_reads_a_rivals_established_sector_rather_than_reseeding_it():
    """Arrival reveals the destination through the same reveal_sector() a scan
    uses, so a ship landing on ground a rival already stripped inherits what
    is actually left there -- it does not get a fresh full pool for being the
    first of *its* owner's units to see the place."""
    from engine.turn import end_of_turn
    from db.sectors import reveal_sector
    finder = seed_player(email="finder@t.com", player_token="U_FINDER")
    traveller = seed_player(email="trav@t.com", player_token="U_TRAV")
    # The finder discovers (2,0,0) and works it down to 40 energy.
    conn = get_connection(); cur = conn.cursor()
    contested = reveal_sector(cur, finder, 2, 0, 0)
    cur.execute("UPDATE sectors SET energy_capacity=40.0 WHERE id=?", (contested,))
    conn.commit(); conn.close()

    home = seed_sector(0, 0, 0)
    ship = seed_ship(traveller, home, name="Latecomer")
    confirm_move("U_TRAV", ship, 2, 0, 0)
    end_of_turn(); end_of_turn(); end_of_turn()   # distance 2, resolved once turn >= 2

    conn = get_connection()
    landed = conn.execute("SELECT sector_id FROM organizations WHERE id=?",
                          (ship,)).fetchone()["sector_id"]
    energy = conn.execute("SELECT energy_capacity AS e FROM sectors WHERE id=?",
                          (landed,)).fetchone()["e"]
    conn.close()
    assert landed == contested        # joined the existing row, didn't make a new one
    assert energy == 40.0             # inherited the depleted state
