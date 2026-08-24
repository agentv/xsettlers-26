from db.connection import connection, get_connection
from engine.turn import end_of_turn
from engine.production import COLONIZATION_ENERGY_COST
from xsettlers_mcp.tools.organization_tools import set_mission
from xsettlers_mcp.tools.task_force_tools import (
    create_task_force, add_to_task_force, remove_from_task_force,
    disband_task_force, list_task_forces, order_task_force,
    TASK_FORCE_NOT_OWNED, NOT_A_SHIP,
)
from xsettlers_mcp.tools.session import ORG_NOT_OWNED
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod


def _seed_colony(player_id, sector_id, name="Test Colony"):
    conn = get_connection()
    conn.execute("""INSERT INTO organizations
        (org_type,name,player_id,sector_id,is_mobile,mission)
        VALUES ('colony',?,?,?,0,'idle')""", (name, player_id, sector_id))
    conn.commit()
    oid = conn.execute("SELECT id FROM organizations WHERE name=? AND player_id=?",
                       (name, player_id)).fetchone()["id"]
    conn.close(); return oid


def _task_force_id_of(org_id):
    with connection() as conn:
        row = conn.execute("SELECT task_force_id FROM organizations WHERE id=?", (org_id,)).fetchone()
    return row["task_force_id"]


# --- create_task_force ---

def test_create_task_force_with_initial_roster():
    pid = seed_player(); sid = seed_sector()
    o1 = seed_ship(pid, sid, "S1"); o2 = seed_ship(pid, sid, "S2")
    result = create_task_force("U_P1", "Vanguard", [o1, o2])
    assert result["ok"] is True
    tfid = result["task_force_id"]
    assert _task_force_id_of(o1) == tfid
    assert _task_force_id_of(o2) == tfid


def test_create_task_force_empty_roster():
    seed_player()
    result = create_task_force("U_P1", "Scouts", [])
    assert result["ok"] is True
    assert result["member_org_ids"] == []


def test_create_task_force_rejects_duplicate_name():
    seed_player()
    create_task_force("U_P1", "Vanguard", [])
    result = create_task_force("U_P1", "Vanguard", [])
    assert "error" in result


def test_create_task_force_rejects_colony_in_roster():
    pid = seed_player(); sid = seed_sector()
    cid = _seed_colony(pid, sid)
    result = create_task_force("U_P1", "Vanguard", [cid])
    assert result["error"] == NOT_A_SHIP
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM task_forces").fetchone()["n"] == 0


def test_create_task_force_rejects_org_not_owned():
    seed_player()
    other_pid = seed_player(email="other@test.com", player_token="U_P2")
    sid = seed_sector()
    other_ship = seed_ship(other_pid, sid)
    result = create_task_force("U_P1", "Vanguard", [other_ship])
    assert result["error"] == ORG_NOT_OWNED


def test_create_task_force_rejects_ship_already_in_another_task_force():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    create_task_force("U_P1", "First", [oid])
    result = create_task_force("U_P1", "Second", [oid])
    assert "error" in result
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM task_forces WHERE name='Second'"
                            ).fetchone()["n"] == 0


# --- add / remove membership ---

def test_add_to_task_force():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    tfid = create_task_force("U_P1", "Vanguard", [])["task_force_id"]
    result = add_to_task_force("U_P1", tfid, oid)
    assert result["ok"] is True
    assert _task_force_id_of(oid) == tfid


def test_add_to_task_force_rejects_colony():
    pid = seed_player(); sid = seed_sector()
    cid = _seed_colony(pid, sid)
    tfid = create_task_force("U_P1", "Vanguard", [])["task_force_id"]
    result = add_to_task_force("U_P1", tfid, cid)
    assert result["error"] == NOT_A_SHIP


def test_add_to_task_force_rejects_already_a_member_elsewhere():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    tf1 = create_task_force("U_P1", "First", [oid])["task_force_id"]
    tf2 = create_task_force("U_P1", "Second", [])["task_force_id"]
    result = add_to_task_force("U_P1", tf2, oid)
    assert "error" in result
    assert _task_force_id_of(oid) == tf1


def test_add_to_task_force_unowned_task_force():
    pid = seed_player()
    other_pid = seed_player(email="other@test.com", player_token="U_P2")
    sid = seed_sector()
    tfid = create_task_force("U_P2", "Theirs", [])["task_force_id"]
    oid = seed_ship(pid, sid)
    result = add_to_task_force("U_P1", tfid, oid)
    assert result["error"] == TASK_FORCE_NOT_OWNED


def test_remove_from_task_force():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    tfid = create_task_force("U_P1", "Vanguard", [oid])["task_force_id"]
    result = remove_from_task_force("U_P1", tfid, oid)
    assert result["ok"] is True
    assert _task_force_id_of(oid) is None


