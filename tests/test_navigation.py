from db.connection import get_connection
from engine.turn import get_current_turn
from xsettlers_mcp.tools.navigation_tools import (
    preview_move, confirm_move, cancel_move, get_organizations_in_range
)
from tests.conftest import seed_player, seed_sector, seed_ship

# --- preview_move ---

def test_preview_move_returns_travel_time():
    """Distance 3 at jump_range_per_turn=1 should take 3 turns."""
    pid = seed_player(); oid = seed_sector(0,0,0); did = seed_sector(3,0,0)
    sid = seed_ship(pid, oid)
    result = preview_move("U_P1", sid, did, jump_range_per_turn=1)
    assert result["preview"] is True
    assert result["turns_needed"] == 3
    assert result["arrival_turn"] == get_current_turn() + 3

def test_preview_move_no_db_write():
    """preview_move must not park the ship or create an arrival_queue row."""
    pid = seed_player(); oid = seed_sector(0,0,0); did = seed_sector(2,0,0)
    sid = seed_ship(pid, oid)
    preview_move("U_P1", sid, did)
    conn = get_connection()
    org = conn.execute("SELECT sector_id FROM organizations WHERE id=?", (sid,)).fetchone()
    aq  = conn.execute("SELECT * FROM arrival_queue WHERE org_id=?", (sid,)).fetchone()
    conn.close()
    assert org["sector_id"] != -1
    assert aq is None

# --- confirm_move ---

def test_confirm_move_parks_ship_at_sentinel():
    pid = seed_player(); oid = seed_sector(0,0,0); did = seed_sector(3,0,0)
    sid = seed_ship(pid, oid)
    result = confirm_move("U_P1", sid, did)
    assert result["confirmed"] is True
    conn = get_connection()
    org = conn.execute("SELECT sector_id,mission FROM organizations WHERE id=?", (sid,)).fetchone()
    conn.close()
    assert org["sector_id"] == -1
    assert org["mission"] == "move"

def test_confirm_move_inserts_arrival_queue_with_origin():
    pid = seed_player(); oid = seed_sector(0,0,0); did = seed_sector(1,0,0)
    sid = seed_ship(pid, oid)
    result = confirm_move("U_P1", sid, did)
    conn = get_connection()
    row = conn.execute("SELECT * FROM arrival_queue WHERE org_id=?", (sid,)).fetchone()
    conn.close()
    assert row is not None
    assert row["dest_sector_id"] == did
    assert row["origin_sector_id"] == oid
    assert row["arrival_turn"] == result["arrival_turn"]

def test_confirm_move_colony_rejected():
    pid = seed_player(); sid = seed_sector()
    conn = get_connection()
    conn.execute("INSERT INTO organizations (org_type,name,player_id,sector_id,is_mobile,mission)"
                 " VALUES ('colony','Base',?,?,0,'idle')", (pid, sid))
    conn.commit()
    cid = conn.execute("SELECT id FROM organizations WHERE name='Base'").fetchone()["id"]
    conn.close()
    assert "error" in confirm_move("U_P1", cid, seed_sector(1,0,0))

# --- cancel_move ---

def test_cancel_move_rubber_bands_to_origin():
    pid = seed_player(); oid = seed_sector(0,0,0); did = seed_sector(3,0,0)
    sid = seed_ship(pid, oid)
    confirm_move("U_P1", sid, did)
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

# --- get_organizations_in_range ---

def test_range_returns_nearby_excludes_far():
    pid = seed_player(); oid = seed_sector(0,0,0)
    near = seed_sector(1,0,0); far = seed_sector(5,0,0)
    sid  = seed_ship(pid, oid)
    ids = [r["id"] for r in get_organizations_in_range("U_P1", sid, jump_range=2)]
    assert near in ids
    assert far not in ids

def test_range_rejects_transit_ship():
    pid = seed_player(); oid = seed_sector(0,0,0); did = seed_sector(2,0,0)
    sid = seed_ship(pid, oid)
    confirm_move("U_P1", sid, did)
    assert "error" in get_organizations_in_range("U_P1", sid, jump_range=3)
