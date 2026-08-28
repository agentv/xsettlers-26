"""
Resource transfer between two of one player's own organizations: ordering
legality (transfer_resources), and end-of-turn resolution (engine/transfers.py,
run from engine/turn.py step 2.3, just after arrivals settle).

One subject, one file -- the tool wrapper only validates and queues; every
rule about what actually moves is in the engine helper, and only a shared file
proves the two agree.
"""
import json
from db.connection import connection, get_connection
from engine.turn import end_of_turn
from xsettlers_mcp.tools.organization_tools import transfer_resources, queue_command
from xsettlers_mcp.tools.navigation_tools import confirm_move
from npc.strategy import run_strategy, validate_strategy
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod


def _org_energy(org_id):
    with connection() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(energy_stored),0) AS e FROM pods WHERE org_id=?",
            (org_id,)).fetchone()["e"]


def _resolved_event():
    with connection() as conn:
        row = conn.execute(
            "SELECT payload FROM events WHERE event_type='transfer.resolved' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def _queue_len():
    with connection() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM transfer_queue").fetchone()["n"]


def _two_ships_together(energy_on_giver=50.0, giver_cap=100.0,
                        receiver_cap=100.0, energy_on_receiver=0.0):
    """One player, two ships in the same sector. Energy-only pods, so org
    upkeep (which also wants food) prorates to zero and never perturbs a
    holdings assertion."""
    pid = seed_player()
    sid = seed_sector(0, 0, 0)
    giver = seed_ship(pid, sid, name="Giver")
    receiver = seed_ship(pid, sid, name="Receiver")
    seed_pod(giver, storage_capacity=giver_cap, storage_current=energy_on_giver)
    seed_pod(receiver, storage_capacity=receiver_cap, storage_current=energy_on_receiver)
    return pid, giver, receiver


# --- ordering legality -------------------------------------------------------

def test_order_rejects_giver_not_owned():
    _pid, giver, receiver = _two_ships_together()
    seed_player(email="p2@test.com", player_token="U_P2", display_name="Two")
    # p2 owns neither, so it cannot name the giver.
    result = transfer_resources("U_P2", giver, receiver, "energy", 10)
    assert "error" in result


def test_order_rejects_receiver_not_owned():
    _pid, giver, receiver = _two_ships_together()
    p2 = seed_player(email="p2@test.com", player_token="U_P2", display_name="Two")
    alien = seed_ship(p2, seed_sector(0, 0, 0), name="Alien")
    result = transfer_resources("U_P1", giver, alien, "energy", 10)
    assert "not owned by you" in result["error"]


def test_order_rejects_unknown_resource():
    _pid, giver, receiver = _two_ships_together()
    result = transfer_resources("U_P1", giver, receiver, "antimatter", 10)
    assert "Invalid resource" in result["error"]


def test_order_rejects_nonpositive_amount():
    _pid, giver, receiver = _two_ships_together()
    assert "positive" in transfer_resources("U_P1", giver, receiver, "energy", 0)["error"]
    assert "positive" in transfer_resources("U_P1", giver, receiver, "energy", -5)["error"]


def test_order_rejects_transfer_to_self():
    _pid, giver, _receiver = _two_ships_together()
    assert "itself" in transfer_resources("U_P1", giver, giver, "energy", 10)["error"]


def test_order_rejects_different_sectors():
    pid = seed_player()
    giver = seed_ship(pid, seed_sector(0, 0, 0), name="Giver")
    receiver = seed_ship(pid, seed_sector(3, 0, 0), name="Receiver")
    seed_pod(giver, storage_current=20.0)
    seed_pod(receiver)
    result = transfer_resources("U_P1", giver, receiver, "energy", 10)
    assert "share a sector" in result["error"]


def test_order_rejects_giver_in_transit():
    _pid, giver, receiver = _two_ships_together()
    confirm_move("U_P1", giver, 5, 0, 0, jump_range_per_turn=1)  # parks giver at -1
    result = transfer_resources("U_P1", giver, receiver, "energy", 10)
    assert "in transit" in result["error"]


def test_order_queues_a_row_and_logs_it():
    _pid, giver, receiver = _two_ships_together()
    result = transfer_resources("U_P1", giver, receiver, "energy", 30)
    assert result["ok"] and result["resolves_at_turn"] == 1
    assert _queue_len() == 1
    with connection() as conn:
        ev = conn.execute(
            "SELECT payload FROM events WHERE event_type='transfer.ordered'").fetchone()
    assert json.loads(ev["payload"])["amount"] == 30
    # Nothing charged at order time.
    assert _org_energy(giver) == 50.0


# --- resolution ------------------------------------------------------------

def test_co_located_transfer_moves_the_resource_one_tick_later():
    _pid, giver, receiver = _two_ships_together(energy_on_giver=50.0)
    transfer_resources("U_P1", giver, receiver, "energy", 30)
    end_of_turn()
    assert _org_energy(giver) == 20.0
    assert _org_energy(receiver) == 30.0
    assert _queue_len() == 0
    ev = _resolved_event()
    assert ev["completed"] and ev["sent"] == 30.0 and ev["destroyed"] == 0.0


def test_giver_that_spent_it_down_sends_less_rather_than_being_refused():
    _pid, giver, receiver = _two_ships_together(energy_on_giver=50.0)
    transfer_resources("U_P1", giver, receiver, "energy", 40)
    # Giver spends most of it in the intervening turn.
    with connection() as conn:
        conn.execute("UPDATE pods SET energy_stored=10 WHERE org_id=?", (giver,))
    end_of_turn()
    assert _org_energy(giver) == 0.0
    assert _org_energy(receiver) == 10.0
    assert _resolved_event()["sent"] == 10.0


def test_overflow_is_destroyed_and_absent_from_the_turn_waste_figure():
    # Receiver has 10 free; a 30 transfer stores 10 and destroys 20.
    pid, giver, receiver = _two_ships_together(
        energy_on_giver=50.0, receiver_cap=100.0, energy_on_receiver=90.0)
    transfer_resources("U_P1", giver, receiver, "energy", 30)
    end_of_turn()
    assert _org_energy(giver) == 20.0
    assert _org_energy(receiver) == 100.0
    ev = _resolved_event()
    assert ev["sent"] == 30.0 and ev["stored"] == 10.0 and ev["destroyed"] == 20.0
    # before_holdings is read after this step, so the 20 destroyed never
    # reaches the ledger's derived waste.
    with connection() as conn:
        snap = conn.execute(
            "SELECT payload FROM events WHERE event_type='turn.snapshot' AND subject_id=?",
            (pid,)).fetchone()
    assert json.loads(snap["payload"])["energy_wasted"] == 0.0


def test_no_transfer_when_the_two_move_apart_before_resolution():
    _pid, giver, receiver = _two_ships_together(energy_on_giver=50.0)
    transfer_resources("U_P1", giver, receiver, "energy", 30)
    confirm_move("U_P1", receiver, 6, 0, 0, jump_range_per_turn=1)  # receiver leaves
    end_of_turn()
    assert _org_energy(giver) == 50.0        # kept everything
    assert _resolved_event()["completed"] is False


def test_resolution_runs_after_arrivals_so_a_ship_landing_this_turn_can_receive():
    """The reason resolution sits after the arrival pass: a transfer queued
    while the giver was still inbound completes the turn it lands at the
    receiver's sector. Seeded directly -- the order-time co-location check
    would block issuing this through the tool, which is exactly what a future
    queued 'upon arrival, transfer' order would sidestep."""
    pid = seed_player()
    home = seed_sector(0, 0, 0)
    dest = seed_sector(5, 0, 0)
    giver = seed_ship(pid, home, name="Giver")
    receiver = seed_ship(pid, dest, name="Receiver")
    seed_pod(giver, storage_capacity=100.0, storage_current=40.0)
    seed_pod(receiver, storage_capacity=100.0)
    with connection() as conn:
        conn.execute("UPDATE organizations SET sector_id=-1 WHERE id=?", (giver,))
        conn.execute("""INSERT INTO arrival_queue (arrival_turn, org_id, dest_x, dest_y, dest_z)
                        VALUES (1, ?, 5, 0, 0)""", (giver,))
        conn.execute("""INSERT INTO transfer_queue
            (from_org_id, to_org_id, resource, amount, ordered_turn, resolve_turn)
            VALUES (?, ?, 'energy', 25, 0, 1)""", (giver, receiver))
    end_of_turn()
    ev = _resolved_event()
    assert ev["completed"] and ev["sent"] == 25.0
    assert _org_energy(receiver) == 25.0


# --- available to NPC strategies -----------------------------------------

def test_strategy_document_may_order_an_immediate_transfer():
    pid, giver, receiver = _two_ships_together(energy_on_giver=60.0)
    # Org id order: giver is index 0, receiver index 1.
    doc = {"steps": [{"order": {
        "ships": {"slice": [0, 1]}, "action": "transfer",
        "params": {"to_index": 1, "resource": "energy", "amount": 25}}}]}
    assert validate_strategy(doc) is None
    run_strategy(pid, "U_P1", doc, {})
    end_of_turn()
    assert _org_energy(giver) == 35.0
    assert _org_energy(receiver) == 25.0


def test_strategy_queues_a_transfer_for_when_the_hauler_reaches_the_hub():
    """The tactic the user named: mine out somewhere rich, carry it back, and
    dump it on the hub the turn you dock -- expressed as a move followed by an
    upon_arrival transfer."""
    pid = seed_player()
    home = seed_sector(0, 0, 0)
    hub_sector = seed_sector(1, 0, 0)
    hauler = seed_ship(pid, home, name="Hauler")   # index 0
    hub = seed_ship(pid, hub_sector, name="Hub")   # index 1
    seed_pod(hauler, storage_capacity=100.0, storage_current=50.0)
    seed_pod(hub, storage_capacity=100.0)
    doc = {"steps": [
        {"order": {"ships": {"slice": [0, 1]}, "action": "move",
                   "params": {"d_x": 1, "d_y": 0, "d_z": 0, "jump_range_per_turn": 1}}},
        {"order": {"ships": {"slice": [0, 1]}, "action": "transfer", "when": "upon_arrival",
                   "params": {"to_index": 1, "resource": "energy", "amount": 30}}},
    ]}
    assert validate_strategy(doc) is None
    run_strategy(pid, "U_P1", doc, {})
    end_of_turn()   # hauler still inbound
    end_of_turn()   # hauler docks; the ship's log fires the queued transfer order
    end_of_turn()   # step 2.3 resolves the transfer one tick after it was ordered
    ev = _resolved_event()
    assert ev["completed"] and ev["sent"] == 30.0
    assert _org_energy(hub) == 30.0
    assert _org_energy(hauler) == 20.0


def test_strategy_validator_rejects_a_transfer_missing_its_amount():
    doc = {"steps": [{"order": {"ships": "all", "action": "transfer",
                                "params": {"to_index": 1, "resource": "energy"}}}]}
    assert "amount" in validate_strategy(doc)["error"]


def test_queue_command_transfer_requires_target_resource_and_amount():
    _pid, giver, receiver = _two_ships_together()
    confirm_move("U_P1", giver, 3, 0, 0, jump_range_per_turn=1)  # gives it a pending arrival
    result = queue_command("U_P1", giver, "upon_arrival", "transfer",
                           {"to_org_id": receiver, "resource": "energy"})
    assert "amount" in result["error"]
