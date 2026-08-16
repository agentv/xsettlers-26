"""
NPC play: the shipped strategy documents driven the way a real game drives
them -- assign a profile, then let run_npc_decisions()/end_of_turn() do
everything, with nothing calling a strategy by hand.

tests/test_strategy.py covers the document vocabulary itself (selectors,
validation, the decide hook). This file is about whether the strategies in
config/npc_strategies/ actually play.
"""
import json
from db.connection import get_connection
from npc.profiles import assign_npc_profile
from npc.strategies import run_npc_decisions
from engine.turn import end_of_turn
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod

def _seed_fleet(player_id, sector_id, n=8):
    return [seed_ship(player_id, sector_id, name=f"Ship-{i}") for i in range(n)]

def _memory(player_id):
    conn = get_connection()
    row = conn.execute("SELECT memory FROM npc_profiles WHERE player_id=?", (player_id,)).fetchone()
    conn.close()
    return json.loads(row["memory"])

def _orgs(player_id):
    conn = get_connection()
    rows = {r["id"]: dict(r) for r in conn.execute(
        """SELECT id, sector_id, mission, is_mobile,
                  scan_offset_x, scan_offset_y, scan_offset_z
           FROM organizations WHERE player_id=?""", (player_id,)).fetchall()}
    conn.close()
    return rows

# Aim is always the "X2" bearing (distance 2, SCAN_RANGE's max), decoupled
# from however far a strategy's legs happen to be: movement has no range cap,
# scanning does.
AIM = {"N": (0, -2, 0), "S": (0, 2, 0), "E": (2, 0, 0), "W": (-2, 0, 0)}
# The order the shipped documents cycle their cardinals in, by fleet index.
CYCLE = ["N", "S", "E", "W"]


# --- profile assignment ------------------------------------------------------

def test_assign_npc_profile_sets_flag_and_creates_row():
    pid = seed_player()
    result = assign_npc_profile(pid, "fan_out", config={"jump_range_per_turn": 2})
    assert result == {"ok": True, "player_id": pid, "strategy_name": "fan_out"}
    conn = get_connection()
    player = conn.execute("SELECT is_npc FROM players WHERE id=?", (pid,)).fetchone()
    profile = conn.execute("SELECT strategy_name, config, memory FROM npc_profiles WHERE player_id=?",
                           (pid,)).fetchone()
    conn.close()
    assert player["is_npc"] == 1
    assert profile["strategy_name"] == "fan_out"
    assert json.loads(profile["config"]) == {"jump_range_per_turn": 2}
    assert json.loads(profile["memory"]) == {}

def test_assign_npc_profile_reassignment_resets_memory():
    """Switching a player's strategy discards its old memory: the program
    counter and bindings point into a specific document, so carrying them over
    would start the new strategy partway through steps it never had."""
    pid = seed_player()
    assign_npc_profile(pid, "fan_out")
    conn = get_connection()
    conn.execute("UPDATE npc_profiles SET memory=? WHERE player_id=?",
                 (json.dumps({"pc": 3, "bindings": {"target": {"x": 1}}}), pid))
    conn.commit(); conn.close()
    assign_npc_profile(pid, "turtle")
    conn = get_connection()
    profile = conn.execute("SELECT strategy_name, memory FROM npc_profiles WHERE player_id=?",
                           (pid,)).fetchone()
    conn.close()
    assert profile["strategy_name"] == "turtle"
    assert json.loads(profile["memory"]) == {}

def test_assign_npc_profile_refuses_a_strategy_the_library_does_not_have():
    pid = seed_player()
    err = assign_npc_profile(pid, "does_not_exist")
    assert "Unknown strategy" in err["error"]
    conn = get_connection()
    row = conn.execute("SELECT COUNT(*) AS n FROM npc_profiles WHERE player_id=?", (pid,)).fetchone()
    conn.close()
    assert row["n"] == 0, "nothing is written when the reference does not resolve"

