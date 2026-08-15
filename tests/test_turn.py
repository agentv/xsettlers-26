import json
from db.connection import get_connection
from db.sectors import (MIN_SECTOR_ENERGY, MAX_SECTOR_ENERGY, CONFIDENCE_DECAY_PER_TURN,
                        TURNS_TO_BLINK_OUT)
from engine.turn import (end_of_turn, check_consensus_acceleration,
                         _calculate_final_scores, get_next_tick_at)
from xsettlers_mcp.tools.sector_tools import get_sector_map, show_sector_neighborhood
from tests.conftest import (seed_player, seed_sector, seed_ship, seed_pod,
                            seed_player_sector)

def test_snapshot_holdings_writes_turn_snapshot_event_with_waste_and_score():
    """turn.snapshot events (one per player per turn) persist after-state
    holdings, this turn's weighted score
    (same formula as show_game_status/_calculate_final_scores), and derived
    per-resource waste (produced - consumed - actual delta).

    Hand-verified saturated org. Two energy pods and a food pod, all full at
    10/10. Org upkeep runs first and takes 5 food + 3 energy; the two energy
    pods then make 12 between them, of which only 7 finds anywhere to go and
    2 is lost. The food pod produces nothing at all -- its recipe needs goods,
    and the org holds none -- which is why energy is the only resource that
    wastes anything.

    Note the org ends at 30/30 either way: it is capacity-bound, so raising
    production raises waste rather than holdings. That is the behaviour this
    test is really pinning.

    Deliberately three pods rather than two: at the current production rates
    a 2-pod version leaves enough headroom that nothing overflows, and a
    waste test that never wastes anything is worse than no test."""
    pid = seed_player(); sid = seed_sector(energy=1000.0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_capacity=10.0, storage_current=10.0)
    seed_pod(oid, task="produce_energy", storage_capacity=10.0, storage_current=10.0)
    seed_pod(oid, task="produce_food", storage_capacity=10.0, storage_current=10.0)
    end_of_turn()
    conn = get_connection()
    row = conn.execute(
        "SELECT payload FROM events WHERE event_type='turn.snapshot' AND turn=0 AND subject_id=?",
        (pid,)).fetchone()
    conn.close()
    payload = json.loads(row["payload"])
    assert payload["energy"] == 27.0
    assert payload["food"] == 3.0
    assert payload["goods"] == 0.0
    assert payload["energy_wasted"] == 2.0   # 12 produced - 3 upkeep - 7 actually stored
    assert payload["food_wasted"] == 0.0
    assert payload["goods_wasted"] == 0.0
    assert payload["score"] == 3.0  # 27*0 (energy) + 3*1 (food) + 0*2 (goods)

def test_get_next_tick_at_none_until_clock_has_run():
    """None means "clock has never ticked yet" -- distinct from a stale
    value, which a caller can't tell apart from a paused clock without also
    checking the server is actually alive (see scripts/status.py)."""
    assert get_next_tick_at() is None
    conn = get_connection()
    conn.execute("UPDATE game_state SET next_tick_at=? WHERE id=1", ("2026-01-01T00:00:00.000Z",))
    conn.commit(); conn.close()
    assert get_next_tick_at() == "2026-01-01T00:00:00.000Z"

def test_show_game_status_countdown_is_dashes_when_clock_has_never_run():
    """No scenario's clock has ticked yet in this test DB, so next_tick_at is
    None and the countdown must show dashes rather than a stale/garbage time."""
    from xsettlers_mcp.tools.organization_reports import show_game_status
    seed_player()
    status = show_game_status("U_P1")
    assert status["next_tick_at"] is None
    assert status["next_tick_countdown"] == "--:--"
    assert status["display"]["header"] == "Turn 0 of 20 (--:--)"

