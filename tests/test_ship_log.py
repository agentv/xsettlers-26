import json
from db.connection import get_connection
from engine.turn import end_of_turn
from xsettlers_mcp.tools.navigation_tools import confirm_move
from xsettlers_mcp.tools.organization_tools import queue_command
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod

def _queue_count():
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) AS n FROM org_command_queue").fetchone()["n"]
    conn.close(); return n

def _org(org_id):
    conn = get_connection()
    row = conn.execute("SELECT sector_id, mission FROM organizations WHERE id=?", (org_id,)).fetchone()
    conn.close(); return row

def _pod_task(pod_id):
    conn = get_connection()
    row = conn.execute("SELECT task FROM pods WHERE id=?", (pod_id,)).fetchone()
    conn.close(); return row["task"]

def test_queue_command_rejects_wrong_owner():
    p1 = seed_player()
    seed_player(email="p2@test.com", player_token="U_P2", display_name="Player Two")
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    confirm_move("U_P1", ship, 5, 0, 0, jump_range_per_turn=1)
    result = queue_command("U_P2", ship, "before_arrival", "move", {"dest_x": 9, "dest_y": 0, "dest_z": 0})
    assert "error" in result

def test_queue_command_rejects_invalid_trigger_phase():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    result = queue_command("U_P1", ship, "mid_flight", "move", {})
    assert "Invalid trigger_phase" in result["error"]

def test_queue_command_rejects_invalid_action():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    confirm_move("U_P1", ship, 5, 0, 0, jump_range_per_turn=1)
    result = queue_command("U_P1", ship, "before_arrival", "cancel_move", {})
    assert "Invalid action" in result["error"]

def test_queue_command_requires_transit_for_before_arrival():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)  # stationary -- never moved
    result = queue_command("U_P1", ship, "before_arrival", "move", {"dest_x": 5, "dest_y": 0, "dest_z": 0})
    assert "in transit" in result["error"]

def test_queue_command_requires_transit_for_after_arrival():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    result = queue_command("U_P1", ship, "after_arrival", "move", {"dest_x": 5, "dest_y": 0, "dest_z": 0})
    assert "in transit" in result["error"]

def test_queue_command_computes_resolve_turn_for_before_arrival():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    move = confirm_move("U_P1", ship, 2, 0, 0, jump_range_per_turn=1)
    result = queue_command("U_P1", ship, "before_arrival", "move", {"dest_x": 5, "dest_y": 0, "dest_z": 0})
    assert result["ok"] is True
    assert result["resolve_turn"] == move["arrival_turn"]

def test_queue_command_computes_resolve_turn_for_after_arrival():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    move = confirm_move("U_P1", ship, 2, 0, 0, jump_range_per_turn=1)
    result = queue_command("U_P1", ship, "after_arrival", "move", {"dest_x": 5, "dest_y": 0, "dest_z": 0})
    assert result["ok"] is True
    assert result["resolve_turn"] == move["arrival_turn"] + 1

def test_before_arrival_fires_same_end_of_turn_call_that_lands_org():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    move = confirm_move("U_P1", ship, 2, 0, 0, jump_range_per_turn=1)  # arrival_turn = 0+2+1 = 3
    queue_command("U_P1", ship, "before_arrival", "move", {"dest_x": 5, "dest_y": 0, "dest_z": 0})

    for _ in range(3):  # turn 0->1->2->3: the third call is the one that lands arrival_turn=3
        end_of_turn()

    org = _org(ship)
    assert org["sector_id"] == -1  # already back in transit toward the chained destination
    assert org["mission"] == "move"
    conn = get_connection()
    aq = conn.execute("SELECT dest_x,dest_y,dest_z FROM arrival_queue WHERE org_id=?", (ship,)).fetchone()
    conn.close()
    assert (aq["dest_x"], aq["dest_y"], aq["dest_z"]) == (5, 0, 0)
    assert _queue_count() == 0  # one-shot: dispatched and deleted

def test_after_arrival_does_not_fire_until_the_following_end_of_turn_call():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    move = confirm_move("U_P1", ship, 2, 0, 0, jump_range_per_turn=1)  # arrival_turn = 3
    queue_command("U_P1", ship, "after_arrival", "move", {"dest_x": 5, "dest_y": 0, "dest_z": 0})

    for _ in range(3):  # the landing call -- after_arrival must NOT have fired yet
        end_of_turn()
    org = _org(ship)
    assert org["sector_id"] != -1  # landed, not yet re-departed
    assert org["mission"] == "idle"
    assert _queue_count() == 1  # still pending

    end_of_turn()  # one more pass: now it fires
    org = _org(ship)
    assert org["sector_id"] == -1
    assert org["mission"] == "move"
    assert _queue_count() == 0

