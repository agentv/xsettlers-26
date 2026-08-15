import json
import pytest
from db.connection import get_connection
from engine.turn import end_of_turn
from xsettlers_mcp.tools.sector_tools import SCAN_RANGE
from xsettlers_mcp.tools.organization_tools import (
    set_mission, set_pod_task, rename_organization,
    set_org_scan_bearing, set_pod_scan_bearing, queue_command
)
from xsettlers_mcp.tools.organization_reports import (
    show_civilization_status, show_game_status, show_organization
)
from engine.production import (POD_PRODUCTION, COLONY_PRODUCTION_MULTIPLIER,
                               COLONIZATION_ENERGY_COST)
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
    conn = get_connection()
    org = conn.execute("SELECT org_type,is_mobile,mission FROM organizations WHERE id=?",
                       (oid,)).fetchone()
    conn.close()
    assert org["org_type"] == "ship"      # not flipped yet -- only at resolution
    assert org["is_mobile"] == 0          # locked immediately, per set_mission
    assert org["mission"] == "colonize"

def test_set_mission_colonize_converts_after_three_turns():
    pid, sid, oid = _seed_colonizer()
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

def test_set_mission_colonize_charges_energy_up_front():
    """The charge lands at commitment, not at resolution three turns later."""
    pid, sid, oid = _seed_colonizer(storage_current=200.0)
    result = set_mission("U_P1", oid, "colonize")
    assert result.get("ok") is True
    assert result["energy_spent"] == COLONIZATION_ENERGY_COST
    conn = get_connection()
    energy = conn.execute("SELECT SUM(energy_stored) AS e FROM pods WHERE org_id=?",
                          (oid,)).fetchone()["e"]
    conn.close()
    assert energy == 200.0 - COLONIZATION_ENERGY_COST

def test_set_mission_colonize_refused_when_energy_short():
    """All-or-nothing, unlike every prorated cost in the economy: a ship that
    cannot pay in full is left completely untouched -- not locked, not
    partially charged, free to try again after fuelling up."""
    pid, sid, oid = _seed_colonizer(storage_current=COLONIZATION_ENERGY_COST - 1)
    result = set_mission("U_P1", oid, "colonize")
    assert "error" in result
    conn = get_connection()
    org = conn.execute("SELECT mission,is_mobile FROM organizations WHERE id=?",
                       (oid,)).fetchone()
    energy = conn.execute("SELECT SUM(energy_stored) AS e FROM pods WHERE org_id=?",
                          (oid,)).fetchone()["e"]
    conn.close()
    assert org["mission"] != "colonize"
    assert org["is_mobile"] == 1                      # never locked
    assert energy == COLONIZATION_ENERGY_COST - 1     # never charged

