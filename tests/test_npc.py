import json
from db.connection import get_connection
from db.npc_profiles import assign_npc_profile
from engine.npc import run_npc_decisions
from engine.turn import end_of_turn
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod

def _seed_fleet(player_id, sector_id, n=8):
    return [seed_ship(player_id, sector_id, name=f"Ship-{i}") for i in range(n)]

def _memory(player_id):
    conn = get_connection()
    row = conn.execute("SELECT memory FROM npc_profiles WHERE player_id=?", (player_id,)).fetchone()
    conn.close()
    return json.loads(row["memory"])

def test_assign_npc_profile_sets_flag_and_creates_row():
    pid = seed_player()
    result = assign_npc_profile(pid, "fan_out_consolidate", config={"leg_distance": 5})
    assert result == {"ok": True, "player_id": pid, "strategy_name": "fan_out_consolidate"}
    conn = get_connection()
    player = conn.execute("SELECT is_npc FROM players WHERE id=?", (pid,)).fetchone()
    profile = conn.execute("SELECT strategy_name, config, memory FROM npc_profiles WHERE player_id=?",
                           (pid,)).fetchone()
    conn.close()
    assert player["is_npc"] == 1
    assert profile["strategy_name"] == "fan_out_consolidate"
    assert json.loads(profile["config"]) == {"leg_distance": 5}
    assert json.loads(profile["memory"]) == {}

def test_assign_npc_profile_reassignment_resets_memory():
    """Switching a player's strategy discards its old memory -- a different
    strategy has no use for another strategy's working-state blob."""
    pid = seed_player()
    assign_npc_profile(pid, "fan_out_consolidate")
    conn = get_connection()
    conn.execute("UPDATE npc_profiles SET memory=? WHERE player_id=?",
                 (json.dumps({"opening_dispatched": True}), pid))
    conn.commit(); conn.close()
    assign_npc_profile(pid, "fan_out_consolidate", config={"leg_distance": 7})
    conn = get_connection()
    profile = conn.execute("SELECT config, memory FROM npc_profiles WHERE player_id=?", (pid,)).fetchone()
    conn.close()
    assert json.loads(profile["memory"]) == {}
    assert json.loads(profile["config"]) == {"leg_distance": 7}

def test_run_npc_decisions_noop_for_non_npc_players():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    seed_ship(pid, sid)
    run_npc_decisions()  # no npc_profiles row for this player -- must not raise or act
    conn = get_connection()
    org = conn.execute("SELECT sector_id FROM organizations WHERE player_id=?", (pid,)).fetchone()
    conn.close()
    assert org["sector_id"] == sid

def test_unknown_strategy_name_is_skipped_without_crashing():
    pid = seed_player()
    assign_npc_profile(pid, "does_not_exist")
    run_npc_decisions()  # must not raise

def test_fan_out_strategy_noop_with_fewer_than_eight_ships():
    """Fewer than 8 ships means the 4-direction/2-ships-per-direction plan
    doesn't fit -- the strategy marks itself done without acting, rather than
    IndexError-ing or retrying every turn forever."""
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    _seed_fleet(pid, sid, 3)
    assign_npc_profile(pid, "fan_out_consolidate")
    run_npc_decisions()
    conn = get_connection()
    orgs = conn.execute("SELECT sector_id FROM organizations WHERE player_id=?", (pid,)).fetchall()
    conn.close()
    assert all(o["sector_id"] == sid for o in orgs)
    assert _memory(pid)["opening_dispatched"] is True

def test_fan_out_dispatches_opening_moves_for_eight_ships():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    _seed_fleet(pid, sid, 8)
    assign_npc_profile(pid, "fan_out_consolidate",
                       config={"leg_distance": 3, "jump_range_per_turn": 1})
    run_npc_decisions()
    conn = get_connection()
    orgs = conn.execute("SELECT sector_id, mission FROM organizations WHERE player_id=?", (pid,)).fetchall()
    conn.close()
    assert all(o["sector_id"] == -1 and o["mission"] == "move" for o in orgs)
    memory = _memory(pid)
    assert memory["opening_dispatched"] is True
    assert set(memory["mover"].keys()) == {"north", "south", "east", "west"}
    # Second leg is now driven by the ship's log, not memory polling: each
    # mover should have an after_arrival 'move' command queued for its
    # second_dest, resolving one turn after arrival_turn (distance 3 at
    # jump_range 1 => arrival_turn 0+3+1=4, so resolve_turn=5).
    conn = get_connection()
    queued = {r["org_id"]: r for r in conn.execute(
        "SELECT org_id,trigger_phase,action,resolve_turn,params FROM org_command_queue").fetchall()}
    conn.close()
    mover_ids = set(memory["mover"].values())
    assert set(queued.keys()) == mover_ids
    for mover_id in mover_ids:
        row = queued[mover_id]
        assert row["trigger_phase"] == "after_arrival"
        assert row["action"] == "move"
        assert row["resolve_turn"] == 5

