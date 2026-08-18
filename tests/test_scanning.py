"""
Scanning, end to end: aiming a scanner, what a legal aim is, and what
resolution does with it at end of turn.

One file because scanning is one subject. An org's innate sensors and a pod on
the scan task follow identical rules -- same cost, same range, same relative
aiming, same suppression in transit -- so the tests that would prove those two
agree belong side by side rather than in an organization file and a sector
file that never get read together.
"""
import json
import pytest
from db.connection import get_connection
from db.sectors import CONFIDENCE_DECAY_PER_TURN
from engine.bearings import SCAN_RANGE, get_scan_range
from engine.turn import end_of_turn
from xsettlers_mcp.tools.organization_tools import (
    set_pod_task, set_org_scan_bearing, set_pod_scan_bearing)
from xsettlers_mcp.tools.organization_reports import show_organization
from xsettlers_mcp.tools.navigation_tools import confirm_move
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod


def _scanner_with_stock(offset=(5, 0, 0), as_pod=False):
    """An org that can comfortably afford upkeep and a scan, aimed out of
    range. Returns (player_id, org_id, pod_id)."""
    pid = seed_player(); sid = seed_sector(energy=1000.0); oid = seed_ship(pid, sid)
    pod = seed_pod(oid, task="scan" if as_pod else "idle", storage_capacity=200.0)
    conn = get_connection()
    conn.execute("UPDATE pods SET food_stored=100, energy_stored=100 WHERE id=?", (pod,))
    if as_pod:
        conn.execute("UPDATE pods SET task_params=? WHERE id=?",
                     (json.dumps({"offset_x": offset[0], "offset_y": offset[1],
                                  "offset_z": offset[2]}), pod))
    else:
        conn.execute("""UPDATE organizations
            SET scan_offset_x=?, scan_offset_y=?, scan_offset_z=? WHERE id=?""",
            (*offset, oid))
    conn.commit(); conn.close()
    return pid, oid, pod


def _alert_rows():
    conn = get_connection()
    rows = [dict(r) for r in conn.execute(
        """SELECT subject_id, subject_type, actor_id, payload FROM events
           WHERE event_type='alert.scan_out_of_range' ORDER BY id""").fetchall()]
    conn.close()
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    return rows


def _sector_exists(x, y, z):
    conn = get_connection()
    row = conn.execute("SELECT id FROM sectors WHERE coord_x=? AND coord_y=? AND coord_z=?",
                       (x, y, z)).fetchone()
    conn.close()
    return row is not None


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


def test_scan_within_range_reveals_target():
    """Confidence is checked here too (not just sector existence): scan
    resolution (step 3c of end_of_turn()) stamps confidence=100, but fog
    decay (step 5) runs later in the same pass and immediately decays it
    since the scanned sector isn't occupied by any of the player's orgs --
    pre-existing engine behavior, not something reveal_sector changes."""
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, task="scan")
    seed_pod(sid, task="produce_food", storage_current=100.0)
    seed_pod(sid, task="produce_energy", storage_current=100.0)   # scanning costs energy
    r = get_scan_range(sid)
    set_pod_task("U_P1", pod, "scan", offset_x=r, offset_y=0, offset_z=0)
    end_of_turn()
    conn = get_connection()
    sector = conn.execute(
        "SELECT id FROM sectors WHERE coord_x=? AND coord_y=0 AND coord_z=0", (r,)).fetchone()
    assert sector is not None
    ps = conn.execute("SELECT confidence FROM player_sectors WHERE player_id=? AND sector_id=?",
                      (pid, sector["id"])).fetchone()
    assert ps["confidence"] == 100 - CONFIDENCE_DECAY_PER_TURN
    conn.close()

def test_scan_out_of_range_aim_is_rejected_at_set_time():
    """Aim is an offset, so its range is fixed and knowable when set -- an
    illegal aim can never become legal, so it is refused outright rather than
    accepted and left to fail silently at resolution."""
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, task="scan")
    seed_pod(sid, task="produce_food", storage_current=100.0)
    seed_pod(sid, task="produce_energy", storage_current=100.0)   # scanning costs energy
    out_of_range = get_scan_range(sid) + 5
    result = set_pod_task("U_P1", pod, "scan", offset_x=out_of_range, offset_y=0, offset_z=0)
    assert "error" in result and result["in_range"] is False
    end_of_turn()
    conn = get_connection()
    sector = conn.execute(
        "SELECT id FROM sectors WHERE coord_x=? AND coord_y=0 AND coord_z=0",
        (out_of_range,)).fetchone()
    conn.close()
    assert sector is None

