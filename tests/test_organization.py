import json
import pytest
from db.connection import get_connection
from engine.turn import end_of_turn
from xsettlers_mcp.tools.organization_tools import (
    set_mission, set_pod_task, show_civilization_status, show_game_status,
    rename_organization,
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

def test_set_pod_task_scan_stores_target_coords():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    result = set_pod_task("U_P1", pod, "scan", target_x=5, target_y=5, target_z=0)
    assert result["target_x"] == 5 and result["target_y"] == 5 and result["target_z"] == 0
    assert result["in_range"] is False  # distance sqrt(50) >> scan range 1
    conn = get_connection()
    params = json.loads(conn.execute("SELECT task_params FROM pods WHERE id=?",
                                     (pod,)).fetchone()["task_params"])
    conn.close()
    assert params == {"target_x": 5, "target_y": 5, "target_z": 0, "in_range": False}

@pytest.mark.parametrize("origin,target,expected_distance,expected_in_range", [
    ((0,0,0), (1,0,0), 1.0, True),
    ((3,3,0), (5,3,0), 2.0, False),
])
def test_set_pod_task_scan_target_status(origin, target, expected_distance, expected_in_range):
    """The response should remind the player where they are and how far the
    target is, not just a bare in_range boolean -- one call gives the full
    picture: current position, target, distance, scan range, legality.
    In-range and out-of-range are two cases of the same behavior, not two
    independently-failing things (merged per the 2026-07-29 test-suite audit)."""
    pid = seed_player(); sid = seed_sector(*origin); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    result = set_pod_task("U_P1", pod, "scan",
                             target_x=target[0], target_y=target[1], target_z=target[2])
    assert (result["current_x"], result["current_y"], result["current_z"]) == origin
    assert result["distance"] == expected_distance
    assert result["scan_range"] == 1
    assert result["in_range"] is expected_in_range

def test_scan_target_status_none_while_in_transit():
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid)
    confirm_move("U_P1", sid, 3, 0, 0)
    result = set_pod_task("U_P1", pod, "scan", target_x=1, target_y=0, target_z=0)
    assert result["current_x"] is None and result["distance"] is None and result["in_range"] is None

def test_set_pod_scan_target_reports_in_range():
    from xsettlers_mcp.tools.organization_tools import set_pod_scan_target
    pid = seed_player(); sid = seed_sector(0,0,0); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    set_pod_task("U_P1", pod, "scan")
    result = set_pod_scan_target("U_P1", pod, 5, 5, 0)
    assert result["in_range"] is False
    result = set_pod_scan_target("U_P1", pod, 1, 0, 0)
    assert result["in_range"] is True

def test_set_pod_task_scan_partial_target_rejected():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    assert "error" in set_pod_task("U_P1", pod, "scan", target_x=5, target_y=5)

# --- show_civilization_status: player-scoped fleet report (roster + aggregates) ---

def test_show_civilization_status_org_fields_tasking_storage_production():
    """tasking, storage, and production all come out of the same
    show_civilization_status() query for a single org -- one call exercises
    all three together rather than one test per field, since there's no
    independent failure mode where they'd need separate tests (they were
    split into 3 tests originally; consolidated per the 2026-07-29 test-
    suite audit)."""
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    seed_pod(oid, task="produce_food", storage_current=20.0)
    seed_pod(oid, task="produce_goods", storage_current=5.0)
    status = show_civilization_status("U_P1")
    org = next(o for o in status["organizations"] if o["id"] == oid)
    assert org["tasking"] == {"produce_energy": 2, "produce_food": 1, "produce_goods": 1}
    assert org["storage"] == {"energy": 20.0, "food": 20.0, "goods": 5.0}
    assert org["storage_summary"] == "E:20, F:20, G:5"
    assert org["production"] == {"energy": 20.0, "food": 10.0, "goods": 5.0}
    assert org["production_summary"] == "E:20, F:10, G:5"
    assert "storage_summary" in status["display"]["columns"]

