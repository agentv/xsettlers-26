"""
The resource economy: what a pod produces, what it costs to run, what the
sector can supply, and where output goes when storage is full.

Exercises engine/turn.py's production pass together with
engine/org_resources.py's pooling rules -- the two only make sense read
together, since production is prorated by what the org's pooled stock can pay
for and then spilled across pods by the same pooling.
"""
import pytest
from db.connection import connection, get_connection
from engine.turn import end_of_turn
from xsettlers_mcp.tools.organization_tools import set_pod_task
from engine.production import POD_PRODUCTION, COLONY_PRODUCTION_MULTIPLIER
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod


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
        with connection() as conn:
            conn.execute("UPDATE pods SET goods_stored=500.0 WHERE id=?", (pod,))

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

def test_storage_capped_at_capacity():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    # 99 - 1 org upkeep = 98, + 5 produced = 103, capped back to 100.
    pod = seed_pod(oid, storage_capacity=100.0, storage_current=99.0)
    seed_pod(oid, task="produce_food", storage_current=100.0)
    set_pod_task("U_P1", pod, "produce_energy"); end_of_turn()
    with connection() as conn:
        assert conn.execute("SELECT energy_stored FROM pods WHERE id=?",
                            (pod,)).fetchone()["energy_stored"] == 100.0

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
    with connection() as conn:
        assert conn.execute("SELECT goods_stored FROM pods WHERE id=?",
                            (goods_pod,)).fetchone()["goods_stored"] > 0.0

def test_production_depletes_sector_capacity():
    pid = seed_player(); sid = seed_sector(energy=50.0)
    oid = seed_ship(pid, sid)
    pod = seed_pod(oid, storage_capacity=100.0, storage_current=0.0)
    seed_pod(oid, task="produce_food", storage_current=100.0)
    set_pod_task("U_P1", pod, "produce_energy"); end_of_turn()
    with connection() as conn:
        sector = conn.execute("SELECT energy_capacity FROM sectors WHERE id=?", (sid,)).fetchone()
        pod_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()
    assert sector["energy_capacity"] == 46.0  # 50 - flat rate 4
    assert pod_row["energy_stored"] == 4.0

def test_production_floors_at_zero_and_stops_once_depleted():
    pid = seed_player(); sid = seed_sector(energy=2.0)  # less than the flat rate (6)
    oid = seed_ship(pid, sid)
    pod = seed_pod(oid, storage_capacity=100.0, storage_current=0.0)
    seed_pod(oid, task="produce_food", storage_current=100.0)
    set_pod_task("U_P1", pod, "produce_energy"); end_of_turn()
    with connection() as conn:
        sector = conn.execute("SELECT energy_capacity FROM sectors WHERE id=?", (sid,)).fetchone()
        pod_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()
    assert sector["energy_capacity"] == 0.0
    assert pod_row["energy_stored"] == 2.0  # capped by what the sector had left
    end_of_turn()  # sector is now empty -- no further production gain, and org
                   # upkeep (3 energy/turn) exhausts the 2.0 banked, prorated
    with connection() as conn:
        pod_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()
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
    with connection() as conn:
        pod_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()
    assert pod_row["energy_stored"] == 0.0

def test_retasking_a_pod_does_not_clear_its_storage():
    """The bug this whole model exists to fix: a pod's stored resources are
    real inventory, not a label derived from its current mission. Retasking
    must never wipe or hide what's already there."""
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid, task="produce_energy", storage_current=42.0)
    set_pod_task("U_P1", pod, "idle")
    with connection() as conn:
        energy = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (pod,)).fetchone()["energy_stored"]
    assert energy == 42.0  # untouched by the retask

def test_retasked_pods_energy_still_counts_toward_org_pool():
    """The original bug: idling an org's only energy-holding pods made their
    stored energy invisible to consumption, even though it physically
    remained. Now it must still be available to pay other pods' recipes."""
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    energy_pod = seed_pod(oid, task="produce_energy", storage_current=100.0)
    set_pod_task("U_P1", energy_pod, "idle")  # retask away from produce_energy
    goods_pod = seed_pod(oid, storage_capacity=100.0, storage_current=0.0)
    set_pod_task("U_P1", goods_pod, "produce_goods")  # needs 4 energy + 1 food
    # Idled too, and for the same reason it is here at all: it banks the food
    # the goods recipe needs. Left producing it would consume a goods per turn
    # -- exactly what produce_goods makes -- and the org's goods would net to
    # zero whether or not the recipe was ever paid, which is the thing under
    # test.
    food_pod = seed_pod(oid, task="produce_food", storage_current=100.0)
    set_pod_task("U_P1", food_pod, "idle")
    end_of_turn()
    with connection() as conn:
        goods = conn.execute("SELECT goods_stored FROM pods WHERE id=?", (goods_pod,)).fetchone()["goods_stored"]
    assert goods > 0.0  # produced fine, drawing on the now-idle pod's banked energy

