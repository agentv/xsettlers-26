"""
Strategy documents: selection, validation, and the decide hook that lets a
document react to what its scans found.

Subject-shaped, not module-shaped: selectors, the validator and the
interpreter are all "what a strategy document means", and only testing them
together proves the vocabulary a document may use is the same one the
validator accepts and the interpreter implements.
"""
import json
import pytest

from db.connection import connection, get_connection
from npc import decide
from npc.library import load_strategies, strategy_names
from npc.profiles import assign_npc_profile
from npc.strategies import run_npc_decisions
from npc.strategy import (validate_strategy, select, params_for, resolve_params,
                          run_strategy)
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod


def _fleet(n=8, start_id=1):
    """A fleet snapshot shaped the way select() takes one."""
    return [{"id": start_id + i, "index": i, "sector_id": 10, "mission": "idle"}
            for i in range(n)]


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
    assert [s["id"] for s in select("all", _fleet())] == list(range(1, 9))
    assert [s["id"] for s in select(None, _fleet())] == list(range(1, 9))


def test_select_slice_is_clamped_to_the_fleet_that_exists():
    chosen = select({"slice": [2, 99]}, _fleet())
    assert [s["id"] for s in chosen] == [3, 4, 5, 6, 7, 8]


def test_select_stride_and_offset_pick_every_nth_ship():
    chosen = select({"slice": [0, 8], "stride": 2, "offset": 1}, _fleet())
    assert [s["id"] for s in chosen] == [2, 4, 6, 8]


def test_select_idle_skips_ships_in_transit_or_on_a_mission():
    fleet = _fleet()
    fleet[0]["sector_id"] = -1              # in transit
    fleet[1]["mission"] = "colonize"        # mid-colonization
    assert [s["id"] for s in select("idle", fleet)] == [3, 4, 5, 6, 7, 8]


# --- params cycling ----------------------------------------------------------

def test_params_cycle_round_robin_across_ships():
    order = {"params": [{"d_x": 1}, {"d_x": 2}]}
    assert [params_for(order, i)["d_x"] for i in range(4)] == [1, 2, 1, 2]


def test_repeat_each_gives_consecutive_ships_the_same_params():
    order = {"params": [{"d_x": 1}, {"d_x": 2}], "repeat_each": 2}
    assert [params_for(order, i)["d_x"] for i in range(4)] == [1, 1, 2, 2]


def test_a_single_params_mapping_applies_to_every_ship():
    order = {"params": {"d_x": 7}}
    assert [params_for(order, i)["d_x"] for i in range(3)] == [7, 7, 7]


def test_params_are_keyed_on_fleet_index_not_position_in_the_selection():
    """The reason a ship keeps its heading under `ships: idle`: cycling on the
    ship's own fleet index means a ship is handed the same params whether or
    not its neighbours happened to be busy this turn."""
    order = {"params": [{"bearing": "N"}, {"bearing": "S"},
                        {"bearing": "E"}, {"bearing": "W"}]}
    fleet = _fleet()
    fleet[0]["mission"] = "move"
    fleet[1]["mission"] = "move"
    # Ships 2 and 3 are now first and second in the selection, but must still
    # get their own directions (E, W) rather than the first two (N, S).
    chosen = select("idle", fleet)
    assert [params_for(order, s["index"])["bearing"] for s in chosen[:2]] == ["E", "W"]


# --- substitution ------------------------------------------------------------

def test_a_binding_expands_into_destination_coordinates():
    bindings = {"target": {"x": 4, "y": 5, "z": 0, "energy_capacity": 90}}
    resolved = resolve_params({"dest": "$target"}, bindings, {})
    assert resolved == {"dest_x": 4, "dest_y": 5, "dest_z": 0}


def test_a_reference_falls_back_to_the_documents_config():
    resolved = resolve_params({"jump_range_per_turn": "$jump"}, {}, {"jump": 3})
    assert resolved["jump_range_per_turn"] == 3


def test_a_binding_beats_config_of_the_same_name():
    resolved = resolve_params({"v": "$n"}, {"n": "bound"}, {"n": "config"})
    assert resolved["v"] == "bound"