def test_rescanning_known_sector_refreshes_confidence_without_altering_resources():
    """Re-scanning an already-revealed sector must not re-randomize its
    resources (reveal_sector is idempotent -- it's a fresh look at whatever
    is currently there, not a reset), and should refresh the player's
    confidence even if it had decayed to near-zero in the meantime."""
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, task="scan")
    seed_pod(sid, task="produce_food", storage_current=100.0)
    seed_pod(sid, task="produce_energy", storage_current=100.0)   # scanning costs energy
    set_pod_task("U_P1", pod, "scan", bearing="E")
    end_of_turn()  # first scan: reveals the sector, stamps then decays confidence

    conn = get_connection()
    sector = conn.execute(
        "SELECT id, energy_capacity FROM sectors WHERE coord_x=1 AND coord_y=0 AND coord_z=0").fetchone()
    original_capacity = sector["energy_capacity"]
    # simulate it having decayed further, e.g. several unoccupied turns since
    conn.execute("UPDATE player_sectors SET confidence=5 WHERE player_id=? AND sector_id=?",
                 (pid, sector["id"]))
    conn.commit(); conn.close()

    set_pod_task("U_P1", pod, "scan", bearing="E")  # re-scan same target
    end_of_turn()

    conn = get_connection()
    resector = conn.execute("SELECT energy_capacity FROM sectors WHERE id=?", (sector["id"],)).fetchone()
    ps = conn.execute("SELECT confidence FROM player_sectors WHERE player_id=? AND sector_id=?",
                      (pid, sector["id"])).fetchone()
    conn.close()
    assert resector["energy_capacity"] == original_capacity  # not re-randomized
    # stamped back to 100 by the re-scan, then decays once more within this
    # same end_of_turn() pass since the org still doesn't occupy it
    assert ps["confidence"] == 100 - CONFIDENCE_DECAY_PER_TURN

def test_scan_in_transit_still_costs_food_but_reveals_nothing():
    """Now that out-of-range aims are refused at set time, transit is the one
    remaining case where a scan pays and returns nothing: the reveal is
    suppressed while the ship has no position, but the food is still drawn.
    Tracked as a known gap in docs/TODO.md -- pinned here so a fix to the
    cost side is a deliberate change rather than an accident."""
    from xsettlers_mcp.tools.navigation_tools import confirm_move
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, task="scan")
    seed_pod(sid, task="produce_food", storage_current=100.0)
    seed_pod(sid, task="produce_energy", storage_current=100.0)   # scanning costs energy
    set_pod_task("U_P1", pod, "scan", bearing="E")
    confirm_move("U_P1", sid, 4, 0, 0)          # in transit at end of turn
    end_of_turn()
    conn = get_connection()
    food = conn.execute("SELECT SUM(food_stored) s FROM pods WHERE org_id=?", (sid,)).fetchone()["s"]
    revealed = conn.execute("SELECT COUNT(*) n FROM sectors WHERE id != -1").fetchone()["n"]
    conn.close()
    assert food < 100.0        # paid for it
    assert revealed == 1       # only the origin sector -- nothing was scanned
def test_org_scan_out_of_range_alerts_and_reveals_nothing():
    pid, oid, _ = _scanner_with_stock()
    end_of_turn()
    alerts = _alert_rows()
    assert len(alerts) == 1
    a = alerts[0]
    assert a["subject_type"] == "organization" and a["subject_id"] == oid
    assert a["actor_id"] == pid
    assert a["payload"] == {"org_id": oid, "target_x": 5, "target_y": 0, "target_z": 0,
                            "distance": 5.0, "range": 2}
    assert not _sector_exists(5, 0, 0), "an overreaching aim must not reveal its target"


def test_pod_scan_out_of_range_alerts_against_the_pod_not_the_org():
    """Same rule, different subject: the alert names the pod that overreached,
    and carries pod_id alongside org_id so a client can say which one."""
    pid, oid, pod = _scanner_with_stock(as_pod=True)
    end_of_turn()
    alerts = _alert_rows()
    assert len(alerts) == 1
    a = alerts[0]
    assert a["subject_type"] == "pod" and a["subject_id"] == pod
    assert a["actor_id"] == pid
    assert a["payload"] == {"pod_id": pod, "org_id": oid,
                            "target_x": 5, "target_y": 0, "target_z": 0,
                            "distance": 5.0, "range": 2}
    assert not _sector_exists(5, 0, 0)


