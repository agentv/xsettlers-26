import json
from db.connection import get_connection
from db.npc_profiles import assign_npc_profile
from engine.npc import run_npc_decisions
from engine.turn import end_of_turn
from tests.conftest import seed_player, seed_sector, seed_ship

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
    assert memory["second_leg_dispatched"] is False
    assert set(memory["mover"].keys()) == {"north", "south", "east", "west"}
    # arrival_turn (distance 3 at jump_range 1 => turn 0+3+1=4) + hold_turns (default 2)
    assert memory["second_leg_turn"] == 6

def test_end_of_turn_automatically_drives_npc_through_both_legs():
    """
    The gap flagged in docs/TODO.md's NPC scoping note: player2_policy.py had
    to be invoked manually. Here nothing but end_of_turn() itself is called
    -- proving the strategy fires on its own, first for the opening moves and
    later (once its own ships have actually arrived and hold_turns has
    passed) for the second-leg jump.
    """
    pid = seed_player()
    sid = seed_sector(25, 25, 0)
    ship_ids = _seed_fleet(pid, sid, 8)
    assign_npc_profile(pid, "fan_out_consolidate",
                       config={"leg_distance": 3, "jump_range_per_turn": 1, "hold_turns": 2})

    end_of_turn()  # turn 0: opening moves dispatched by the NPC step itself
    conn = get_connection()
    orgs = conn.execute("SELECT sector_id FROM organizations WHERE id IN "
                        f"({','.join('?'*len(ship_ids))})", ship_ids).fetchall()
    conn.close()
    assert all(o["sector_id"] == -1 for o in orgs)  # all in transit

    for _ in range(6):  # turns 1..6: arrival resolves, then hold_turns elapses, then second leg fires
        end_of_turn()

    memory = _memory(pid)
    assert memory["second_leg_dispatched"] is True
    conn = get_connection()
    aq = {r["org_id"]: (r["dest_x"], r["dest_y"], r["dest_z"])
          for r in conn.execute("SELECT org_id, dest_x, dest_y, dest_z FROM arrival_queue").fetchall()}
    conn.close()
    mover_ids = set(memory["mover"].values())
    assert mover_ids <= set(aq.keys())  # movers are in transit again, toward their second leg
    for name, mover_id in memory["mover"].items():
        assert list(aq[mover_id]) == memory["second_dest"][name]
    stayer_ids = set(memory["stayer"].values())
    assert stayer_ids.isdisjoint(aq.keys())  # stayers were never re-dispatched
