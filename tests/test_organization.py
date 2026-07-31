import json
import pytest
from db.connection import get_connection
from engine.turn import end_of_turn
from xsettlers_mcp.tools.sector_tools import SCAN_RANGE
from xsettlers_mcp.tools.organization_tools import (
    set_mission, set_pod_task, show_civilization_status, show_game_status,
    rename_organization, set_org_scan_bearing, set_pod_scan_bearing,
    show_organization
)
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
    p1 = seed_player(email="p1@t.com", player_token="U_P1")
    p2 = seed_player(email="p2@t.com", player_token="U_P2")
    sid = seed_sector(); oid = seed_ship(p2, sid, name="Enemy")
    assert "error" in set_mission("U_P1", oid, "idle")

# --- set_mission('move') delegates to confirm_move ---

def test_set_mission_move_delegates_to_confirm_move():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    result = set_mission("U_P1", oid, "move", {"dest_x": 3, "dest_y": 0, "dest_z": 0})
    assert result.get("confirmed") is True
    assert result["arrival_turn"] > 0
    conn = get_connection()
    org = conn.execute("SELECT sector_id,is_mobile,mission FROM organizations WHERE id=?",
                       (oid,)).fetchone()
    queued = conn.execute("SELECT dest_x,dest_y,dest_z FROM arrival_queue WHERE org_id=?",
                          (oid,)).fetchone()
    conn.close()
    assert org["sector_id"] == -1          # parked at the sentinel, not left in place
    assert org["is_mobile"] == 0
    assert org["mission"] == "move"
    assert queued is not None              # a real arrival_queue row exists to resolve it
    assert (queued["dest_x"], queued["dest_y"], queued["dest_z"]) == (3, 0, 0)

def test_set_mission_move_requires_dest_params():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    result = set_mission("U_P1", oid, "move", {"dest_x": 3})
    assert "error" in result
    conn = get_connection()
    org = conn.execute("SELECT sector_id,mission FROM organizations WHERE id=?",
                       (oid,)).fetchone()
    conn.close()
    assert org["sector_id"] != -1          # rejected before anything was mutated
    assert org["mission"] != "move"

# --- set_pod_task happy path ---

def test_set_pod_task_produce_energy():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    result = set_pod_task("U_P1", pod, "produce_energy")
    assert result.get("ok") is True
    conn = get_connection()
    assert conn.execute("SELECT task FROM pods WHERE id=?",
                        (pod,)).fetchone()["task"] == "produce_energy"
    conn.close()

def test_storage_capped_at_capacity():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid, storage_capacity=100.0, storage_current=95.0)
    seed_pod(oid, task="produce_food", storage_current=100.0)
    set_pod_task("U_P1", pod, "produce_energy"); end_of_turn()
    conn = get_connection()
    assert conn.execute("SELECT energy_stored FROM pods WHERE id=?",
                        (pod,)).fetchone()["energy_stored"] == 100.0
    conn.close()

def test_non_energy_production_continues_in_transit_if_input_available():
    """produce_goods isn't sector-sourced -- only its energy input cost matters,
    drawn from the org's own stock -- so it keeps producing in transit as long
    as that stock holds out. (produce_energy is different: see
    test_energy_production_stops_during_transit below.)
    """
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); oid = seed_sector(0,0,0)
    ship = seed_ship(pid, oid)
    energy_pod = seed_pod(ship, task="produce_energy", storage_current=100.0)  # feeds goods' input
    seed_pod(ship, task="produce_food", storage_current=100.0)  # feeds goods' + org upkeep's food cost
    goods_pod  = seed_pod(ship, storage_capacity=100.0, storage_current=0.0)
    set_pod_task("U_P1", goods_pod, "produce_goods")
    confirm_move("U_P1", ship, 3, 0, 0); end_of_turn()
    conn = get_connection()
    assert conn.execute("SELECT goods_stored FROM pods WHERE id=?",
                        (goods_pod,)).fetchone()["goods_stored"] > 0.0
    conn.close()

# --- sector resource depletion (see engine/production.py's RESOURCE_CAPACITY_COLUMN) ---