def test_show_game_status_countdown_formats_mmss_from_next_tick_at():
    from datetime import datetime, timedelta, timezone
    from xsettlers_mcp.tools.organization_reports import show_game_status
    seed_player()
    future = datetime.now(timezone.utc) + timedelta(minutes=4, seconds=30)
    conn = get_connection()
    conn.execute("UPDATE game_state SET next_tick_at=? WHERE id=1",
                (future.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",))
    conn.commit(); conn.close()
    status = show_game_status("U_P1")
    assert status["next_tick_countdown"] in ("04:30", "04:29")  # clock-skew tolerant

def test_calculate_final_scores_applies_score_weights():
    """The game-over win condition: score is a weighted sum
    (config/game_config.yaml's score_weights -- energy=0/food=1/goods=2),
    not a flat energy+food+goods total -- must
    match what show_civilization_status/show_game_status compute mid-game
    via the same weights, or the displayed standing and the real winner
    could disagree."""
    p1 = seed_player(email="p1@t.com", player_token="U_P1")
    p2 = seed_player(email="p2@t.com", player_token="U_P2")
    sid = seed_sector()
    o1 = seed_ship(p1, sid, name="P1 Ship")
    o2 = seed_ship(p2, sid, name="P2 Ship")
    seed_pod(o1, task="produce_energy", storage_capacity=100.0, storage_current=80.0)  # weighted 0
    seed_pod(o2, task="produce_food", storage_capacity=100.0, storage_current=10.0)
    seed_pod(o2, task="produce_goods", storage_capacity=100.0, storage_current=10.0)
    standings = _calculate_final_scores()
    by_player = {s["player_id"]: s["score"] for s in standings}
    assert by_player[p1] == 0
    assert by_player[p2] == 30  # 10*1 (food) + 10*2 (goods)
    assert standings[0]["player_id"] == p2  # higher score wins despite lower raw total

def test_turn_resets_end_turn_declared():
    seed_player()
    conn = get_connection()
    conn.execute("UPDATE players SET end_turn_declared=1"); conn.commit(); conn.close()
    end_of_turn()
    conn = get_connection()
    assert conn.execute("SELECT end_turn_declared FROM players").fetchone()[0] == 0
    conn.close()

def test_consensus_fires_when_all_declared():
    seed_player()
    conn = get_connection()
    conn.execute("UPDATE players SET end_turn_declared=1"); conn.commit(); conn.close()
    assert check_consensus_acceleration() is True

def test_consensus_does_not_fire_when_undeclared():
    seed_player()
    assert check_consensus_acceleration() is False

# --- lazy sector reveal (see db/sectors.py) ---

def test_arrival_reveals_destination_and_stamps_visibility():
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    confirm_move("U_P1", sid, 1, 0, 0, jump_range_per_turn=1)  # turns_needed == 1
    end_of_turn()  # arrival not yet due
    end_of_turn()  # arrival resolves
    conn = get_connection()
    sector = conn.execute("SELECT id,energy_capacity FROM sectors "
                          "WHERE coord_x=1 AND coord_y=0 AND coord_z=0").fetchone()
    assert sector is not None
    assert MIN_SECTOR_ENERGY <= sector["energy_capacity"] <= MAX_SECTOR_ENERGY
    org = conn.execute("SELECT sector_id FROM organizations WHERE id=?", (sid,)).fetchone()
    assert org["sector_id"] == sector["id"]
    ps = conn.execute("SELECT confidence FROM player_sectors WHERE player_id=? AND sector_id=?",
                      (pid, sector["id"])).fetchone()
    assert ps["confidence"] == 100
    conn.close()

# --- fog decay (see db/sectors.py's CONFIDENCE_DECAY_PER_TURN) ---

def _confidence(player_id, sector_id):
    conn = get_connection()
    row = conn.execute("SELECT confidence FROM player_sectors WHERE player_id=? AND sector_id=?",
                       (player_id, sector_id)).fetchone()
    conn.close(); return row["confidence"]

def test_fog_decays_by_a_flat_amount_and_blinks_out_on_schedule():
    """Flat subtraction, not a fraction of what's left: a proportional decay
    on an integer column never reaches 0 (round(4 * 0.9) == 4), so sectors
    would linger on the map forever. Confidence must land exactly on 0 after
    TURNS_TO_BLINK_OUT unoccupied turns."""
    pid = seed_player()
    home = seed_sector(0, 0, 0); seed_ship(pid, home)   # keeps the player alive
    remembered = seed_sector(9, 9, 0)
    seed_player_sector(pid, remembered, 100)

    seen = [_confidence(pid, remembered)]
    for _ in range(TURNS_TO_BLINK_OUT):
        end_of_turn()
        seen.append(_confidence(pid, remembered))

    assert seen == [100 - i * CONFIDENCE_DECAY_PER_TURN for i in range(TURNS_TO_BLINK_OUT + 1)]
    assert seen[-1] == 0

def test_blinked_out_sector_leaves_the_map_but_keeps_its_row():
    """At 0 the sector drops out of every player-facing view (they all filter
    confidence > 0), but the row survives as the player's own history of
    having been there."""
    pid = seed_player()
    home = seed_sector(0, 0, 0); oid = seed_ship(pid, home)
    remembered = seed_sector(2, 0, 0)
    seed_player_sector(pid, remembered, CONFIDENCE_DECAY_PER_TURN)

    end_of_turn()

    assert _confidence(pid, remembered) == 0
    assert not any(s["id"] == remembered for s in get_sector_map("U_P1"))
    neighborhood = show_sector_neighborhood("U_P1", org_id=oid, radius=3)
    assert not any(s["id"] == remembered for s in neighborhood["sectors"])
    conn = get_connection()
    assert conn.execute("SELECT COUNT(*) n FROM player_sectors WHERE sector_id=?",
                        (remembered,)).fetchone()["n"] == 1
    conn.close()

def test_fog_does_not_decay_a_sector_you_occupy():
    pid = seed_player()
    home = seed_sector(0, 0, 0); seed_ship(pid, home)
    seed_player_sector(pid, home, 100)
    end_of_turn()
    assert _confidence(pid, home) == 100

# --- end-of-game scoreboard (must be recorded, not just printed) ---

def _play_to_game_over():
    from engine.turn import TURN_LIMIT
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_goods", storage_current=50.0)
    seed_pod(oid, task="produce_food", storage_current=100.0)
    for _ in range(TURN_LIMIT):
        end_of_turn()
    return pid

def test_final_scores_are_recorded_as_an_event_not_just_printed():
    """A game whose outcome exists only in a server log is a game nobody can
    be told they won -- and replay/audit needs the scoreboard as it stood at
    the whistle, not something recomputed later."""
    from engine.turn import get_final_scores, FINAL_SCORES_EVENT
    _play_to_game_over()
    conn = get_connection()
    row = conn.execute("SELECT payload FROM events WHERE event_type=?",
                       (FINAL_SCORES_EVENT,)).fetchone()
    conn.close()
    assert row is not None
    final = get_final_scores()
    assert final["winner"] == "Player One"
    assert final["final_turn"] == final["turn_limit"]
    assert final["score_weights"]                      # self-explaining without re-reading config
    top = final["standings"][0]
    assert top["rank"] == 1
    assert {"score", "energy", "food", "goods", "total", "capacity"} <= set(top)

def test_final_scores_are_written_once():
    """Writing twice would give a game two endings."""
    from engine.turn import FINAL_SCORES_EVENT
    _play_to_game_over()
    end_of_turn(); end_of_turn()          # no-ops past the limit
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) n FROM events WHERE event_type=?",
                     (FINAL_SCORES_EVENT,)).fetchone()["n"]
    conn.close()
    assert n == 1

