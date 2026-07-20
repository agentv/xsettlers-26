from db.connection import get_connection
from engine.turn import end_of_turn
from mcp.tools.organization_tools import set_mission, set_pod_mission
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod

# --- set_mission happy path ---

def test_set_mission_idle():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    result = set_mission("U_P1", oid, "idle")
    assert result.get("ok") is True
    conn = get_connection()
    assert conn.execute("SELECT mission FROM organizations WHERE id=?",
                        (oid,)).fetchone()["mission"] == "idle"
    conn.close()

def test_set_mission_colonize_locks_immediately_but_does_not_convert_yet():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    set_mission("U_P1", oid, "colonize")
    conn = get_connection()
    org = conn.execute("SELECT org_type,is_mobile,mission FROM organizations WHERE id=?",
                       (oid,)).fetchone()
    conn.close()
    assert org["org_type"] == "ship"      # not flipped yet -- only at resolution
    assert org["is_mobile"] == 0          # locked immediately, per set_mission
    assert org["mission"] == "colonize"

def test_set_mission_colonize_converts_after_three_turns():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    set_mission("U_P1", oid, "colonize")   # scheduled for current_turn(0) + 3
    end_of_turn(); end_of_turn()           # turns 1, 2 -- not resolved yet
    conn = get_connection()
    still_ship = conn.execute("SELECT org_type FROM organizations WHERE id=?",
                              (oid,)).fetchone()["org_type"]
    conn.close()
    assert still_ship == "ship"
    end_of_turn()                          # turn 3 -- resolve_at_turn matches
    conn = get_connection()
    org = conn.execute("SELECT org_type,is_mobile,mission FROM organizations WHERE id=?",
                       (oid,)).fetchone()
    conn.close()
    assert org["org_type"] == "colony"
    assert org["is_mobile"] == 0
    assert org["mission"] == "idle"

# --- set_mission negative paths ---

def test_set_mission_unknown_player():
    assert "error" in set_mission("U_NOBODY", 1, "idle")

def test_set_mission_invalid_type():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    assert "error" in set_mission("U_P1", oid, "dance")

def test_set_mission_colony_cannot_move():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    set_mission("U_P1", oid, "colonize"); end_of_turn()
    assert "error" in set_mission("U_P1", oid, "move")

def test_set_mission_unowned_org():
    p1 = seed_player(email="p1@t.com", slack_id="U_P1")
    p2 = seed_player(email="p2@t.com", slack_id="U_P2")
    sid = seed_sector(); oid = seed_ship(p2, sid, name="Enemy")
    assert "error" in set_mission("U_P1", oid, "idle")

# --- set_pod_mission happy path ---

def test_set_pod_mission_produce_energy():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    result = set_pod_mission("U_P1", pod, "produce_energy")
    assert result.get("ok") is True
    conn = get_connection()
    assert conn.execute("SELECT mission FROM pods WHERE id=?",
                        (pod,)).fetchone()["mission"] == "produce_energy"
    conn.close()

def test_produce_fills_storage_at_end_of_turn():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid, storage_capacity=100.0, storage_current=0.0)
    set_pod_mission("U_P1", pod, "produce_energy"); end_of_turn()
    conn = get_connection()
    assert conn.execute("SELECT storage_current FROM pods WHERE id=?",
                        (pod,)).fetchone()["storage_current"] > 0
    conn.close()

def test_storage_capped_at_capacity():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid, storage_capacity=100.0, storage_current=95.0)
    set_pod_mission("U_P1", pod, "produce_energy"); end_of_turn()
    conn = get_connection()
    assert conn.execute("SELECT storage_current FROM pods WHERE id=?",
                        (pod,)).fetchone()["storage_current"] == 100.0
    conn.close()

def test_pods_in_transit_produce():
    """Pods produce regardless of whether their parent ship is in transit.
    Canonical rule: production runs on all pods every turn, transit state does not suppress it.
    """
    from mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); oid = seed_sector(0,0,0); did = seed_sector(3,0,0)
    ship = seed_ship(pid, oid)
    pod  = seed_pod(ship, storage_capacity=100.0, storage_current=0.0)
    set_pod_mission("U_P1", pod, "produce_energy"); confirm_move("U_P1", ship, did); end_of_turn()
    conn = get_connection()
    assert conn.execute("SELECT storage_current FROM pods WHERE id=?",
                        (pod,)).fetchone()["storage_current"] > 0.0
    conn.close()

# --- set_pod_mission negative paths ---

def test_set_pod_mission_unknown_player():
    assert "error" in set_pod_mission("U_NOBODY", 1, "produce_energy")

def test_set_pod_mission_invalid_type():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    assert "error" in set_pod_mission("U_P1", pod, "explode")

def test_set_pod_mission_unowned_pod():
    p1 = seed_player(email="p1@t.com", slack_id="U_P1")
    p2 = seed_player(email="p2@t.com", slack_id="U_P2")
    sid = seed_sector(); oid = seed_ship(p2, sid); pod = seed_pod(oid)
    assert "error" in set_pod_mission("U_P1", pod, "produce_energy")