def test_literals_pass_through_untouched():
    assert resolve_params({"d_x": 0, "bearing": "N2"}, {}, {}) == {"d_x": 0, "bearing": "N2"}


# --- validation --------------------------------------------------------------

def test_a_valid_document_passes():
    assert validate_strategy({"steps": [
        {"order": {"ships": "all", "action": "move", "params": {"d_x": 1, "d_y": 0, "d_z": 0}}}]}) is None


def test_an_empty_document_is_valid():
    assert validate_strategy({"steps": []}) is None
    assert validate_strategy(None) is None


def test_a_document_must_be_a_mapping():
    assert "must be a mapping" in validate_strategy([{"order": {}}])["error"]


def test_unknown_document_key_is_rejected():
    err = validate_strategy({"steps": [], "loopy": True})
    assert "unknown document keys" in err["error"]


def test_a_step_must_be_exactly_one_of_order_or_decide():
    err = validate_strategy({"steps": [{"order": {"action": "colonize"},
                                        "decide": {}}]})
    assert "exactly one of" in err["error"]
    assert "exactly one of" in validate_strategy({"steps": [{}]})["error"]


def test_unknown_action_is_rejected():
    err = validate_strategy({"steps": [{"order": {"action": "self_destruct"}}]})
    assert "invalid action" in err["error"]


def test_unknown_order_key_is_rejected():
    err = validate_strategy({"steps": [{"order": {"action": "colonize", "shipz": "all"}}]})
    assert "unknown order keys" in err["error"]


def test_at_turn_without_a_turn_number_is_rejected():
    err = validate_strategy({"steps": [{"order": {"action": "colonize", "when": "at_turn"}}]})
    assert "needs a turn number" in err["error"]


def test_arrival_relative_step_without_a_preceding_move_is_rejected():
    err = validate_strategy({"steps": [{"order": {
        "action": "move", "when": "upon_arrival",
        "params": {"d_x": 0, "d_y": 1, "d_z": 0}}}]})
    assert "a move in progress" in err["error"]


def test_arrival_relative_step_is_accepted_after_a_now_move():
    assert validate_strategy({"steps": [
        {"order": {"action": "move", "params": {"d_x": 0, "d_y": 1, "d_z": 0}}},
        {"order": {"action": "move", "when": "upon_arrival",
                   "params": {"d_x": 0, "d_y": 1, "d_z": 0}}}]}) is None


def test_upon_departure_only_accepts_set_pod_task():
    err = validate_strategy({"steps": [{"order": {
        "action": "move", "when": "upon_departure",
        "params": {"d_x": 0, "d_y": 1, "d_z": 0}}}]})
    assert "only supports set_pod_task" in err["error"]


def test_move_without_a_destination_is_rejected():
    assert "needs a destination" in validate_strategy(
        {"steps": [{"order": {"action": "move"}}]})["error"]


def test_bad_slice_is_rejected():
    err = validate_strategy({"steps": [{"order": {"action": "colonize",
                                                  "ships": {"slice": [5, 2]}}}]})
    assert "empty or negative" in err["error"]


def test_idle_is_a_valid_selector():
    assert validate_strategy({"steps": [{"order": {"action": "colonize",
                                                   "ships": "idle"}}]}) is None


def test_bad_pod_task_is_rejected():
    err = validate_strategy({"steps": [{"order": {"action": "set_pod_task",
                                                  "params": {"task": "sing"}}}]})
    assert "invalid pod task" in err["error"]


def test_empty_params_list_is_rejected():
    err = validate_strategy({"steps": [{"order": {"action": "colonize", "params": []}}]})
    assert "nothing to" in err["error"]


# --- decide validation -------------------------------------------------------

def test_unknown_gate_is_rejected():
    err = validate_strategy({"steps": [{"decide": {
        "await": "when_i_feel_like_it", "from": "scan_targets",
        "rank_by": "energy_capacity", "pick": "max", "bind": "t"}}]})
    assert "unknown gate" in err["error"]