def test_end_of_turn_automatically_drives_npc_through_both_legs():
    """
    The gap flagged in docs/TODO.md's NPC scoping note: player2_policy.py had
    to be invoked manually. Here nothing but end_of_turn() itself is called
    -- proving the strategy fires on its own, first for the opening moves and
    later (once its own ships have actually arrived and the ship's log's
    after_arrival phase fires, one turn later) for the second-leg jump.
    """
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 8)
    assign_npc_profile(pid, "fan_out_consolidate",
                       config={"leg_distance": 3, "jump_range_per_turn": 1})

    end_of_turn()  # turn 0: opening moves dispatched by the NPC step itself
    conn = get_connection()
    orgs = conn.execute("SELECT sector_id FROM organizations WHERE id IN "
                        f"({','.join('?'*len(ship_ids))})", ship_ids).fetchall()
    conn.close()
    assert all(o["sector_id"] == -1 for o in orgs)  # all in transit

    for _ in range(4):  # arrival resolves (turn 4), then after_arrival fires (turn 5)
        end_of_turn()

    memory = _memory(pid)
    conn = get_connection()
    queued = conn.execute("SELECT COUNT(*) AS n FROM org_command_queue").fetchone()
    aq = {r["org_id"]: (r["dest_x"], r["dest_y"], r["dest_z"])
          for r in conn.execute("SELECT org_id, dest_x, dest_y, dest_z FROM arrival_queue").fetchall()}
    conn.close()
    assert queued["n"] == 0  # one-shot: dispatched and deleted, nothing left queued
    mover_ids = set(memory["mover"].values())
    assert mover_ids <= set(aq.keys())  # movers are in transit again, toward their second leg
    for name, mover_id in memory["mover"].items():
        assert list(aq[mover_id]) == memory["second_dest"][name]
    stayer_ids = set(memory["stayer"].values())
    assert stayer_ids.isdisjoint(aq.keys())  # stayers were never re-dispatched

# --- turtle ---

def test_turtle_never_acts():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 4)
    assign_npc_profile(pid, "turtle")
    for _ in range(3):
        run_npc_decisions()
    conn = get_connection()
    orgs = conn.execute("SELECT sector_id, mission FROM organizations WHERE player_id=?", (pid,)).fetchall()
    conn.close()
    assert all(o["sector_id"] == sid and o["mission"] == "idle" for o in orgs)

# --- burst_and_colonize ---

def test_burst_and_colonize_dispatches_and_colonizes_on_first_call():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 8)
    for sid_ship in ship_ids:
        seed_pod(sid_ship, task="produce_energy", storage_current=100.0)
    assign_npc_profile(pid, "burst_and_colonize",
                       config={"leg_distance": 3, "jump_range_per_turn": 1, "colonize_fraction": 0.25})
    run_npc_decisions()

    memory = _memory(pid)
    assert memory["dispatched"] is True
    assert len(memory["colonized"]) == 2  # round(8 * 0.25)
    assert len(memory["fanned_out"]) == 6

    conn = get_connection()
    orgs = {r["id"]: r for r in conn.execute(
        "SELECT id, sector_id, mission, is_mobile FROM organizations WHERE player_id=?", (pid,)).fetchall()}
    conn.close()
    for oid in memory["colonized"]:
        assert orgs[oid]["mission"] == "colonize"
        assert orgs[oid]["sector_id"] == sid  # colonizing ships never moved
        assert orgs[oid]["is_mobile"] == 0
    for oid in memory["fanned_out"]:
        assert orgs[oid]["sector_id"] == -1  # in transit
        assert orgs[oid]["mission"] == "move"