def test_production_depletes_sector_capacity():
    pid = seed_player(); sid = seed_sector(energy=50.0)
    oid = seed_ship(pid, sid)
    pod = seed_pod(oid, storage_capacity=100.0, storage_current=0.0)
    seed_pod(oid, task="produce_food", storage_current=100.0)
    set_pod_task("U_P1", pod, "produce_energy"); end_of_turn()
    conn = get_connection()
    sector = conn.execute("SELECT energy_capacity FROM sectors WHERE id=?", (sid,)).fetchone()
    pod_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()
    conn.close()
    assert sector["energy_capacity"] == 40.0  # 50 - flat rate 10
    assert pod_row["energy_stored"] == 10.0

def test_production_floors_at_zero_and_stops_once_depleted():
    pid = seed_player(); sid = seed_sector(energy=5.0)  # less than the flat rate (10)
    oid = seed_ship(pid, sid)
    pod = seed_pod(oid, storage_capacity=100.0, storage_current=0.0)
    seed_pod(oid, task="produce_food", storage_current=100.0)
    set_pod_task("U_P1", pod, "produce_energy"); end_of_turn()
    conn = get_connection()
    sector = conn.execute("SELECT energy_capacity FROM sectors WHERE id=?", (sid,)).fetchone()
    pod_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()
    conn.close()
    assert sector["energy_capacity"] == 0.0
    assert pod_row["energy_stored"] == 5.0  # capped by what the sector had left
    end_of_turn()  # sector is now empty -- no further production gain, but org
                   # upkeep (1 energy/turn) still draws down the 5.0 already banked
    conn = get_connection()
    pod_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()
    conn.close()
    assert pod_row["energy_stored"] == 4.0

def test_energy_production_stops_during_transit():
    """produce_energy harvests from the sector it's sitting in -- a ship in
    transit is parked at the sentinel sector (-1), which is permanently at
    0 capacity, so energy production is 0 while traveling regardless of how
    much food is available to pay its input cost."""
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); oid = seed_sector(0,0,0, energy=1000.0)
    ship = seed_ship(pid, oid)
    pod = seed_pod(ship, storage_capacity=100.0, storage_current=0.0)
    seed_pod(ship, task="produce_food", storage_current=100.0)  # plenty of food available
    set_pod_task("U_P1", pod, "produce_energy"); confirm_move("U_P1", ship, 3, 0, 0); end_of_turn()
    conn = get_connection()
    pod_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()
    conn.close()
    assert pod_row["energy_stored"] == 0.0

# --- typed, mission-independent storage with spillover (see engine/turn.py) ---

def test_retasking_a_pod_does_not_clear_its_storage():
    """The bug this whole model exists to fix: a pod's stored resources are
    real inventory, not a label derived from its current mission. Retasking
    must never wipe or hide what's already there."""
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid, task="produce_energy", storage_current=42.0)
    set_pod_task("U_P1", pod, "idle")
    conn = get_connection()
    energy = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()["energy_stored"]
    conn.close()
    assert energy == 42.0  # untouched by the retask

def test_retasked_pods_energy_still_counts_toward_org_pool():
    """The original bug: idling an org's only energy-holding pods made their
    stored energy invisible to consumption, even though it physically
    remained. Now it must still be available to pay other pods' recipes."""
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    energy_pod = seed_pod(oid, task="produce_energy", storage_current=100.0)
    set_pod_task("U_P1", energy_pod, "idle")  # retask away from produce_energy
    goods_pod = seed_pod(oid, storage_capacity=100.0, storage_current=0.0)
    set_pod_task("U_P1", goods_pod, "produce_goods")  # needs 2 energy + 1 food
    seed_pod(oid, task="produce_food", storage_current=100.0)
    end_of_turn()
    conn = get_connection()
    goods = conn.execute("SELECT goods_stored FROM pods WHERE id=?", (goods_pod,)).fetchone()["goods_stored"]
    conn.close()
    assert goods > 0.0  # produced fine, drawing on the now-idle pod's banked energy

