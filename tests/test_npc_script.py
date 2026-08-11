import json
from db.connection import get_connection
from db.npc_profiles import assign_npc_profile
from engine.npc import run_npc_decisions, strategy_names
from engine.npc_programs import load_programs
from engine.npc_script import validate_program, _select, _params_for
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod

def _seed_fleet(player_id, sector_id, n=8):
    return [seed_ship(player_id, sector_id, name=f"Ship-{i}") for i in range(n)]

def _orgs(player_id):
    conn = get_connection()
    rows = {r["id"]: dict(r) for r in conn.execute(
        """SELECT id, sector_id, mission, scan_offset_x, scan_offset_y, scan_offset_z
           FROM organizations WHERE player_id=?""", (player_id,)).fetchall()}
    conn.close(); return rows

def _arrivals():
    conn = get_connection()
    rows = {r["org_id"]: (r["dest_x"], r["dest_y"], r["dest_z"]) for r in conn.execute(
        "SELECT org_id, dest_x, dest_y, dest_z FROM arrival_queue").fetchall()}
    conn.close(); return rows

def _memory(player_id):
    conn = get_connection()
    row = conn.execute("SELECT memory FROM npc_profiles WHERE player_id=?", (player_id,)).fetchone()
    conn.close(); return json.loads(row["memory"])


# --- ship selectors ----------------------------------------------------------

def test_select_all_is_the_whole_fleet():
    assert _select("all", [10, 11, 12]) == [10, 11, 12]
    assert _select(None, [10, 11, 12]) == [10, 11, 12]

def test_select_slice_is_clamped_to_the_fleet_that_exists():
    """A program authored for 8 ships must not blow up on a 3-ship fleet."""
    assert _select({"slice": [0, 8]}, [10, 11, 12]) == [10, 11, 12]
    assert _select({"slice": [2, 8]}, [10, 11, 12]) == [12]
    assert _select({"slice": [5, 8]}, [10, 11, 12]) == []

def test_select_stride_and_offset_pick_every_nth_ship():
    ships = [10, 11, 12, 13, 14, 15, 16, 17]
    # The "mover" of each pair -- what fan_out_consolidate's second leg needs.
    assert _select({"slice": [0, 8], "stride": 2, "offset": 1}, ships) == [11, 13, 15, 17]
    assert _select({"slice": [0, 8], "stride": 2}, ships) == [10, 12, 14, 16]


# --- params cycling ----------------------------------------------------------

def test_params_cycle_round_robin_across_ships():
    step = {"params": [{"d_x": 1}, {"d_x": 2}, {"d_x": 3}]}
    assert [_params_for(step, i)["d_x"] for i in range(7)] == [1, 2, 3, 1, 2, 3, 1]

def test_repeat_each_gives_consecutive_ships_the_same_params():
    step = {"params": [{"d_x": 1}, {"d_x": 2}], "repeat_each": 2}
    assert [_params_for(step, i)["d_x"] for i in range(6)] == [1, 1, 2, 2, 1, 1]

def test_a_single_params_mapping_applies_to_every_ship():
    step = {"params": {"d_x": 9}}
    assert [_params_for(step, i)["d_x"] for i in range(3)] == [9, 9, 9]


# --- validate_program --------------------------------------------------------
# Validation runs at assign time, not fire time: a program is authored by a
# person (eventually in a builder), so an error has to reach them while they
# are still holding it.

def test_valid_program_passes():
    assert validate_program([{"ships": "all", "when": "now", "action": "move",
                              "params": {"d_x": 1, "d_y": 0, "d_z": 0}}]) is None

def test_empty_program_is_valid():
    assert validate_program([]) is None
    assert validate_program(None) is None

def test_program_must_be_a_list():
    assert "must be a list" in validate_program({"ships": "all"})["error"]

def test_unknown_action_is_rejected():
    err = validate_program([{"action": "self_destruct"}])
    assert "invalid action" in err["error"]

def test_unknown_step_key_is_rejected():
    """A typo'd key would otherwise be silently ignored and the step would
    quietly do something other than what was written."""
    err = validate_program([{"action": "colonize", "shipz": "all"}])
    assert "unknown keys" in err["error"] and "shipz" in err["error"]

def test_at_turn_without_a_turn_number_is_rejected():
    err = validate_program([{"action": "colonize", "when": "at_turn"}])
    assert "at_turn" in err["error"]

def test_arrival_relative_step_without_a_preceding_move_is_rejected():
    """before_arrival/after_arrival anchor to a move already under way, so a
    program that opens with one has nothing to attach to and would be refused
    ship by ship at run time. Caught while the author can still reorder."""
    err = validate_program([{"action": "move", "when": "after_arrival",
                             "params": {"d_x": 1, "d_y": 0, "d_z": 0}}])
    assert "earlier step must have sent these ships moving" in err["error"]

def test_arrival_relative_step_is_accepted_after_a_now_move():
    assert validate_program([
        {"action": "move", "when": "now", "params": {"d_x": 1, "d_y": 0, "d_z": 0}},
        {"action": "move", "when": "after_arrival", "params": {"d_x": 1, "d_y": 0, "d_z": 0}},
    ]) is None

def test_during_transit_only_accepts_set_pod_task():
    err = validate_program([{"action": "move", "when": "during_transit",
                             "params": {"d_x": 1, "d_y": 0, "d_z": 0}}])
    assert "during_transit" in err["error"]

def test_move_without_a_destination_is_rejected():
    assert "needs a destination" in validate_program([{"action": "move"}])["error"]

def test_move_mixing_absolute_and_relative_is_rejected():
    err = validate_program([{"action": "move",
                             "params": {"dest_x": 1, "dest_y": 1, "dest_z": 1, "d_x": 1}}])
    assert "not both" in err["error"]