def test_dispatch_skips_when_org_mission_changed_before_trigger_fires():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    confirm_move("U_P1", ship, 2, 0, 0, jump_range_per_turn=1)  # arrival_turn = 3
    queue_command("U_P1", ship, "after_arrival", "move", {"dest_x": 5, "dest_y": 0, "dest_z": 0})

    for _ in range(3):
        end_of_turn()  # lands at turn 3, mission='idle'

    # Player manually gives the org new orders before after_arrival's turn 4 fires.
    conn = get_connection()
    conn.execute("UPDATE organizations SET mission='defend' WHERE id=?", (ship,))
    conn.commit(); conn.close()

    end_of_turn()  # after_arrival is due now, but must not clobber the manual order

    org = _org(ship)
    assert org["mission"] == "defend"
    assert org["sector_id"] != -1  # never re-departed
    assert _queue_count() == 0  # the stale row was still consumed, just not acted on

def test_before_arrival_dispatches_set_pod_task():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    pod = seed_pod(ship, task="produce_energy")
    confirm_move("U_P1", ship, 2, 0, 0, jump_range_per_turn=1)  # arrival_turn = 3
    queue_command("U_P1", ship, "before_arrival", "set_pod_task",
                 {"pod_id": pod, "task": "produce_food"})

    for _ in range(3):
        end_of_turn()

    assert _pod_task(pod) == "produce_food"
    assert _queue_count() == 0

def test_after_arrival_dispatches_set_pod_task_one_turn_later():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    pod = seed_pod(ship, task="produce_energy")
    confirm_move("U_P1", ship, 2, 0, 0, jump_range_per_turn=1)  # arrival_turn = 3
    queue_command("U_P1", ship, "after_arrival", "set_pod_task",
                 {"pod_id": pod, "task": "produce_goods"})

    for _ in range(3):
        end_of_turn()
    assert _pod_task(pod) == "produce_energy"  # not yet -- landed this call, one more to go

    end_of_turn()
    assert _pod_task(pod) == "produce_goods"
    assert _queue_count() == 0

def test_during_transit_dispatches_set_pod_task_the_instant_it_departs():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    pod = seed_pod(ship, task="produce_energy")
    queue_command("U_P1", ship, "during_transit", "set_pod_task",
                 {"pod_id": pod, "task": "scan"})
    assert _pod_task(pod) == "produce_energy"  # not yet -- org hasn't departed

    confirm_move("U_P1", ship, 5, 0, 0, jump_range_per_turn=1)  # departure itself dispatches it

    assert _pod_task(pod) == "scan"  # fired synchronously, no end_of_turn() call needed
    assert _queue_count() == 0

def test_during_transit_rejects_non_pod_task_actions():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    result = queue_command("U_P1", ship, "during_transit", "move",
                           {"dest_x": 5, "dest_y": 0, "dest_z": 0})
    assert "error" in result

def test_at_turn_requires_no_transit_and_fires_at_the_exact_turn():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    pod = seed_pod(ship, task="produce_energy")  # ship never moves -- not in transit
    result = queue_command("U_P1", ship, "at_turn", "set_pod_task",
                           {"pod_id": pod, "task": "produce_goods"}, turn=5)
    assert result["ok"] is True
    assert result["resolve_turn"] == 5

    for _ in range(4):
        end_of_turn()
    assert _pod_task(pod) == "produce_energy"  # turn 4 in progress -- not due yet

    end_of_turn()
    assert _pod_task(pod) == "produce_goods"  # now at turn 5
    assert _queue_count() == 0

def test_at_turn_requires_turn_parameter():
    p1 = seed_player()
    sid = seed_sector(0, 0, 0)
    ship = seed_ship(p1, sid)
    result = queue_command("U_P1", ship, "at_turn", "move", {"dest_x": 5, "dest_y": 0, "dest_z": 0})
    assert "error" in result


# --- Dispatch containment ----------------------------------------------------
# queue_command now validates params up front, so these rows can no longer be
# created through the tool. They are inserted directly to stand for what that
# check cannot cover: rows queued before it existed (a live DB may hold some),
# and any future action that learns to fail. The invariant under test is that
# one player's bad order cannot stop the turn for everyone -- which is exactly
# what used to happen, since the exception escaped end_of_turn() and the row
# was never deleted, so it re-fired on every subsequent tick forever.