def test_out_of_range_scan_still_pays_its_cost():
    """Cost is charged before the range check -- an overreaching scanner
    wastes its food and energy exactly like an unaimed one does."""
    _, oid, pod = _scanner_with_stock()
    conn = get_connection()
    before = conn.execute("SELECT food_stored, energy_stored FROM pods WHERE id=?",
                          (pod,)).fetchone()
    conn.close()
    end_of_turn()
    conn = get_connection()
    after = conn.execute("SELECT food_stored, energy_stored FROM pods WHERE id=?",
                         (pod,)).fetchone()
    conn.close()
    # upkeep (5 food, 3 energy) + scan (1 food, 2 energy)
    assert before["food_stored"] - after["food_stored"] == 6.0
    assert before["energy_stored"] - after["energy_stored"] == 5.0


# --- Detection: a scan reveals who is standing there, not just what is there ---

def _two_players():
    watcher = seed_player(email="watcher@t.com", player_token="U_WATCH")
    rival = seed_player(email="rival@t.com", player_token="U_RIVAL")
    return watcher, rival


def _fuelled_ship(player_id, sector_id, name):
    """A ship that can actually afford to look. Scanning costs 1 food + 2
    energy a turn (POD_CONSUMPTION_RECIPE), and a starved org pays the cost
    without revealing anything -- so a detection test on an empty ship would
    pass or fail for the wrong reason."""
    ship = seed_ship(player_id, sector_id, name=name)
    seed_pod(ship, task="produce_energy", storage_current=100.0)
    seed_pod(ship, task="produce_food", storage_current=100.0)
    return ship


def _scan_east_from(token, ship_id):
    """Aim a ship's innate sensors two sectors east and resolve the turn."""
    set_org_scan_bearing(token, ship_id, bearing="E2")
    end_of_turn()


def test_scan_records_a_sighting_of_a_rival_in_the_scanned_sector():
    """The point of the whole mechanism: looking at a sector tells you who is
    in it, not only what it holds."""
    watcher, rival = _two_players()
    home = seed_sector(0, 0, 0)
    target = seed_sector(2, 0, 0)
    watcher_ship = _fuelled_ship(watcher, home, "Watcher")
    rival_ship = seed_ship(rival, target, name="Quarry")
    _scan_east_from("U_WATCH", watcher_ship)

    conn = get_connection()
    rows = conn.execute("SELECT * FROM org_sightings WHERE observer_id=?",
                        (watcher,)).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["org_id"] == rival_ship
    assert rows[0]["owner_id"] == rival
    assert rows[0]["sector_id"] == target
    assert rows[0]["org_type"] == "ship"


def test_a_scan_does_not_sight_your_own_organizations():
    """A self-sighting would draw a rival marker on your own sector."""
    watcher, _ = _two_players()
    home = seed_sector(0, 0, 0)
    target = seed_sector(2, 0, 0)
    watcher_ship = _fuelled_ship(watcher, home, "Watcher")
    seed_ship(watcher, target, name="Mine")
    _scan_east_from("U_WATCH", watcher_ship)

    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) AS n FROM org_sightings").fetchone()["n"]
    conn.close()
    assert n == 0


def test_a_sighting_is_dated_and_survives_the_rival_moving_on():
    """A sighting is an observation, not a tracker. The rival leaving does not
    un-happen the look -- the record keeps saying where it was and when."""
    watcher, rival = _two_players()
    home = seed_sector(0, 0, 0)
    target = seed_sector(2, 0, 0)
    elsewhere = seed_sector(9, 9, 0)
    watcher_ship = _fuelled_ship(watcher, home, "Watcher")
    rival_ship = seed_ship(rival, target, name="Quarry")
    _scan_east_from("U_WATCH", watcher_ship)

    conn = get_connection()
    seen_turn = conn.execute("SELECT seen_at_turn FROM org_sightings WHERE org_id=?",
                             (rival_ship,)).fetchone()["seen_at_turn"]
    conn.execute("UPDATE organizations SET sector_id=? WHERE id=?", (elsewhere, rival_ship))
    conn.commit()
    row = conn.execute("SELECT sector_id, seen_at_turn FROM org_sightings WHERE org_id=?",
                       (rival_ship,)).fetchone()
    conn.close()
    assert row["sector_id"] == target        # where it was seen, not where it is
    assert row["seen_at_turn"] == seen_turn


def test_re_sighting_moves_the_record_rather_than_appending_to_it():
    """One row per (observer, org): the engine has no use for a full track."""
    watcher, rival = _two_players()
    home = seed_sector(0, 0, 0)
    target = seed_sector(2, 0, 0)
    watcher_ship = _fuelled_ship(watcher, home, "Watcher")
    rival_ship = seed_ship(rival, target, name="Quarry")
    _scan_east_from("U_WATCH", watcher_ship)
    end_of_turn()   # the aim persists, so it scans the same sector again

    conn = get_connection()
    rows = conn.execute("SELECT * FROM org_sightings WHERE org_id=?", (rival_ship,)).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["seen_at_turn"] >= 1     # updated to the later look