def test_show_civilization_status_production_zeroes_energy_in_transit():
    """A ship in transit is parked at the sentinel sector (0 energy_capacity),
    so its energy production reads 0 regardless of tasking -- food/goods
    aren't sector-sourced and are unaffected (matches engine/turn.py's own
    sector-capacity cap, see _org_production)."""
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    seed_pod(oid, task="produce_food", storage_current=20.0)
    confirm_move("U_P1", oid, 3, 0, 0)
    status = show_civilization_status("U_P1")
    org = next(o for o in status["organizations"] if o["id"] == oid)
    assert org["production"] == {"energy": 0.0, "food": 10.0}

def test_show_civilization_status_assets_include_capacity_and_percent_full():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_capacity=100.0, storage_current=50.0)
    seed_pod(oid, task="produce_food", storage_capacity=100.0, storage_current=50.0)
    status = show_civilization_status("U_P1")
    assert status["assets"]["capacity"] == 200.0
    assert status["assets"]["total"] == 100.0
    assert status["assets"]["percent_full"] == 50.0

def test_show_civilization_status_display_hints():
    """Precomputed presentation fields (short_name, status, tasking_summary)
    and a top-level display block, so a client with no LLM in the loop can
    render a table without building its own formatting logic."""
    pid = seed_player(); sid = seed_sector(3,3,0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    seed_pod(oid, task="produce_food", storage_current=20.0)
    status = show_civilization_status("U_P1")
    org = next(o for o in status["organizations"] if o["id"] == oid)
    assert org["short_name"] == org["name"].replace("Ship-", "")
    assert org["status"] == "at (3,3,0)"
    assert org["tasking_summary"] == "E:1, F:1"
    assert status["display"]["resource_abbrev"] == {"energy": "E", "food": "F", "goods": "G"}
    assert "short_name" in status["display"]["columns"]

def test_show_civilization_status_in_transit_status_string():
    """The display `status` string is deliberately minimal -- just "in
    transit", no destination or ETA (a player asked for this view to be
    that terse) -- but dest_sector/turns_remaining are still real raw
    fields for a client that wants to build a richer status itself."""
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); sid = seed_sector(0,0,0); oid = seed_ship(pid, sid)
    confirm_move("U_P1", oid, 3, 0, 0)  # at turn 0, turns_needed=3, arrival_turn=3
    status = show_civilization_status("U_P1")
    org = next(o for o in status["organizations"] if o["id"] == oid)
    assert org["turns_remaining"] == 3
    assert org["dest_sector"]["coords"] == [3, 0, 0]
    assert org["status"] == "in transit"

def test_show_civilization_status_turns_remaining_zero_when_arrival_due_this_turn():
    """When arrival_turn == the current turn, the ship hasn't landed yet --
    it resolves when this turn is ended, not before -- turns_remaining==0
    captures that unambiguously even though the display string no longer
    spells it out."""
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    from engine.turn import end_of_turn
    pid = seed_player(); sid = seed_sector(0,0,0); oid = seed_ship(pid, sid)
    confirm_move("U_P1", oid, 1, 0, 0, jump_range_per_turn=1)  # arrival_turn=1
    end_of_turn()  # advances current_turn from 0 to 1 -- arrival not yet due at top of this call
    status = show_civilization_status("U_P1")
    org = next(o for o in status["organizations"] if o["id"] == oid)
    assert status["turn"] == 1
    assert status["next_tick_at"] is None  # clock never ran in this test -- see engine.turn.get_next_tick_at
    assert org["turns_remaining"] == 0
    assert org["status"] == "in transit"

# --- show_organization: locked MVP cargo-table display hints ---

def test_show_organization_display_hints_cargo_table():
    pid = seed_player(); sid = seed_sector(3,3,0); oid = seed_ship(pid, sid, name="Ship-P1-05")
    seed_pod(oid, task="produce_energy", storage_capacity=100.0, storage_current=100.0)
    seed_pod(oid, task="produce_energy", storage_capacity=100.0, storage_current=77.0)
    result = show_organization("U_P1", oid)
    assert result["status"] == "at (3,3,0)"
    assert result["display"]["header"] == "Ship-P1-05 — at (3,3,0), idle"
    assert result["display"]["columns"] == [
        "task_display", "count", "energy", "food", "goods", "capacity_display"]
    task = next(t for t in result["tasks"] if t["task"] == "produce_energy")
    assert task["task_display"] == "Energy"
    assert task["capacity_display"] == "177/200"