def test_unknown_source_is_rejected():
    err = validate_strategy({"steps": [{"decide": {
        "from": "every_sector_on_the_map", "rank_by": "energy_capacity",
        "pick": "max", "bind": "t"}}]})
    assert "unknown source" in err["error"]


def test_unknown_rank_field_is_rejected():
    err = validate_strategy({"steps": [{"decide": {
        "from": "scan_targets", "rank_by": "vibes", "pick": "max", "bind": "t"}}]})
    assert "unknown rank_by" in err["error"]


def test_decide_requires_a_binding_name():
    err = validate_strategy({"steps": [{"decide": {
        "from": "scan_targets", "rank_by": "energy_capacity", "pick": "max"}}]})
    assert "requires 'bind'" in err["error"]


def test_a_reference_nothing_binds_is_rejected():
    """Caught while the author is still holding the document, not three turns
    later inside a clock tick."""
    err = validate_strategy({"steps": [
        {"order": {"action": "move", "params": {"dest": "$nowhere"}}}]})
    assert "$nowhere" in err["error"] and "not bound" in err["error"]


def test_a_reference_bound_by_an_earlier_decide_is_accepted():
    assert validate_strategy({"steps": [
        {"order": {"action": "move", "params": {"d_x": 0, "d_y": 1, "d_z": 0}}},
        {"decide": {"await": "all_scans_resolved", "from": "scan_targets",
                    "rank_by": "energy_capacity", "pick": "max", "bind": "target"}},
        {"order": {"action": "move", "params": {"dest": "$target"}}}]}) is None


def test_a_forward_reference_is_rejected():
    """Order matters: a step cannot use a binding a later step produces."""
    err = validate_strategy({"steps": [
        {"order": {"action": "move", "params": {"dest": "$target"}}},
        {"decide": {"from": "scan_targets", "rank_by": "energy_capacity",
                    "pick": "max", "bind": "target"}}]})
    assert "$target" in err["error"]


# --- the shipped library -----------------------------------------------------

def test_every_shipped_strategy_is_valid():
    strategies = load_strategies()
    assert strategies, "no strategies found in config/npc_strategies/"
    for name, document in strategies.items():
        assert validate_strategy(document) is None, f"{name} is malformed"


def test_strategy_names_is_the_library():
    assert "fan_out" in strategy_names()
    assert "turtle" in strategy_names()


def test_assigning_an_unknown_strategy_is_refused():
    player_id = seed_player("npc@test", "NPC")
    err = assign_npc_profile(player_id, "no_such_strategy")
    assert "Unknown strategy" in err["error"]


# --- fog of war --------------------------------------------------------------

def test_a_decision_cannot_see_a_sector_the_player_has_not_scanned():
    """
    The structural guarantee: a document names a source, and every source
    requires a player_sectors row at confidence > 0. A rich sector the player
    has never seen is simply not a candidate -- there is no way to write a
    document that reaches it.
    """
    player_id = seed_player("fog@test", "Fogged")
    home = seed_sector(0, 0, 0, energy=50)
    rich = seed_sector(0, -2, 0, energy=999)
    ship = seed_ship(player_id, home, name="Scout")
    with connection() as conn:
        conn.execute("""UPDATE organizations SET scan_offset_x=0, scan_offset_y=-2,
                        scan_offset_z=0 WHERE id=?""", (ship,))

    # Aimed at the rich sector, but nothing has been revealed there yet.
    assert decide._scan_targets(player_id) == []
    assert decide._all_scans_resolved(player_id) is False

    # Once the scan resolves, the same sector becomes a candidate.
    with connection() as conn:
        conn.execute("INSERT INTO player_sectors (player_id, sector_id, confidence) VALUES (?,?,100)",
                     (player_id, rich))
    assert decide._all_scans_resolved(player_id) is True
    assert decide._scan_targets(player_id) == [
        {"x": 0, "y": -2, "z": 0, "energy_capacity": 999}]