def test_run_npc_decisions_noop_for_non_npc_players():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    seed_ship(pid, sid)
    run_npc_decisions()  # no npc_profiles row for this player -- must not raise or act
    conn = get_connection()
    org = conn.execute("SELECT sector_id FROM organizations WHERE player_id=?", (pid,)).fetchone()
    conn.close()
    assert org["sector_id"] == sid


# --- turtle ------------------------------------------------------------------

def test_turtle_never_acts():
    """The control the whole taxonomy is measured against: an empty document
    is a complete strategy."""
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    _seed_fleet(pid, sid, 4)
    assign_npc_profile(pid, "turtle")
    for _ in range(3):
        run_npc_decisions()
    orgs = _orgs(pid)
    assert all(o["sector_id"] == sid and o["mission"] == "idle" for o in orgs.values())


# --- frontier_map_stay_frosty ------------------------------------------------

def test_frontier_moves_idle_ships_and_aims_scanner_ahead():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 4)
    assign_npc_profile(pid, "frontier_map_stay_frosty")
    run_npc_decisions()

    orgs = _orgs(pid)
    for index, oid in enumerate(ship_ids):
        org = orgs[oid]
        assert org["sector_id"] == -1 and org["mission"] == "move"
        bearing = CYCLE[index % len(CYCLE)]
        assert (org["scan_offset_x"], org["scan_offset_y"],
                org["scan_offset_z"]) == AIM[bearing]

def test_frontier_aims_the_same_ships_it_just_ordered_to_move():
    """The fleet is snapshotted once per turn, so the aim step still selects
    the ships the move step took out of the idle set moments earlier. Without
    that, `ships: idle` in a second step would match nothing."""
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 4)
    assign_npc_profile(pid, "frontier_map_stay_frosty")
    run_npc_decisions()
    orgs = _orgs(pid)
    assert all(orgs[o]["scan_offset_x"] is not None for o in ship_ids), \
        "every ship that was ordered to move also got its scanner aimed"

def test_frontier_redirects_after_landing():
    """No terminal state: `loop: true` rewinds the document every turn, so a
    ship that lands is picked up and sent out again. Not on the same call as
    landing -- run_npc_decisions is step 0, so it reads state as of the end of
    the previous end_of_turn()."""
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_id = _seed_fleet(pid, sid, 1)[0]
    assign_npc_profile(pid, "frontier_map_stay_frosty")

    def _org():
        conn = get_connection()
        row = conn.execute("SELECT sector_id, mission FROM organizations WHERE id=?",
                           (ship_id,)).fetchone()
        conn.close()
        return row

    end_of_turn()                        # opening dispatch
    assert _org()["sector_id"] == -1

    for _ in range(10):                  # travel until it lands
        if _org()["sector_id"] != -1:
            break
        end_of_turn()
    landed = _org()
    assert landed["sector_id"] != -1 and landed["mission"] == "idle"

    end_of_turn()                        # step 0 now sees the landed ship
    assert _org()["sector_id"] == -1, "a landed ship is sent straight back out"


# --- fan_out (the adaptive one) ----------------------------------------------

def test_fan_out_dispatches_scouts_with_aimed_scan():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 4)
    assign_npc_profile(pid, "fan_out")
    run_npc_decisions()

    memory = _memory(pid)
    assert memory["pc"] == 2, "parked on the decide step, waiting for scans"
    assert "waiting" in memory
    assert memory["bindings"] == {}

    orgs = _orgs(pid)
    for index, oid in enumerate(ship_ids):
        org = orgs[oid]
        assert org["sector_id"] == -1 and org["mission"] == "move"
        bearing = CYCLE[index % len(CYCLE)]
        assert (org["scan_offset_x"], org["scan_offset_y"],
                org["scan_offset_z"]) == AIM[bearing]