def test_show_organization_status_in_transit():
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); sid = seed_sector(0,0,0); oid = seed_ship(pid, sid)
    confirm_move("U_P1", oid, 3, 0, 0)
    result = show_organization("U_P1", oid)
    assert result["status"] == "in transit"

# --- show_game_status: public scoreboard (all players, aggregate totals only) ---

def test_show_game_status_returns_all_players_standings():
    p1 = seed_player(email="p1@t.com", player_token="U_P1")
    p2 = seed_player(email="p2@t.com", player_token="U_P2")
    sid = seed_sector()
    o1 = seed_ship(p1, sid, name="P1 Ship")
    o2 = seed_ship(p2, sid, name="P2 Ship")
    seed_pod(o1, task="produce_food", storage_capacity=100.0, storage_current=80.0)
    seed_pod(o2, task="produce_food", storage_capacity=100.0, storage_current=20.0)
    status = show_game_status("U_P1")
    assert status["next_tick_at"] is None  # clock never ran in this test -- see engine.turn.get_next_tick_at
    by_player = {s["player_id"]: s for s in status["standings"]}
    assert by_player[p1]["total"] == 80.0
    assert by_player[p2]["total"] == 20.0
    # ordered by score descending (equivalent to total here since both hold
    # only food) -- the scoreboard, highest first
    assert status["standings"][0]["player_id"] == p1
    assert status["standings"][0]["rank"] == 1
    assert status["standings"][1]["rank"] == 2
    assert status["display"]["resource_abbrev"] == {"energy": "E", "food": "F", "goods": "G"}

def test_show_game_status_ranks_by_weighted_score_not_raw_total():
    """`rank`/ordering follows the score_weights-weighted score
    (config/game_config.yaml: energy=0, food=1, goods=2 as of 2026-07-30),
    not the raw total -- a player with a lower raw total but a
    higher-scoring resource mix should still rank first, proving `score`
    is a real computed field driving order, not just an alias for `total`."""
    p1 = seed_player(email="p1@t.com", player_token="U_P1")
    p2 = seed_player(email="p2@t.com", player_token="U_P2")
    sid = seed_sector()
    o1 = seed_ship(p1, sid, name="P1 Ship")
    o2 = seed_ship(p2, sid, name="P2 Ship")
    seed_pod(o1, task="produce_energy", storage_capacity=100.0, storage_current=80.0)  # weighted 0
    seed_pod(o2, task="produce_food", storage_capacity=100.0, storage_current=10.0)
    seed_pod(o2, task="produce_goods", storage_capacity=100.0, storage_current=10.0)
    status = show_game_status("U_P1")
    by_player = {s["player_id"]: s for s in status["standings"]}
    assert by_player[p1]["total"] == 80.0
    assert by_player[p2]["total"] == 20.0
    assert by_player[p1]["score"] == 0.0     # 80 energy * weight 0
    assert by_player[p2]["score"] == 30.0    # 10 food * weight 1 + 10 goods * weight 2
    assert status["standings"][0]["player_id"] == p2   # lower total, higher score -- still ranks first
    assert status["standings"][0]["rank"] == 1
    assert status["standings"][1]["player_id"] == p1
    assert status["standings"][1]["rank"] == 2

def test_show_game_status_does_not_leak_fleet_detail():
    pid = seed_player(); sid = seed_sector(); seed_ship(pid, sid)
    status = show_game_status("U_P1")
    assert "organizations" not in status["standings"][0]
    assert "tasking" not in status["standings"][0]

def test_show_game_status_rejects_unknown_player():
    assert "error" in show_game_status("U_NOBODY")

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