def test_burst_and_colonize_only_dispatches_once():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 4)
    for sid_ship in ship_ids:
        seed_pod(sid_ship, task="produce_energy", storage_current=100.0)
    assign_npc_profile(pid, "burst_and_colonize")
    run_npc_decisions()
    memory_after_first = _memory(pid)
    run_npc_decisions()  # must be a no-op the second time
    assert _memory(pid) == memory_after_first

def test_burst_and_colonize_noop_fraction_with_a_single_ship():
    """len(ship_ids) > 1 gates colonizing at all -- a lone ship always fans
    out rather than being forced to colonize by round()."""
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    _seed_fleet(pid, sid, 1)
    assign_npc_profile(pid, "burst_and_colonize")
    run_npc_decisions()
    memory = _memory(pid)
    assert memory["colonized"] == []
    assert len(memory["fanned_out"]) == 1

# --- frontier_map_stay_frosty ---

def test_frontier_map_stay_frosty_moves_idle_ships_and_aims_scanner_ahead():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 4)
    assign_npc_profile(pid, "frontier_map_stay_frosty",
                       config={"leg_distance": 3, "jump_range_per_turn": 1})
    run_npc_decisions()

    conn = get_connection()
    orgs = {r["id"]: r for r in conn.execute(
        """SELECT id, sector_id, mission, scan_offset_x, scan_offset_y, scan_offset_z
           FROM organizations WHERE player_id=?""", (pid,)).fetchall()}
    conn.close()
    # Scan aim is always the "X2" bearing (distance 2, SCAN_RANGE's max),
    # decoupled from leg_distance (3 here) -- movement has no range cap,
    # scanning does.
    scan_offsets = {"N": (0, -2, 0), "S": (0, 2, 0), "E": (2, 0, 0), "W": (-2, 0, 0)}
    directions = _memory(pid)["directions"]
    for oid in ship_ids:
        org = orgs[oid]
        assert org["sector_id"] == -1 and org["mission"] == "move"
        bearing = directions[str(oid)]
        assert (org["scan_offset_x"], org["scan_offset_y"], org["scan_offset_z"]) == scan_offsets[bearing]

def test_frontier_map_stay_frosty_redirects_after_landing():
    """No terminal state -- a ship that lands gets picked up and redirected
    further out the same bearing on the NEXT NPC pass after landing (not the
    same call as landing itself: run_npc_decisions is step 0, so it always
    reads state as of the end of the PREVIOUS end_of_turn() call -- it can't
    see this call's own later arrival-resolution step)."""
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 1)
    assign_npc_profile(pid, "frontier_map_stay_frosty",
                       config={"leg_distance": 2, "jump_range_per_turn": 1})

    def _org():
        conn = get_connection()
        row = conn.execute("SELECT sector_id, mission FROM organizations WHERE id=?",
                           (ship_ids[0],)).fetchone()
        conn.close()
        return row

    end_of_turn()  # call 1: opening dispatch, arrival_turn = 0+2+1 = 3
    assert _org()["sector_id"] == -1

    end_of_turn()  # call 2: still travelling
    assert _org()["sector_id"] == -1

    end_of_turn()  # call 3: lands (arrival resolves this call); NPC step0 ran before that, so no redirect yet
    org = _org()
    assert org["sector_id"] != -1 and org["mission"] == "idle"

    end_of_turn()  # call 4: NPC step0 now sees the landed ship from call 3, redirects it
    org = _org()
    assert org["sector_id"] == -1
    assert org["mission"] == "move"

# --- fan_out (opportunity-seeking) ---

def test_fan_out_dispatches_scouts_with_aimed_scan():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 4)
    assign_npc_profile(pid, "fan_out", config={"scout_distance": 2, "jump_range_per_turn": 1})
    run_npc_decisions()

    memory = _memory(pid)
    assert memory["opening_dispatched"] is True
    assert len(memory["scouts"]) == 4
    assert memory["found"] == {}

    conn = get_connection()
    orgs = {r["id"]: r for r in conn.execute(
        """SELECT id, sector_id, mission, scan_offset_x, scan_offset_y, scan_offset_z
           FROM organizations WHERE player_id=?""", (pid,)).fetchall()}
    conn.close()
    offsets = {"N": (0, -2, 0), "S": (0, 2, 0), "E": (2, 0, 0), "W": (-2, 0, 0)}
    for oid in ship_ids:
        org = orgs[oid]
        assert org["sector_id"] == -1 and org["mission"] == "move"
        bearing = memory["scouts"][str(oid)]["bearing"]
        assert (org["scan_offset_x"], org["scan_offset_y"], org["scan_offset_z"]) == offsets[bearing]

