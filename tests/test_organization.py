import json
from db.connection import connection
from engine.turn import end_of_turn
from xsettlers_mcp.tools.organization_tools import (
    set_mission, set_pod_task, set_call_sign, queue_command,
    UNIMPLEMENTED_MISSIONS, VALID_ORG_MISSIONS, WEAPONS_INOPERABLE
)
from engine.production import COLONIZATION_ENERGY_COST
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod

# --- set_mission happy path ---

def test_set_mission_idle():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    result = set_mission("U_P1", oid, "idle")
    assert result.get("ok") is True
    with connection() as conn:
        assert conn.execute("SELECT mission FROM organizations WHERE id=?",
                            (oid,)).fetchone()["mission"] == "idle"

def _seed_colonizer(storage_current=200.0):
    """A ship that can afford to colonize. Colonizing costs
    COLONIZATION_ENERGY_COST energy up front (see set_mission), so a ship
    with empty pods is refused outright -- every colonize test needs fuel
    aboard before the order will even be accepted."""
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=storage_current)
    return pid, sid, oid

def test_set_mission_colonize_locks_immediately_but_does_not_convert_yet():
    pid, sid, oid = _seed_colonizer()
    set_mission("U_P1", oid, "colonize")
    with connection() as conn:
        org = conn.execute("SELECT org_type,is_mobile,mission FROM organizations WHERE id=?",
                           (oid,)).fetchone()
    assert org["org_type"] == "ship"      # not flipped yet -- only at resolution
    assert org["is_mobile"] == 0          # locked immediately, per set_mission
    assert org["mission"] == "colonize"

def test_set_mission_colonize_converts_after_three_turns():
    pid, sid, oid = _seed_colonizer()
    set_mission("U_P1", oid, "colonize")   # scheduled for current_turn(0) + 3
    end_of_turn(); end_of_turn()           # turns 1, 2 -- not resolved yet
    with connection() as conn:
        still_ship = conn.execute("SELECT org_type FROM organizations WHERE id=?",
                                  (oid,)).fetchone()["org_type"]
    assert still_ship == "ship"
    end_of_turn()                          # turn 3 -- resolve_at_turn matches
    with connection() as conn:
        org = conn.execute("SELECT org_type,is_mobile,mission FROM organizations WHERE id=?",
                           (oid,)).fetchone()
    assert org["org_type"] == "colony"
    assert org["is_mobile"] == 0
    assert org["mission"] == "idle"

def test_set_mission_colonize_charges_energy_up_front():
    """The charge lands at commitment, not at resolution three turns later."""
    pid, sid, oid = _seed_colonizer(storage_current=200.0)
    result = set_mission("U_P1", oid, "colonize")
    assert result.get("ok") is True
    assert result["energy_spent"] == COLONIZATION_ENERGY_COST
    with connection() as conn:
        energy = conn.execute("SELECT SUM(energy_stored) AS e FROM pods WHERE org_id=?",
                              (oid,)).fetchone()["e"]
    assert energy == 200.0 - COLONIZATION_ENERGY_COST

def test_set_mission_colonize_refused_when_energy_short():
    """All-or-nothing, unlike every prorated cost in the economy: a ship that
    cannot pay in full is left completely untouched -- not locked, not
    partially charged, free to try again after fuelling up."""
    pid, sid, oid = _seed_colonizer(storage_current=COLONIZATION_ENERGY_COST - 1)
    result = set_mission("U_P1", oid, "colonize")
    assert "error" in result
    with connection() as conn:
        org = conn.execute("SELECT mission,is_mobile FROM organizations WHERE id=?",
                           (oid,)).fetchone()
        energy = conn.execute("SELECT SUM(energy_stored) AS e FROM pods WHERE org_id=?",
                              (oid,)).fetchone()["e"]
    assert org["mission"] != "colonize"
    assert org["is_mobile"] == 1                      # never locked
    assert energy == COLONIZATION_ENERGY_COST - 1     # never charged

# --- set_mission negative paths ---

def test_set_mission_unknown_player():
    assert "error" in set_mission("U_NOBODY", 1, "idle")