def test_fan_out_commits_to_the_revealed_sector_once_scan_resolves():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_id = _seed_fleet(pid, sid, 1)[0]
    # Scan costs food+energy (POD_CONSUMPTION_RECIPE) -- a bare ship has
    # nothing to pay with and its scan never resolves.
    seed_pod(ship_id, task="produce_food", storage_current=100.0)
    seed_pod(ship_id, task="produce_energy", storage_current=100.0)
    assign_npc_profile(pid, "fan_out")

    for _ in range(4):
        end_of_turn()

    memory = _memory(pid)
    assert memory["bindings"]["target"]["x"] == 25
    assert memory["bindings"]["target"]["y"] == 21   # home(25,25) - 2 scout - 2 aim, north is -y
    conn = get_connection()
    org = conn.execute("SELECT sector_id, mission FROM organizations WHERE id=?",
                       (ship_id,)).fetchone()
    aq = conn.execute("SELECT dest_x,dest_y,dest_z FROM arrival_queue WHERE org_id=?",
                      (ship_id,)).fetchone()
    conn.close()
    assert org["sector_id"] == -1 and org["mission"] == "move"
    assert (aq["dest_x"], aq["dest_y"], aq["dest_z"]) == (25, 21, 0)

def test_fan_out_converges_whole_fleet_on_the_richest_scouted_sector():
    """The behaviour the decide step exists for: nobody commits until every
    scan is in, and then the WHOLE fleet goes to the best find -- not just the
    ship that found it."""
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 8)
    for oid in ship_ids:
        seed_pod(oid, task="produce_food", storage_current=100.0)
        seed_pod(oid, task="produce_energy", storage_current=100.0)
    # Each direction's reveal target (2 scout + 2 aim = 4 out from home) with
    # a distinct known richness -- south is the deliberate best. reveal_sector
    # leaves an existing row untouched, so these survive real scan resolution.
    seed_sector(25, 21, 0, energy=600.0)   # north
    seed_sector(25, 29, 0, energy=900.0)   # south -- richest
    seed_sector(29, 25, 0, energy=750.0)   # east
    seed_sector(21, 25, 0, energy=500.0)   # west
    assign_npc_profile(pid, "fan_out")

    for _ in range(4):
        end_of_turn()

    memory = _memory(pid)
    target = memory["bindings"]["target"]
    assert (target["x"], target["y"], target["z"]) == (25, 29, 0)
    assert target["energy_capacity"] == 900.0

    conn = get_connection()
    dests = conn.execute("""SELECT dest_x, dest_y, dest_z FROM arrival_queue
        WHERE org_id IN ({})""".format(",".join("?" * len(ship_ids))), ship_ids).fetchall()
    conn.close()
    assert len(dests) == 8
    assert all((d["dest_x"], d["dest_y"], d["dest_z"]) == (25, 29, 0) for d in dests)

def test_fan_out_holds_the_fleet_until_every_scan_is_in():
    """A partial picture is not a decision: one scout reporting must not move
    the fleet."""
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 2)
    for oid in ship_ids:
        seed_pod(oid, task="produce_food", storage_current=100.0)
    assign_npc_profile(pid, "fan_out")
    run_npc_decisions()

    # Resolve only the first scout's aim.
    revealed = seed_sector(25, 21, 0, energy=600.0)
    conn = get_connection()
    conn.execute("""UPDATE organizations SET sector_id=(SELECT id FROM sectors
                    WHERE coord_x=25 AND coord_y=23 AND coord_z=0), mission='idle'
                    WHERE id=?""", (ship_ids[0],))
    conn.execute("INSERT OR REPLACE INTO player_sectors (player_id, sector_id, confidence) "
                 "VALUES (?,?,100)", (pid, revealed))
    conn.commit(); conn.close()

    run_npc_decisions()
    memory = _memory(pid)
    assert memory["pc"] == 2, "still parked on the decide step"
    assert memory["bindings"] == {}, "nothing bound while a scout is unaccounted for"

def test_fan_out_noop_with_no_ships():
    pid = seed_player()
    assign_npc_profile(pid, "fan_out")
    run_npc_decisions()  # must not raise
    assert _memory(pid) == {}