def test_move_with_incomplete_coordinates_is_rejected():
    err = validate_program([{"action": "move", "params": {"d_x": 1, "d_y": 2}}])
    assert "all three coordinates" in err["error"]

def test_empty_params_list_is_rejected():
    err = validate_program([{"action": "colonize", "params": []}])
    assert "nothing to" in err["error"]

def test_bad_slice_is_rejected():
    err = validate_program([{"action": "colonize", "ships": {"slice": [5, 2]}}])
    assert "empty or negative" in err["error"]

def test_bad_pod_task_is_rejected():
    err = validate_program([{"action": "set_pod_task", "params": {"task": "sing"}}])
    assert "invalid pod task" in err["error"]

def test_assign_npc_profile_refuses_an_invalid_inline_program():
    """The whole point of validating at assign time -- nothing is written."""
    pid = seed_player()
    result = assign_npc_profile(pid, "scripted",
                                config={"program": [{"action": "nope"}]})
    assert "error" in result
    conn = get_connection()
    profile = conn.execute("SELECT player_id FROM npc_profiles WHERE player_id=?",
                           (pid,)).fetchone()
    npc_flag = conn.execute("SELECT is_npc FROM players WHERE id=?", (pid,)).fetchone()
    conn.close()
    assert profile is None, "no profile row written"
    assert npc_flag["is_npc"] == 0, "the player was not flagged either"


# --- the library -------------------------------------------------------------

def test_every_shipped_program_is_valid():
    """The named programs are not validated on each assign, so they are
    validated here instead -- a malformed one would otherwise only surface as
    a fleet that quietly does nothing."""
    programs = load_programs()
    assert {"turtle", "fan_out_consolidate", "burst_and_colonize"} <= set(programs)
    for name, program in programs.items():
        assert validate_program(program) is None, f"{name} is malformed"

def test_strategy_names_is_the_union_of_code_and_programs():
    names = strategy_names()
    assert "fan_out" in names and "frontier_map_stay_frosty" in names  # code
    assert "turtle" in names and "burst_and_colonize" in names          # programs
    assert "scripted" in names                                          # inline programs


# --- running an inline program ----------------------------------------------

def test_inline_program_runs_through_the_scripted_strategy():
    """The path an NPC builder will use: a program in the profile's own config,
    under no library name at all."""
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ships = _seed_fleet(pid, sid, 4)
    assign_npc_profile(pid, "scripted", config={"program": [
        {"ships": {"slice": [0, 2]}, "when": "now", "action": "move",
         "params": [{"d_x": 2, "d_y": 0, "d_z": 0}, {"d_x": -2, "d_y": 0, "d_z": 0}]},
        {"ships": {"slice": [2, 4]}, "when": "now", "action": "aim_scan",
         "params": {"bearing": "N2"}},
    ]})
    run_npc_decisions()

    arrivals = _arrivals()
    assert arrivals[ships[0]] == (27, 25, 0)
    assert arrivals[ships[1]] == (23, 25, 0)
    assert ships[2] not in arrivals and ships[3] not in arrivals

    orgs = _orgs(pid)
    for ship in ships[2:]:
        # North is -y (see sector_tools.SCAN_BEARINGS); "N2" reaches 2 sectors.
        assert (orgs[ship]["scan_offset_x"], orgs[ship]["scan_offset_y"]) == (0, -2)

def test_a_program_runs_only_once():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    _seed_fleet(pid, sid, 2)
    assign_npc_profile(pid, "scripted", config={"program": [
        {"ships": "all", "when": "now", "action": "move",
         "params": {"d_x": 1, "d_y": 0, "d_z": 0}}]})
    run_npc_decisions()
    first = _memory(pid)
    run_npc_decisions()
    assert _memory(pid) == first

def test_per_ship_failures_are_recorded_rather_than_abandoning_the_fleet():
    """One ship's order failing must not cost the rest of the fleet its
    opening -- and the profile row is the only place a record of it survives
    the tick."""
    pid = seed_player()
    sid = seed_sector(0, 0, 0)
    ships = _seed_fleet(pid, sid, 2)
    assign_npc_profile(pid, "scripted", config={"program": [
        # Ship 0 is sent off the edge of the map; ship 1's order is fine.
        {"ships": "all", "when": "now", "action": "move",
         "params": [{"d_x": -5, "d_y": 0, "d_z": 0}, {"d_x": 4, "d_y": 0, "d_z": 0}]}]})
    run_npc_decisions()

    arrivals = _arrivals()
    assert ships[0] not in arrivals
    assert arrivals[ships[1]] == (4, 0, 0), "the rest of the fleet still went"
    errors = _memory(pid)["errors"]
    assert len(errors) == 1 and errors[0]["org_id"] == ships[0]
    assert "negative indices" in errors[0]["error"]

def test_set_pod_task_addresses_pods_by_index_within_the_org():
    """A program cannot name pod ids -- they differ per ship and per game."""
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship = seed_ship(pid, sid)
    first = seed_pod(ship, task="idle")
    second = seed_pod(ship, task="idle")
    assign_npc_profile(pid, "scripted", config={"program": [
        {"ships": "all", "when": "now", "action": "set_pod_task",
         "params": {"pod_index": 1, "task": "produce_goods"}}]})
    run_npc_decisions()
    conn = get_connection()
    tasks = {r["id"]: r["task"] for r in conn.execute(
        "SELECT id, task FROM pods WHERE org_id=?", (ship,)).fetchall()}
    conn.close()
    assert tasks[first] == "idle"
    assert tasks[second] == "produce_goods"