def test_a_blinked_out_sector_stops_being_a_candidate():
    """confidence 0 means gone, not stale -- the same rule every player-facing
    read applies (db/sectors.py)."""
    player_id = seed_player("decayed@test", "Decayed")
    home = seed_sector(0, 0, 0, energy=50)
    seen = seed_sector(2, 0, 0, energy=400)
    ship = seed_ship(player_id, home, name="Scout")
    with connection() as conn:
        conn.execute("""UPDATE organizations SET scan_offset_x=2, scan_offset_y=0,
                        scan_offset_z=0 WHERE id=?""", (ship,))
        conn.execute("INSERT INTO player_sectors (player_id, sector_id, confidence) VALUES (?,?,0)",
                     (player_id, seen))
    assert decide._scan_targets(player_id) == []


def test_duplicate_findings_are_one_candidate():
    """Two scouts on the same bearing confirm one reading, not two."""
    player_id = seed_player("dup@test", "Dup")
    home = seed_sector(0, 0, 0, energy=50)
    found = seed_sector(0, -2, 0, energy=700)
    for i in range(2):
        ship = seed_ship(player_id, home, name=f"Scout-{i}")
        with connection() as conn:
            conn.execute("""UPDATE organizations SET scan_offset_x=0, scan_offset_y=-2,
                            scan_offset_z=0 WHERE id=?""", (ship,))
    with connection() as conn:
        conn.execute("INSERT INTO player_sectors (player_id, sector_id, confidence) VALUES (?,?,100)",
                     (player_id, found))
    assert len(decide._scan_targets(player_id)) == 1


# --- the interpreter ---------------------------------------------------------

def test_a_gate_that_has_not_opened_leaves_the_counter_where_it_is():
    """Waiting is not an error and is not a verb -- it is a decide step that
    did not pass, retried next turn."""
    player_id = seed_player("wait@test", "Waiter")
    home = seed_sector(0, 0, 0, energy=50)
    ship = seed_ship(player_id, home, name="Scout")
    with connection() as conn:
        conn.execute("""UPDATE organizations SET scan_offset_x=0, scan_offset_y=-2,
                        scan_offset_z=0 WHERE id=?""", (ship,))

    document = {"steps": [{"decide": {
        "await": "all_scans_resolved", "from": "scan_targets",
        "rank_by": "energy_capacity", "pick": "max", "bind": "target"}}]}
    memory = run_strategy(player_id, "tok", document, {})
    assert memory["pc"] == 0
    assert "waiting" in memory
    assert memory["bindings"] == {}


def test_a_terminal_document_stops_when_its_steps_run_out():
    player_id = seed_player("done@test", "Done")
    home = seed_sector(0, 0, 0, energy=50)
    seed_ship(player_id, home, name="Ship")
    document = {"steps": [{"order": {"ships": "all", "action": "colonize"}}]}
    memory = run_strategy(player_id, "tok", document, {})
    assert memory["pc"] == 1
    # A second pass does nothing further.
    assert run_strategy(player_id, "tok", document, memory)["pc"] == 1


def test_a_looping_document_rewinds_and_runs_one_pass_per_turn():
    player_id = seed_player("loop@test", "Looper")
    home = seed_sector(0, 0, 0, energy=50)
    seed_ship(player_id, home, name="Ship")
    document = {"loop": True,
                "steps": [{"order": {"ships": "all", "action": "colonize"}}]}
    memory = run_strategy(player_id, "tok", document, {})
    assert memory["pc"] == 0, "a looping document rewinds for the next turn"


def test_a_document_with_no_fleet_does_nothing():
    player_id = seed_player("empty@test", "Empty")
    document = {"steps": [{"order": {"ships": "all", "action": "colonize"}}]}
    assert run_strategy(player_id, "tok", document, {}) == {}


def test_run_npc_decisions_skips_a_profile_naming_a_missing_strategy():
    """A since-renamed strategy must not take the turn down for everyone,
    including the humans."""
    player_id = seed_player("gone@test", "Gone")
    assign_npc_profile(player_id, "turtle")
    with connection() as conn:
        conn.execute("UPDATE npc_profiles SET strategy_name='removed_last_week' WHERE player_id=?",
                     (player_id,))
    run_npc_decisions()   # must not raise