def test_scan_contact_event_names_what_was_detected():
    """Contact is worth an event -- it is the first thing in this game that one
    player learns about another."""
    watcher, rival = _two_players()
    home = seed_sector(0, 0, 0)
    target = seed_sector(2, 0, 0)
    watcher_ship = _fuelled_ship(watcher, home, "Watcher")
    rival_ship = seed_ship(rival, target, name="Quarry")
    _scan_east_from("U_WATCH", watcher_ship)

    conn = get_connection()
    row = conn.execute("""SELECT payload FROM events WHERE event_type='scan.contact'
                          AND actor_id=?""", (watcher,)).fetchone()
    conn.close()
    assert row is not None
    payload = json.loads(row["payload"])
    assert payload["target_x"] == 2 and payload["target_y"] == 0
    assert [d["org_id"] for d in payload["detected"]] == [rival_ship]


def test_detection_threshold_of_six_of_six_always_detects():
    """The die is rolled even at certainty, so lowering the threshold later
    changes the odds without changing when anything is rolled."""
    from db.sightings import DETECTION_DIE_SIDES, DETECTION_THRESHOLD, roll_detection
    assert (DETECTION_THRESHOLD, DETECTION_DIE_SIDES) == (6, 6)
    assert all(roll_detection() for _ in range(500))


def test_a_lowered_threshold_lets_some_organizations_go_unnoticed(monkeypatch):
    """The knob the threshold exists to be. At 0 of 6 nothing is ever seen,
    which proves the roll actually gates the record rather than decorating it."""
    import db.sightings as sightings
    monkeypatch.setattr(sightings, "DETECTION_THRESHOLD", 0)
    watcher, rival = _two_players()
    home = seed_sector(0, 0, 0)
    target = seed_sector(2, 0, 0)
    watcher_ship = _fuelled_ship(watcher, home, "Watcher")
    seed_ship(rival, target, name="Quarry")
    _scan_east_from("U_WATCH", watcher_ship)

    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) AS n FROM org_sightings").fetchone()["n"]
    conn.close()
    assert n == 0


def test_a_look_that_finds_nothing_clears_what_you_believed_was_there():
    """Intel is per sector and refreshed by looking. A rival that has moved on
    must stop being reported the next time anyone checks -- otherwise a player's
    map accumulates ghosts that only fog-of-war decay can ever remove."""
    watcher, rival = _two_players()
    home = seed_sector(0, 0, 0)
    target = seed_sector(2, 0, 0)
    watcher_ship = _fuelled_ship(watcher, home, "Watcher")
    rival_ship = seed_ship(rival, target, name="Quarry")
    _scan_east_from("U_WATCH", watcher_ship)

    conn = get_connection()
    assert conn.execute("SELECT COUNT(*) AS n FROM org_sightings").fetchone()["n"] == 1
    # The rival departs; the aim persists, so next turn looks at the same sector.
    conn.execute("UPDATE organizations SET sector_id=-1 WHERE id=?", (rival_ship,))
    conn.commit(); conn.close()
    end_of_turn()

    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) AS n FROM org_sightings").fetchone()["n"]
    conn.close()
    assert n == 0


def test_a_sighting_leaves_the_map_when_its_sector_blinks_out():
    """Sightings age on the sector's own fog-of-war schedule rather than a
    second timer -- at confidence 0 the sector is gone and takes what you knew
    about its occupants with it."""
    from xsettlers_mcp.tools.sector_tools import show_sector_neighborhood
    watcher, rival = _two_players()
    home = seed_sector(0, 0, 0)
    target = seed_sector(2, 0, 0)
    watcher_ship = _fuelled_ship(watcher, home, "Watcher")
    seed_ship(rival, target, name="Quarry")
    _scan_east_from("U_WATCH", watcher_ship)

    def cells():
        view = show_sector_neighborhood("U_WATCH", center_x=0, center_y=0, center_z=0)
        return {(s["coord_x"], s["coord_y"]): s["cell"] for s in view["sectors"]}

    assert cells().get((2, 0)) == "r"
    conn = get_connection()
    conn.execute("""UPDATE player_sectors SET confidence=0
                    WHERE player_id=? AND sector_id=?""", (watcher, target))
    conn.commit(); conn.close()
    assert (2, 0) not in cells()      # blinked out, intel included