def test_production_overflow_spills_into_sibling_pod():
    """A producing pod already full should spill its output into another pod
    in the same org with free space, rather than losing it."""
    pid = seed_player(); sid = seed_sector(energy=1000.0); oid = seed_ship(pid, sid)
    full_pod = seed_pod(oid, task="produce_energy", storage_capacity=10.0, storage_current=10.0)
    empty_pod = seed_pod(oid, storage_capacity=100.0, storage_current=0.0)  # idle, all free space
    seed_pod(oid, task="produce_food", storage_current=100.0)
    end_of_turn()
    conn = get_connection()
    full_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (full_pod,)).fetchone()
    empty_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (empty_pod,)).fetchone()
    conn.close()
    # Org upkeep (1 energy/turn) runs first and drains 1 from full_pod (the
    # only energy source), opening 1 unit of free space there; production
    # then makes 10 more, refills full_pod's freed unit first (back to its
    # 10 capacity), and the remaining 9 spills into empty_pod.
    assert full_row["energy_stored"] == 10.0  # topped back up to its own capacity
    assert empty_row["energy_stored"] == 9.0  # the rest spilled over here

def test_production_overflow_lost_when_org_fully_saturated():
    """If every pod in the org is completely full, excess production is
    simply lost -- not stored anywhere, not an error."""
    pid = seed_player(); sid = seed_sector(energy=1000.0); oid = seed_ship(pid, sid)
    full_energy_pod = seed_pod(oid, task="produce_energy", storage_capacity=10.0, storage_current=10.0)
    full_food_pod = seed_pod(oid, task="produce_food", storage_capacity=10.0, storage_current=10.0)
    end_of_turn()
    conn = get_connection()
    row = conn.execute("SELECT energy_stored,food_stored,goods_stored FROM pods WHERE id=?",
                       (full_energy_pod,)).fetchone()
    conn.close()
    # nothing to spill into (only other pod in the org is also full) -- the
    # 10 units this pod would have produced (capped by its 1-food cost being
    # affordable) are simply lost, not stored anywhere or raising an error
    assert row["energy_stored"] == 10.0

# --- set_pod_task negative paths ---

def test_set_pod_task_unknown_player():
    assert "error" in set_pod_task("U_NOBODY", 1, "produce_energy")

def test_set_pod_task_invalid_type():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    assert "error" in set_pod_task("U_P1", pod, "explode")

def test_set_pod_task_unowned_pod():
    p1 = seed_player(email="p1@t.com", player_token="U_P1")
    p2 = seed_player(email="p2@t.com", player_token="U_P2")
    sid = seed_sector(); oid = seed_ship(p2, sid); pod = seed_pod(oid)
    assert "error" in set_pod_task("U_P1", pod, "produce_energy")

# --- set_pod_task scan target ---

def test_set_pod_task_scan_stores_its_aim_as_an_offset():
    pid = seed_player(); sid = seed_sector(4, 4, 0); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    result = set_pod_task("U_P1", pod, "scan", bearing="NE")
    assert result["bearing"] == "NE"
    assert (result["offset_x"], result["offset_y"], result["offset_z"]) == (1, -1, 0)
    conn = get_connection()
    params = json.loads(conn.execute("SELECT task_params FROM pods WHERE id=?",
                                     (pod,)).fetchone()["task_params"])
    conn.close()
    # Stored relative, not as the absolute (5,3,0) it currently points at.
    assert params == {"offset_x": 1, "offset_y": -1, "offset_z": 0}

@pytest.mark.parametrize("bearing,offset,distance", [
    ("N",  (0, -1, 0), 1.0),
    ("NE", (1, -1, 0), pytest.approx(1.4142135623)),   # legal only since SCAN_RANGE 2
    ("E2", (2, 0, 0),  2.0),                            # the outer edge
])
def test_scan_bearings_resolve_to_offsets_and_report_reach(bearing, offset, distance):
    """One call gives the whole picture: the offset, its compass name, how far
    it reaches, and whether that is legal."""
    pid = seed_player(); sid = seed_sector(3, 3, 0); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    result = set_pod_task("U_P1", pod, "scan", bearing=bearing)
    assert (result["offset_x"], result["offset_y"], result["offset_z"]) == offset
    assert result["distance"] == distance
    assert result["scan_range"] == SCAN_RANGE
    assert result["in_range"] is True

def test_north_is_negative_y():
    """Fixed convention -- north is up on the neighborhood map, which renders
    y ascending downward. Arbitrary, but everything player-facing depends on it."""
    pid = seed_player(); sid = seed_sector(5, 5, 0); oid = seed_ship(pid, sid)
    result = set_org_scan_bearing("U_P1", oid, "N")
    assert result["offset_y"] == -1
    assert result["aimed_at"] == [5, 4, 0]

def test_unknown_bearing_is_rejected():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    assert "error" in set_org_scan_bearing("U_P1", oid, "NNE")