def test_production_overflow_spills_into_sibling_pod():
    """A producing pod already full should spill its output into another pod
    in the same org with free space, rather than losing it."""
    pid = seed_player(); sid = seed_sector(energy=1000.0); oid = seed_ship(pid, sid)
    full_pod = seed_pod(oid, task="produce_energy", storage_capacity=10.0, storage_current=10.0)
    empty_pod = seed_pod(oid, storage_capacity=100.0, storage_current=0.0)  # idle, all free space
    seed_pod(oid, task="produce_food", storage_current=100.0)
    end_of_turn()
    with connection() as conn:
        full_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (full_pod,)).fetchone()
        empty_row = conn.execute("SELECT energy_stored FROM pods WHERE id=?", (empty_pod,)).fetchone()
    # Org upkeep (3 energy/turn) runs first and drains 3 from full_pod (the
    # only energy source), opening 3 units of free space there; production
    # then makes 4 more, refills full_pod's freed units first (back to its
    # 10 capacity), and the remaining 1 spills into empty_pod.
    assert full_row["energy_stored"] == 10.0  # topped back up to its own capacity
    assert empty_row["energy_stored"] == 1.0  # the rest spilled over here

def test_production_overflow_lost_when_org_fully_saturated():
    """If every pod in the org is completely full, excess production is
    simply lost -- not stored anywhere, not an error."""
    pid = seed_player(); sid = seed_sector(energy=1000.0); oid = seed_ship(pid, sid)
    full_energy_pod = seed_pod(oid, task="produce_energy", storage_capacity=10.0, storage_current=10.0)
    full_food_pod = seed_pod(oid, task="produce_food", storage_capacity=10.0, storage_current=10.0)
    end_of_turn()
    with connection() as conn:
        row = conn.execute("SELECT energy_stored,food_stored,goods_stored FROM pods WHERE id=?",
                           (full_energy_pod,)).fetchone()
    # nothing to spill into (only other pod in the org is also full) -- the
    # 10 units this pod would have produced (capped by its 1-food cost being
    # affordable) are simply lost, not stored anywhere or raising an error
    assert row["energy_stored"] == 10.0

def test_org_upkeep_drains_pooled_food_and_energy():
    """Every org costs 5 food + 1 energy per turn just to exist, drawn from
    its own pooled stock -- on top of whatever its individual pods cost.
    Sector energy is seeded at 0 so the energy pod can't refill itself via
    its own production this same turn -- isolates the upkeep drain, since
    otherwise production would immediately mask it back up to capacity."""
    pid = seed_player(); sid = seed_sector(energy=0.0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_food", storage_current=100.0)
    seed_pod(oid, task="produce_energy", storage_current=100.0)
    end_of_turn()
    with connection() as conn:
        food = conn.execute("SELECT SUM(food_stored) s FROM pods WHERE org_id=?", (oid,)).fetchone()["s"]
        energy = conn.execute("SELECT SUM(energy_stored) s FROM pods WHERE org_id=?", (oid,)).fetchone()["s"]
    assert food == 95.0    # 100 - 5 upkeep (no producing pod there to add any back)
    assert energy == 97.0  # 100 - 3 upkeep

def test_org_upkeep_prorated_when_insufficient():
    """Only 2 food and 0 energy on hand against a 5-food/1-energy cost --
    upkeep should prorate to the most restrictive resource (energy, at 0),
    draining nothing rather than going negative or draining food anyway."""
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_food", storage_current=2.0)
    end_of_turn()
    with connection() as conn:
        food = conn.execute("SELECT SUM(food_stored) s FROM pods WHERE org_id=?", (oid,)).fetchone()["s"]
    assert food == 2.0  # ratio floored at 0 (no energy at all) -- no partial drain either

def test_idle_pod_costs_nothing():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    pod = seed_pod(oid, task="idle", storage_current=42.0)
    end_of_turn()
    with connection() as conn:
        assert conn.execute("SELECT energy_stored FROM pods WHERE id=?",
                            (pod,)).fetchone()["energy_stored"] == 42.0