def test_get_final_scores_is_none_before_game_over():
    from engine.turn import get_final_scores
    seed_player()
    end_of_turn()
    assert get_final_scores() is None

def test_show_game_status_becomes_the_final_scoreboard_after_game_over():
    """The scoreboard a player is handed at the end is the recorded one."""
    from xsettlers_mcp.tools.organization_reports import show_game_status
    _play_to_game_over()
    status = show_game_status("U_P1")
    assert status["game_over"] is True and status["is_final"] is True
    assert status["winner"] == "Player One"
    assert status["display"]["header"].startswith("FINAL — game over at turn")
    assert status["standings"][0]["rank"] == 1

def test_show_game_status_is_not_final_mid_game():
    from xsettlers_mcp.tools.organization_reports import show_game_status
    seed_player(); end_of_turn()
    status = show_game_status("U_P1")
    assert status["game_over"] is False and status["winner"] is None
    assert status["display"]["header"].startswith("Turn ")


# The tools reject an out-of-range aim at set time, so these write the offset
# straight into the DB. That is not a contrived state: engine/turn.py re-checks
# range at resolution precisely because get_scan_range() is expected to vary
# per org later (sensor pods), at which point an aim that was legal when set
# can stop being legal. This is the branch that handles it, on the engine
# side, for both scan paths (see engine/turn.py's _resolve_scan).