def test_bearing_and_explicit_offset_are_mutually_exclusive():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    result = set_org_scan_bearing("U_P1", oid, "N", offset_x=1, offset_y=0, offset_z=0)
    assert "error" in result and "not both" in result["error"]

def test_partial_offset_is_rejected():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    assert "error" in set_org_scan_bearing("U_P1", oid, offset_x=1)

def test_aim_can_be_set_while_in_transit():
    """An offset needs no position to validate -- unlike absolute targeting,
    which could not compute range for a ship at the sentinel sector."""
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    confirm_move("U_P1", oid, 4, 0, 0)
    result = set_org_scan_bearing("U_P1", oid, "E")
    assert result["ok"] is True and result["in_range"] is True
    assert result["aimed_at"] is None          # no position yet to point from

def test_set_pod_scan_bearing_requires_the_scan_task():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid, task="produce_food")
    assert "error" in set_pod_scan_bearing("U_P1", pod, "N")

def test_set_pod_scan_bearing_clears_when_given_nothing():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid, task="scan")
    set_pod_scan_bearing("U_P1", pod, "N")
    assert set_pod_scan_bearing("U_P1", pod)["cleared"] is True
    conn = get_connection()
    assert conn.execute("SELECT task_params FROM pods WHERE id=?",
                        (pod,)).fetchone()["task_params"] is None
    conn.close()

def test_aim_is_rejected_on_a_non_scan_task():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    assert "error" in set_pod_task("U_P1", pod, "produce_food", bearing="N")

# --- rename_organization (players refer to units by name) ---

def test_rename_organization_sets_a_new_name():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid, name="S1")
    result = rename_organization("U_P1", oid, "Vanguard")
    assert result["ok"] is True
    assert (result["previous_name"], result["name"]) == ("S1", "Vanguard")
    conn = get_connection()
    assert conn.execute("SELECT name FROM organizations WHERE id=?", (oid,)).fetchone()["name"] == "Vanguard"
    conn.close()

def test_rename_organization_rejects_a_duplicate_within_one_player():
    """An ambiguous name is not a name -- names are the player's handle for
    issuing orders, so they must resolve to exactly one unit."""
    pid = seed_player(); sid = seed_sector()
    seed_ship(pid, sid, name="Vanguard"); other = seed_ship(pid, sid, name="S2")
    result = rename_organization("U_P1", other, "vanguard")   # case-insensitive
    assert "error" in result and "already have" in result["error"]

def test_rename_organization_allows_the_same_name_for_different_players():
    """Uniqueness is per player: neither can see the other's roster."""
    p1 = seed_player(); p2 = seed_player(email="b@test.com", player_token="U_P2", display_name="Two")
    sid = seed_sector()
    a = seed_ship(p1, sid, name="S1"); b = seed_ship(p2, sid, name="S1")
    assert rename_organization("U_P1", a, "Vanguard")["ok"] is True
    assert rename_organization("U_P2", b, "Vanguard")["ok"] is True

def test_rename_organization_rejects_empty_and_overlong_names():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    assert "error" in rename_organization("U_P1", oid, "   ")
    assert "error" in rename_organization("U_P1", oid, "x" * 25)
    assert rename_organization("U_P1", oid, "  Trimmed  ")["name"] == "Trimmed"

def test_rename_organization_is_ownership_gated():
    p1 = seed_player(); seed_player(email="b@test.com", player_token="U_P2", display_name="Two")
    sid = seed_sector(); oid = seed_ship(p1, sid)
    assert "error" in rename_organization("U_P2", oid, "Stolen")

# --- innate organization scan (a ship's bridge / colony HQ) ---

def test_set_org_scan_target_reports_range_and_persists():
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    result = set_org_scan_bearing("U_P1", oid, "NE")
    assert result["in_range"] is True and result["scan_range"] == SCAN_RANGE
    conn = get_connection()
    row = conn.execute("SELECT scan_offset_x,scan_offset_y,scan_offset_z FROM organizations WHERE id=?",
                       (oid,)).fetchone()
    conn.close()
    assert (row["scan_offset_x"], row["scan_offset_y"], row["scan_offset_z"]) == (1, -1, 0)