def test_colony_outproduces_ship_at_the_multiplier():
    """A colony and a ship with identical pods on the same sector: same costs
    paid, COLONY_PRODUCTION_MULTIPLIER times the output. Before this existed
    the two were mechanically identical and colonizing bought nothing."""
    pid = seed_player(); sid = seed_sector(energy=100000.0)
    conn = get_connection()
    orgs = {}
    for org_type, is_mobile in (("ship", 1), ("colony", 0)):
        conn.execute("""INSERT INTO organizations (org_type,name,player_id,sector_id,
                        is_mobile,mission) VALUES (?,?,?,?,?,'idle')""",
                     (org_type, org_type.upper(), pid, sid, is_mobile))
        orgs[org_type] = conn.execute(
            "SELECT id FROM organizations WHERE name=?", (org_type.upper(),)).fetchone()["id"]
    conn.commit(); conn.close()
    for oid in orgs.values():
        seed_pod(oid, task="produce_energy", storage_capacity=10000.0, storage_current=500.0)
        pod = seed_pod(oid, task="produce_food", storage_capacity=10000.0, storage_current=500.0)
        # produce_food's recipe costs goods as well as energy, and nothing here
        # makes goods -- without a stock on hand the ratio is 0 and neither org
        # produces any food at all, which would make the comparison vacuous.
        conn = get_connection()
        conn.execute("UPDATE pods SET goods_stored=500.0 WHERE id=?", (pod,))
        conn.commit(); conn.close()

    def energy_and_food(oid):
        conn = get_connection()
        row = conn.execute("""SELECT SUM(energy_stored) AS e, SUM(food_stored) AS f
                              FROM pods WHERE org_id=?""", (oid,)).fetchone()
        conn.close(); return row["e"], row["f"]

    before = {k: energy_and_food(v) for k, v in orgs.items()}
    end_of_turn()
    after = {k: energy_and_food(v) for k, v in orgs.items()}

    # Gross production, backed out of the net change: both paid identical
    # costs (upkeep + recipes are untouched by org type), so whatever extra
    # the colony holds is the bonus and nothing else.
    ship_energy_gain = after["ship"][0] - before["ship"][0]
    colony_energy_gain = after["colony"][0] - before["colony"][0]
    base_energy = POD_PRODUCTION["produce_energy"]["energy"]
    assert colony_energy_gain - ship_energy_gain == pytest.approx(
        base_energy * (COLONY_PRODUCTION_MULTIPLIER - 1))

    base_food = POD_PRODUCTION["produce_food"]["food"]
    assert (after["colony"][1] - before["colony"][1]) - (after["ship"][1] - before["ship"][1]) \
        == pytest.approx(base_food * (COLONY_PRODUCTION_MULTIPLIER - 1))

def test_colony_strips_its_sector_faster_than_a_ship():
    """The bonus applies to the sector draw too, not just to what lands in
    storage -- a colony's advantage burns through the ground it stands on
    proportionally faster, so the reward carries its own clock."""
    pid = seed_player()
    ship_sector = seed_sector(x=1, energy=1000.0)
    colony_sector = seed_sector(x=2, energy=1000.0)
    conn = get_connection()
    for org_type, is_mobile, sec in (("ship", 1, ship_sector), ("colony", 0, colony_sector)):
        conn.execute("""INSERT INTO organizations (org_type,name,player_id,sector_id,
                        is_mobile,mission) VALUES (?,?,?,?,?,'idle')""",
                     (org_type, org_type.upper(), pid, sec, is_mobile))
    conn.commit()
    for name in ("SHIP", "COLONY"):
        oid = conn.execute("SELECT id FROM organizations WHERE name=?", (name,)).fetchone()["id"]
        seed_pod(oid, task="produce_energy", storage_capacity=10000.0, storage_current=500.0)
        seed_pod(oid, task="produce_food", storage_capacity=10000.0, storage_current=500.0)
    conn.close()
    end_of_turn()
    conn = get_connection()
    drawn = {}
    for label, sec in (("ship", ship_sector), ("colony", colony_sector)):
        remaining = conn.execute("SELECT energy_capacity AS e FROM sectors WHERE id=?",
                                 (sec,)).fetchone()["e"]
        drawn[label] = 1000.0 - remaining
    conn.close()
    assert drawn["colony"] == pytest.approx(drawn["ship"] * COLONY_PRODUCTION_MULTIPLIER)

# --- set_mission negative paths ---

def test_set_mission_unknown_player():
    assert "error" in set_mission("U_NOBODY", 1, "idle")

def test_set_mission_invalid_type():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    assert "error" in set_mission("U_P1", oid, "dance")

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
    # 99 - 1 org upkeep = 98, + 5 produced = 103, capped back to 100.
    pod = seed_pod(oid, storage_capacity=100.0, storage_current=99.0)
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
    assert sector["energy_capacity"] == 44.0  # 50 - flat rate 6
    assert pod_row["energy_stored"] == 6.0