def test_remove_from_task_force_not_a_member():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    tfid = create_task_force("U_P1", "Vanguard", [])["task_force_id"]
    result = remove_from_task_force("U_P1", tfid, oid)
    assert "error" in result


def test_remove_from_task_force_does_not_touch_other_org_state():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    tfid = create_task_force("U_P1", "Vanguard", [oid])["task_force_id"]
    remove_from_task_force("U_P1", tfid, oid)
    with connection() as conn:
        org = conn.execute("SELECT mission, sector_id FROM organizations WHERE id=?", (oid,)).fetchone()
    assert org["mission"] == "idle"
    assert org["sector_id"] == sid


# --- disband ---

def test_disband_task_force_frees_members_and_deletes_roster():
    pid = seed_player(); sid = seed_sector()
    o1 = seed_ship(pid, sid, "S1"); o2 = seed_ship(pid, sid, "S2")
    tfid = create_task_force("U_P1", "Vanguard", [o1, o2])["task_force_id"]
    result = disband_task_force("U_P1", tfid)
    assert result["ok"] is True
    assert _task_force_id_of(o1) is None
    assert _task_force_id_of(o2) is None
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM task_forces WHERE id=?",
                            (tfid,)).fetchone()["n"] == 0


def test_disband_task_force_unowned():
    seed_player(email="other@test.com", player_token="U_P2")
    tfid = create_task_force("U_P2", "Theirs", [])["task_force_id"]
    seed_player()
    result = disband_task_force("U_P1", tfid)
    assert result["error"] == TASK_FORCE_NOT_OWNED


# --- list ---

def test_list_task_forces():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid, "S1")
    create_task_force("U_P1", "Vanguard", [oid])
    result = list_task_forces("U_P1")
    assert result["ok"] is True
    assert len(result["task_forces"]) == 1
    tf = result["task_forces"][0]
    assert tf["name"] == "Vanguard"
    assert tf["members"] == [{"org_id": oid, "name": "S1"}]


# --- colonizing removes membership automatically ---

def test_colonizing_removes_ship_from_task_force():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=200.0)
    tfid = create_task_force("U_P1", "Vanguard", [oid])["task_force_id"]
    set_mission("U_P1", oid, "colonize")
    assert _task_force_id_of(oid) == tfid   # not flipped yet
    end_of_turn(); end_of_turn(); end_of_turn()  # resolve_at_turn = 3
    assert _task_force_id_of(oid) is None
    with connection() as conn:
        tf_members = conn.execute("SELECT COUNT(*) AS n FROM organizations WHERE task_force_id=?",
                                  (tfid,)).fetchone()["n"]
    assert tf_members == 0


# --- order_task_force: fan-out ---

def test_order_task_force_moves_every_member():
    pid = seed_player(); sid = seed_sector(0, 0, 0)
    seed_sector(5, 5, 0)
    o1 = seed_ship(pid, sid, "S1"); o2 = seed_ship(pid, sid, "S2")
    tfid = create_task_force("U_P1", "Vanguard", [o1, o2])["task_force_id"]
    result = order_task_force("U_P1", tfid, "move",
                              {"dest_x": 5, "dest_y": 5, "dest_z": 0})
    assert result["ok"] is True
    assert result["member_count"] == 2
    assert all(r.get("confirmed") for r in result["results"])
    conn = get_connection()
    for oid in (o1, o2):
        assert conn.execute("SELECT sector_id FROM organizations WHERE id=?",
                            (oid,)).fetchone()["sector_id"] == -1  # parked in transit
    conn.close()


def test_order_task_force_partial_failure_reported_per_member():
    """One member mid-colonization can't accept a new mission; the other
    still goes through, and nothing rolls back for either."""
    pid = seed_player(); sid = seed_sector(0, 0, 0)
    seed_sector(5, 5, 0)
    ok_ship = seed_ship(pid, sid, "S1")
    locked_ship = seed_ship(pid, sid, "S2")
    seed_pod(locked_ship, task="produce_energy", storage_current=200.0)
    set_mission("U_P1", locked_ship, "colonize")   # locks it (is_mobile=0)
    tfid = create_task_force("U_P1", "Vanguard", [ok_ship, locked_ship])["task_force_id"]
    result = order_task_force("U_P1", tfid, "idle")
    by_org = {r["org_id"]: r for r in result["results"]}
    assert by_org[ok_ship].get("ok") is True
    assert "error" in by_org[locked_ship]


def test_order_task_force_empty_roster():
    seed_player()
    tfid = create_task_force("U_P1", "Empty", [])["task_force_id"]
    result = order_task_force("U_P1", tfid, "idle")
    assert result["ok"] is True
    assert result["results"] == []


def test_order_task_force_unowned():
    seed_player(email="other@test.com", player_token="U_P2")
    tfid = create_task_force("U_P2", "Theirs", [])["task_force_id"]
    seed_player()
    result = order_task_force("U_P1", tfid, "idle")
    assert result["error"] == TASK_FORCE_NOT_OWNED