def test_set_mission_invalid_type():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    assert "error" in set_mission("U_P1", oid, "dance")

def test_set_mission_refuses_combat_rather_than_accepting_it_silently():
    """The failure that matters is not the refusal but what used to happen
    instead: the mission was written, the fleet report printed it, and the
    player waited turns on an order engine/turn.py's stubs never resolve."""
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    set_mission("U_P1", oid, "idle")
    for mission in ("defend", "attack"):
        result = set_mission("U_P1", oid, mission)
        assert result["error"] == WEAPONS_INOPERABLE
        assert "ok" not in result
    with connection() as conn:
        assert conn.execute("SELECT mission FROM organizations WHERE id=?",
                            (oid,)).fetchone()["mission"] == "idle"    # left untouched

def test_combat_missions_stay_in_the_vocabulary_they_are_refused_from():
    """Refused, not removed: a player who asks is told combat is not built
    yet, which is true, rather than shown a valid-mission list that omits it
    and reads as "this game has no combat", which is false."""
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    assert UNIMPLEMENTED_MISSIONS < VALID_ORG_MISSIONS
    unknown = set_mission("U_P1", oid, "dance")["error"]
    assert "defend" in unknown and "attack" in unknown
    assert "not implemented" in set_mission("U_P1", oid, "attack")["error"]

def test_set_mission_colony_cannot_move():
    pid, sid, oid = _seed_colonizer()
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
    with connection() as conn:
        org = conn.execute("SELECT sector_id,is_mobile,mission FROM organizations WHERE id=?",
                           (oid,)).fetchone()
        queued = conn.execute("SELECT dest_x,dest_y,dest_z FROM arrival_queue WHERE org_id=?",
                              (oid,)).fetchone()
    assert org["sector_id"] == -1          # parked at the sentinel, not left in place
    assert org["is_mobile"] == 0
    assert org["mission"] == "move"
    assert queued is not None              # a real arrival_queue row exists to resolve it
    assert (queued["dest_x"], queued["dest_y"], queued["dest_z"]) == (3, 0, 0)

def test_set_mission_move_requires_dest_params():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    result = set_mission("U_P1", oid, "move", {"dest_x": 3})
    assert "error" in result
    with connection() as conn:
        org = conn.execute("SELECT sector_id,mission FROM organizations WHERE id=?",
                           (oid,)).fetchone()
    assert org["sector_id"] != -1          # rejected before anything was mutated
    assert org["mission"] != "move"

# --- set_pod_task happy path ---

def test_set_pod_task_produce_energy():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid)
    result = set_pod_task("U_P1", pod, "produce_energy")
    assert result.get("ok") is True
    with connection() as conn:
        assert conn.execute("SELECT task FROM pods WHERE id=?",
                            (pod,)).fetchone()["task"] == "produce_energy"

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

# --- set_call_sign (the player's own name for a unit) ---

def test_set_call_sign_leaves_the_given_name_alone():
    """The whole point: a call sign is additional, not a rename. S1 is still
    S1 afterwards, which is what makes a call sign safe to change mid-game."""
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid, name="S1")
    result = set_call_sign("U_P1", oid, "Vanguard")
    assert result["ok"] is True
    assert (result["previous_call_sign"], result["call_sign"]) == (None, "Vanguard")
    assert result["name"] == "S1"
    with connection() as conn:
        row = conn.execute("SELECT name, call_sign FROM organizations WHERE id=?",
                           (oid,)).fetchone()
    assert (row["name"], row["call_sign"]) == ("S1", "Vanguard")

def test_a_call_sign_can_be_changed_again():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid, name="S1")
    set_call_sign("U_P1", oid, "Vanguard")
    result = set_call_sign("U_P1", oid, "Rearguard")
    assert (result["previous_call_sign"], result["call_sign"]) == ("Vanguard", "Rearguard")