def test_org_scan_reveals_its_target_without_any_scan_pod():
    """The whole point: no pod is dedicated to scanning here."""
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_food", storage_current=100.0)
    set_org_scan_bearing("U_P1", oid, "E2")
    end_of_turn()
    conn = get_connection()
    revealed = conn.execute(
        "SELECT id FROM sectors WHERE coord_x=2 AND coord_y=0 AND coord_z=0").fetchone()
    conn.close()
    assert revealed is not None

def test_org_scan_costs_food_like_a_scan_pod():
    """An idle pod stocked with food isolates the scan's own cost: idle has no
    recipe, so any food drawn this turn is org upkeep plus the innate scan."""
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    pod = seed_pod(oid, task="idle")
    conn = get_connection()
    conn.execute("UPDATE pods SET food_stored=100.0, energy_stored=100.0 WHERE id=?", (pod,))
    conn.commit(); conn.close()

    conn = get_connection()
    baseline_before = conn.execute("SELECT SUM(food_stored) s FROM pods WHERE org_id=?",
                                   (oid,)).fetchone()["s"]
    conn.close()
    end_of_turn()                       # upkeep only, no scan target set yet
    conn = get_connection()
    upkeep_only = baseline_before - conn.execute(
        "SELECT SUM(food_stored) s FROM pods WHERE org_id=?", (oid,)).fetchone()["s"]
    conn.close()

    set_org_scan_bearing("U_P1", oid, "E")
    conn = get_connection()
    before = conn.execute("SELECT SUM(food_stored) s FROM pods WHERE org_id=?", (oid,)).fetchone()["s"]
    conn.close()
    end_of_turn()
    conn = get_connection()
    after = conn.execute("SELECT SUM(food_stored) s FROM pods WHERE org_id=?", (oid,)).fetchone()["s"]
    conn.close()
    scan_turn_cost = before - after
    assert scan_turn_cost > upkeep_only          # the scan cost something on top of upkeep
    assert scan_turn_cost == upkeep_only + 1.0   # exactly one scan pod's food

def test_org_scan_rejects_an_out_of_range_aim_outright():
    """An offset's range is fixed, so an illegal aim can never become legal --
    reject it at set time instead of failing silently at resolution."""
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    result = set_org_scan_bearing("U_P1", oid, offset_x=SCAN_RANGE + 4, offset_y=0, offset_z=0)
    assert "error" in result and result["in_range"] is False
    conn = get_connection()
    row = conn.execute("SELECT scan_offset_x FROM organizations WHERE id=?", (oid,)).fetchone()
    conn.close()
    assert row["scan_offset_x"] is None      # nothing was written

def test_scan_aim_is_relative_and_survives_a_move():
    """The whole point of offsets: a pattern set once travels with the hull."""
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_food", storage_current=200.0)
    set_org_scan_bearing("U_P1", oid, "E")          # scans (1,0,0) from home
    end_of_turn()
    confirm_move("U_P1", oid, 5, 0, 0, jump_range_per_turn=5)
    end_of_turn(); end_of_turn()                    # arrives at (5,0,0)
    conn = get_connection()
    here = conn.execute("SELECT coord_x FROM sectors s JOIN organizations o ON o.sector_id=s.id "
                        "WHERE o.id=?", (oid,)).fetchone()
    revealed = conn.execute("SELECT id FROM sectors WHERE coord_x=6 AND coord_y=0").fetchone()
    conn.close()
    assert here["coord_x"] == 5
    assert revealed is not None                     # now scanning (6,0,0), no re-aiming

def test_org_scan_target_persists_across_turns():
    """Holding a target is a legitimate way to keep a sector from blinking out."""
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_food", storage_current=100.0)
    set_org_scan_bearing("U_P1", oid, "E")
    end_of_turn(); end_of_turn()
    conn = get_connection()
    row = conn.execute("SELECT scan_offset_x FROM organizations WHERE id=?", (oid,)).fetchone()
    conn.close()
    assert row["scan_offset_x"] == 1

def test_set_org_scan_target_clears_when_given_no_coordinates():
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    set_org_scan_bearing("U_P1", oid, "E")
    result = set_org_scan_bearing("U_P1", oid)
    assert result["cleared"] is True
    conn = get_connection()
    row = conn.execute("SELECT scan_offset_x FROM organizations WHERE id=?", (oid,)).fetchone()
    conn.close()
    assert row["scan_offset_x"] is None