def _inject_raw_command(org_id, action, params_json, resolve_turn=0,
                        trigger_phase="at_turn"):
    conn = get_connection()
    conn.execute("""INSERT INTO org_command_queue
        (org_id,trigger_phase,resolve_turn,action,params,created_turn)
        VALUES (?,?,?,?,?,0)""", (org_id, trigger_phase, resolve_turn, action, params_json))
    conn.commit(); conn.close()


def _failure_alerts():
    conn = get_connection()
    rows = [dict(r) for r in conn.execute(
        """SELECT subject_id, payload FROM events
           WHERE event_type='alert.queued_command_failed' ORDER BY id""").fetchall()]
    conn.close()
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return rows


def _turn():
    conn = get_connection()
    t = conn.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()[0]
    conn.close(); return t


def test_a_malformed_queued_command_no_longer_wedges_the_turn_engine():
    """The regression this guard exists for. An invalid task hits the pods
    CHECK constraint; before the guard that IntegrityError escaped
    end_of_turn(), the row survived the rollback, and every later tick raised
    again -- the game could never advance."""
    pid = seed_player(); sid = seed_sector(energy=1000.0); oid = seed_ship(pid, sid)
    pod = seed_pod(oid, task="produce_food", storage_capacity=100.0, storage_current=50.0)
    _inject_raw_command(oid, "set_pod_task",
                        '{"pod_id": %d, "task": "become_a_dragon"}' % pod)
    before = _turn()
    end_of_turn()                      # must not raise
    assert _turn() == before + 1       # the turn actually advanced
    assert _queue_count() == 0         # one-shot: the bad row is gone, not retried
    alerts = _failure_alerts()
    assert len(alerts) == 1 and alerts[0]["subject_id"] == oid
    assert "IntegrityError" in alerts[0]["payload"]["error"]
    # And the tick keeps working from here on -- the old failure mode was that
    # it never would again.
    end_of_turn()
    assert _turn() == before + 2


def test_a_bad_command_does_not_stop_another_players_turn_resolving():
    """Containment is the whole point: the rest of the turn must complete."""
    p1 = seed_player(email="a@t.com", player_token="U_P1")
    p2 = seed_player(email="b@t.com", player_token="U_P2")
    sid = seed_sector(energy=1000.0)
    o1 = seed_ship(p1, sid, name="Broken"); o2 = seed_ship(p2, sid, name="Fine")
    seed_pod(o1, task="produce_food", storage_capacity=100.0, storage_current=50.0)
    good_pod = seed_pod(o2, task="produce_energy", storage_capacity=100.0, storage_current=50.0)
    # o2 also needs food on hand: produce_energy's recipe costs food, and org
    # upkeep takes food too, so an energy pod alone would produce nothing and
    # this test would pass for the wrong reason.
    seed_pod(o2, task="produce_food", storage_capacity=100.0, storage_current=50.0)
    _inject_raw_command(o1, "move", '{"dest_x": 3}')      # KeyError on dest_y
    end_of_turn()
    conn = get_connection()
    produced = conn.execute("SELECT energy_stored FROM pods WHERE id=?",
                            (good_pod,)).fetchone()["energy_stored"]
    snapshots = conn.execute(
        "SELECT COUNT(*) n FROM events WHERE event_type='turn.snapshot'").fetchone()["n"]
    conn.close()
    assert produced > 50.0, "the unaffected player's production still resolved"
    assert snapshots == 2, "both players' ledger rows were still written"
    assert len(_failure_alerts()) == 1
    assert "KeyError" in _failure_alerts()[0]["payload"]["error"]


def test_during_transit_dispatch_is_guarded_too():
    """This dispatcher runs from confirm_move as well as the turn engine, so an
    unguarded raise would fail a player's own move call, not just the tick."""
    pid = seed_player(); sid = seed_sector(energy=1000.0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_food", storage_capacity=100.0, storage_current=50.0)
    _inject_raw_command(oid, "set_pod_task", '{"task": "idle"}',   # no pod_id -> KeyError
                        resolve_turn=None, trigger_phase="during_transit")
    result = confirm_move("U_P1", oid, 2, 0, 0)
    assert result.get("confirmed") is True, "the move itself must still succeed"
    assert _queue_count() == 0
    alerts = _failure_alerts()
    assert len(alerts) == 1 and "KeyError" in alerts[0]["payload"]["error"]