def test_set_call_sign_rejects_a_duplicate_within_one_player():
    """An ambiguous call sign is not a call sign -- it is how a player refers
    to a unit out loud, so it must resolve to exactly one."""
    pid = seed_player(); sid = seed_sector()
    first = seed_ship(pid, sid, name="S1"); other = seed_ship(pid, sid, name="S2")
    set_call_sign("U_P1", first, "Vanguard")
    result = set_call_sign("U_P1", other, "vanguard")   # case-insensitive
    assert "error" in result and "already taken" in result["error"]

def test_a_call_sign_cannot_impersonate_another_units_given_name():
    """Calling S1 "S2" while an actual S2 exists would make every spoken
    reference ambiguous, so the two namespaces are checked together."""
    pid = seed_player(); sid = seed_sector()
    first = seed_ship(pid, sid, name="S1"); seed_ship(pid, sid, name="S2")
    assert "error" in set_call_sign("U_P1", first, "S2")

def test_set_call_sign_allows_the_same_call_sign_for_different_players():
    """Uniqueness is per player: neither can see the other's roster."""
    p1 = seed_player(); p2 = seed_player(email="b@test.com", player_token="U_P2", display_name="Two")
    sid = seed_sector()
    a = seed_ship(p1, sid, name="S1"); b = seed_ship(p2, sid, name="S1")
    assert set_call_sign("U_P1", a, "Vanguard")["ok"] is True
    assert set_call_sign("U_P2", b, "Vanguard")["ok"] is True

def test_set_call_sign_rejects_empty_and_overlong():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    assert "error" in set_call_sign("U_P1", oid, "   ")
    assert "error" in set_call_sign("U_P1", oid, "x" * 25)
    assert set_call_sign("U_P1", oid, "x" * 24)["call_sign"] == "x" * 24
    assert set_call_sign("U_P1", oid, "  Trimmed  ")["call_sign"] == "Trimmed"

def test_set_call_sign_is_ownership_gated():
    p1 = seed_player(); seed_player(email="b@test.com", player_token="U_P2", display_name="Two")
    sid = seed_sector(); oid = seed_ship(p1, sid)
    assert "error" in set_call_sign("U_P2", oid, "Stolen")

# --- queue_command param validation -----------------------------------------
# queue_command validates the full payload, not just the trigger phase, the
# action name and ownership of the ORG. The dispatchers index straight into
# the stored params dict, so an unvalidated malformed order would detonate
# inside end_of_turn() -- which is to say, inside the background clock, on
# everybody's turn at once.

def _org_with_pod(token="U_P1", email="p@t.com", task="produce_food"):
    pid = seed_player(email=email, player_token=token)
    sid = seed_sector(energy=1000.0)
    oid = seed_ship(pid, sid, name=f"Ship-{token}")
    pod = seed_pod(oid, task=task, storage_capacity=100.0, storage_current=50.0)
    return pid, oid, pod


def _pod_task(pod_id):
    with connection() as conn:
        row = conn.execute("SELECT task, task_params FROM pods WHERE id=?", (pod_id,)).fetchone()
    return row["task"], row["task_params"]


def _queued_count():
    with connection() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM org_command_queue").fetchone()["n"]
    return n


def test_queue_command_rejects_an_out_of_range_scan_aim():
    """The player's rule: out of range is an error, the pod keeps its current
    mission, and the player is told -- at the moment they gave the order, not
    silently three turns later when a background clock drops it."""
    _, oid, pod = _org_with_pod()
    result = queue_command("U_P1", oid, "at_turn", "set_pod_task",
                           {"pod_id": pod, "task": "scan",
                            "offset_x": 9, "offset_y": 0, "offset_z": 0}, turn=3)
    assert "error" in result and "scan range is 2" in result["error"]
    assert result["in_range"] is False and result["distance"] == 9.0
    assert _pod_task(pod) == ("produce_food", None)   # previous mission intact
    assert _queued_count() == 0                       # nothing queued


def test_queue_command_accepts_an_in_range_scan_aim():
    _, oid, pod = _org_with_pod()
    result = queue_command("U_P1", oid, "at_turn", "set_pod_task",
                           {"pod_id": pod, "task": "scan",
                            "offset_x": 0, "offset_y": -2, "offset_z": 0}, turn=3)
    assert result["ok"] is True
    assert _queued_count() == 1