def test_production_floors_at_zero_and_stops_once_depleted():
    pid = seed_player(); sid = seed_sector(energy=2.0)  # less than the flat rate (6)
    oid = seed_ship(pid, sid)
    pod = seed_pod(oid, storage_capacity=100.0, storage_current=0.0)
    seed_pod(oid, task="produce_food", storage_current=100.0)
    set_pod_task("U_P1", pod, "produce_energy"); end_of_turn()
    conn = get_connection()
    sector = conn.execute("SELECT energy_capacity FROM sectors WHERE id=?", (sid,)).fetchone()
    pod_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()
    conn.close()
    assert sector["energy_capacity"] == 0.0
    assert pod_row["energy_stored"] == 2.0  # capped by what the sector had left
    end_of_turn()  # sector is now empty -- no further production gain, and org
                   # upkeep (3 energy/turn) exhausts the 2.0 banked, prorated
    conn = get_connection()
    pod_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()
    conn.close()
    assert pod_row["energy_stored"] == 0.0

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
    # Org upkeep (3 energy/turn) runs first and drains 3 from full_pod (the
    # only energy source), opening 3 units of free space there; production
    # then makes 6 more, refills full_pod's freed units first (back to its
    # 10 capacity), and the remaining 3 spills into empty_pod.
    assert full_row["energy_stored"] == 10.0  # topped back up to its own capacity
    assert empty_row["energy_stored"] == 3.0  # the rest spilled over here

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
    seed_pod(oid, task="produce_energy", storage_current=100.0)   # scanning costs energy
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
    seed_pod(oid, task="produce_energy", storage_current=200.0)   # scanning costs energy
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

# --- show_organization's scanner summary (see _scanner_summary) ---

def test_show_organization_reports_no_scanners_when_none_are_active():
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    result = show_organization("U_P1", oid)
    assert result["scanners"] == []
    assert "footer" not in result["display"]

def test_show_organization_lists_the_orgs_own_aimed_sensors():
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    set_org_scan_bearing("U_P1", oid, "N")
    result = show_organization("U_P1", oid)
    assert result["scanners"] == [{"source": "sensors", "bearing": "N", "aimed": True}]
    assert result["display"]["footer"] == "Scans: North"

def test_show_organization_lists_multiple_aimed_scan_pods_and_sensors():
    """The user's own example: three scanners aimed at three different
    bearings should read back as one comma-joined footer line."""
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    set_org_scan_bearing("U_P1", oid, "N")
    p1 = seed_pod(oid, task="scan")
    p2 = seed_pod(oid, task="scan")
    set_pod_scan_bearing("U_P1", p1, "S")
    set_pod_scan_bearing("U_P1", p2, "SE")
    result = show_organization("U_P1", oid)
    assert [s["bearing"] for s in result["scanners"]] == ["N", "S", "SE"]
    assert result["display"]["footer"] == "Scans: North, South, Southeast"

def test_show_organization_flags_an_unaimed_scan_pod_without_dropping_it():
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    pod_id = seed_pod(oid, task="scan")
    result = show_organization("U_P1", oid)
    assert result["scanners"] == [{"source": f"pod {pod_id}", "bearing": None, "aimed": False}]
    assert result["display"]["footer"] == "Scans: none aimed (+1 unaimed)"

def test_show_organization_notes_unaimed_pods_alongside_aimed_ones():
    pid = seed_player(); sid = seed_sector(0, 0, 0); oid = seed_ship(pid, sid)
    set_org_scan_bearing("U_P1", oid, "W")
    seed_pod(oid, task="scan")
    result = show_organization("U_P1", oid)
    assert result["display"]["footer"] == "Scans: West (+1 unaimed)"


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
    conn = get_connection()
    row = conn.execute("SELECT task, task_params FROM pods WHERE id=?", (pod_id,)).fetchone()
    conn.close()
    return row["task"], row["task_params"]


def _queued_count():
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) n FROM org_command_queue").fetchone()["n"]
    conn.close()
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
    conn = get_connection()
    stored = json.loads(conn.execute(
        "SELECT params FROM org_command_queue").fetchone()["params"])
    conn.close()
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
    conn = get_connection()
    before = conn.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()[0]
    conn.close()
    end_of_turn()
    conn = get_connection()
    after = conn.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()[0]
    conn.close()
    assert after == before + 1