def test_fan_out_commits_to_the_revealed_sector_once_scan_resolves():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 1)
    # Scan costs food+energy (POD_CONSUMPTION_RECIPE) -- a bare ship with no
    # pods has nothing to pay with and the scan never resolves. Seed enough
    # of both to cover it every turn for the life of the test.
    seed_pod(ship_ids[0], task="produce_food", storage_current=100.0)
    seed_pod(ship_ids[0], task="produce_energy", storage_current=100.0)
    assign_npc_profile(pid, "fan_out", config={"scout_distance": 2, "jump_range_per_turn": 1})

    end_of_turn()  # call 1: opening scout dispatch, arrival_turn = 0+2+1 = 3, aim = 2 further N
    assert _memory(pid)["found"] == {}

    end_of_turn()  # call 2: still travelling
    assert _memory(pid)["found"] == {}

    end_of_turn()  # call 3: lands AND its scan resolves in the same call; NPC step0 ran before that
    assert _memory(pid)["found"] == {}  # not yet observed by NPC step

    end_of_turn()  # call 4: NPC step0 now sees the landed ship + resolved scan from call 3, converges
    memory = _memory(pid)
    assert memory["converged"] is True
    assert str(ship_ids[0]) in memory["found"]
    conn = get_connection()
    org = conn.execute("SELECT sector_id, mission FROM organizations WHERE id=?", (ship_ids[0],)).fetchone()
    aq = conn.execute("SELECT dest_x,dest_y,dest_z FROM arrival_queue WHERE org_id=?",
                      (ship_ids[0],)).fetchone()
    conn.close()
    assert org["sector_id"] == -1 and org["mission"] == "move"  # committed to final move
    assert (aq["dest_x"], aq["dest_y"], aq["dest_z"]) == (25, 21, 0)  # home(25,25) - 2(scout) - 2(reveal north)
    assert (memory["destination"]["x"], memory["destination"]["y"], memory["destination"]["z"]) == (25, 21, 0)

def test_fan_out_converges_whole_fleet_on_the_richest_scouted_sector():
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 8)
    for oid in ship_ids:
        seed_pod(oid, task="produce_food", storage_current=100.0)
        seed_pod(oid, task="produce_energy", storage_current=100.0)
    # Pre-seed each direction's reveal target (2 scout + 2 aim = 4 out from
    # home) with a distinct, known richness -- south is the deliberate best.
    # reveal_sector() leaves an already-existing row untouched, so these
    # survive the real scan resolution during play (see db/sectors.py).
    seed_sector(25, 21, 0, energy=600.0)   # north
    seed_sector(25, 29, 0, energy=900.0)   # south -- richest
    seed_sector(29, 25, 0, energy=750.0)   # east
    seed_sector(21, 25, 0, energy=500.0)   # west
    assign_npc_profile(pid, "fan_out", config={"scout_distance": 2, "jump_range_per_turn": 1})

    for _ in range(4):  # same cadence as the single-ship case above -- symmetric distances
        end_of_turn()

    memory = _memory(pid)
    assert memory["converged"] is True
    assert len(memory["found"]) == 8
    assert (memory["destination"]["x"], memory["destination"]["y"], memory["destination"]["z"]) == (25, 29, 0)
    assert memory["destination"]["energy_capacity"] == 900.0

    conn = get_connection()
    dests = conn.execute("""SELECT dest_x, dest_y, dest_z FROM arrival_queue
        WHERE org_id IN ({})""".format(",".join("?" * len(ship_ids))), ship_ids).fetchall()
    conn.close()
    assert len(dests) == 8
    assert all((d["dest_x"], d["dest_y"], d["dest_z"]) == (25, 29, 0) for d in dests)

def test_fan_out_noop_with_no_ships():
    pid = seed_player()
    assign_npc_profile(pid, "fan_out")
    run_npc_decisions()  # must not raise
    assert _memory(pid)["opening_dispatched"] is True