def test_queue_command_normalizes_a_compass_bearing_to_offsets():
    """queue_command's docstring promises bearings work, but both dispatchers
    read only offset_x/y/z -- so an unresolved bearing was silently dropped at
    dispatch and the pod got the task with no aim."""
    _, oid, pod = _org_with_pod()
    queue_command("U_P1", oid, "at_turn", "set_pod_task",
                  {"pod_id": pod, "task": "scan", "bearing": "N2"}, turn=3)
    with connection() as conn:
        stored = json.loads(conn.execute(
            "SELECT params FROM org_command_queue").fetchone()["params"])
    assert (stored["offset_x"], stored["offset_y"], stored["offset_z"]) == (0, -2, 0)
    assert "bearing" not in stored


def test_queue_command_rejects_an_invalid_pod_task():
    """Previously reached the pods CHECK constraint and raised IntegrityError
    inside end_of_turn(), wedging the turn engine permanently: the queue row is
    deleted only after its handler returns, so the failing row survived the
    rollback and re-fired on every later tick."""
    _, oid, pod = _org_with_pod()
    result = queue_command("U_P1", oid, "at_turn", "set_pod_task",
                           {"pod_id": pod, "task": "become_a_dragon"}, turn=3)
    assert "error" in result and "Invalid pod task" in result["error"]
    assert _queued_count() == 0


def test_queue_command_refuses_a_pod_belonging_to_another_player():
    """queue_command verified the ORG was yours but never that the pod was.
    apply_set_pod_task documents itself as doing no ownership check ('caller's
    job') and the queued dispatcher wasn't doing that job either, so this
    retasked a rival's pod."""
    _, mine, _ = _org_with_pod(token="U_P1", email="a@t.com")
    _, _, rival_pod = _org_with_pod(token="U_P2", email="b@t.com", task="produce_goods")
    result = queue_command("U_P1", mine, "at_turn", "set_pod_task",
                           {"pod_id": rival_pod, "task": "idle"}, turn=3)
    assert "error" in result and "not part of this organization" in result["error"]
    assert _queued_count() == 0
    end_of_turn()
    assert _pod_task(rival_pod)[0] == "produce_goods"   # untouched


def test_queue_command_requires_full_destination_coordinates():
    """A missing dest_y raised KeyError inside end_of_turn(), same wedge."""
    _, oid, _ = _org_with_pod()
    result = queue_command("U_P1", oid, "at_turn", "move", {"dest_x": 3}, turn=3)
    assert "error" in result and "dest_y" in result["error"] and "dest_z" in result["error"]
    assert _queued_count() == 0


def test_queue_command_rejects_negative_destinations():
    """Matches confirm_move's own rule, which the queued path bypassed."""
    _, oid, _ = _org_with_pod()
    result = queue_command("U_P1", oid, "at_turn", "move",
                           {"dest_x": 1, "dest_y": -4, "dest_z": 0}, turn=3)
    assert "error" in result and "negative" in result["error"]
    assert _queued_count() == 0


def test_queue_command_rejects_an_aim_on_a_non_scan_task():
    _, oid, pod = _org_with_pod()
    result = queue_command("U_P1", oid, "at_turn", "set_pod_task",
                           {"pod_id": pod, "task": "produce_energy", "bearing": "N"}, turn=3)
    assert "error" in result and "Only the scan task takes an aim" in result["error"]
    assert _queued_count() == 0


def test_the_turn_engine_survives_what_used_to_wedge_it():
    """The regression that matters: none of the malformed orders above can be
    queued, so end_of_turn() keeps advancing the game for everyone."""
    _, oid, pod = _org_with_pod()
    for bad in ({"pod_id": pod, "task": "become_a_dragon"},
                {"pod_id": pod},
                {"dest_x": 3}):
        action = "move" if "dest_x" in bad else "set_pod_task"
        assert "error" in queue_command("U_P1", oid, "at_turn", action, bad, turn=0)
    with connection() as conn:
        before = conn.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()[0]
    end_of_turn()
    with connection() as conn:
        after = conn.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()[0]
    assert after == before + 1
