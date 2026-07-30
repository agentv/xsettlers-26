from db.connection import get_connection
from xsettlers_mcp.tools.sector_tools import get_scan_range
from xsettlers_mcp.tools.organization_tools import set_pod_mission
from xsettlers_mcp.tools.navigation_tools import confirm_move
from engine.turn import end_of_turn
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod

# --- scan range enforcement at resolution (see engine/turn.py) ---

def test_scan_within_range_reveals_target():
    """Confidence is checked here too (not just sector existence): scan
    resolution (step 3c of end_of_turn()) stamps confidence=100, but fog
    decay (step 5) runs later in the same pass and immediately decays it
    since the scanned sector isn't occupied by any of the player's orgs --
    pre-existing engine behavior, not something reveal_sector changes."""
    from engine.turn import CONFIDENCE_DECAY
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, mission="scan")
    seed_pod(sid, mission="produce_food", storage_current=100.0)
    r = get_scan_range(sid)
    set_pod_mission("U_P1", pod, "scan", target_x=r, target_y=0, target_z=0)
    end_of_turn()
    conn = get_connection()
    sector = conn.execute(
        "SELECT id FROM sectors WHERE coord_x=? AND coord_y=0 AND coord_z=0", (r,)).fetchone()
    assert sector is not None
    ps = conn.execute("SELECT confidence FROM player_sectors WHERE player_id=? AND sector_id=?",
                      (pid, sector["id"])).fetchone()
    assert ps["confidence"] == round(100 * CONFIDENCE_DECAY)
    conn.close()

def test_scan_out_of_range_does_not_reveal_and_logs_alert():
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, mission="scan")
    seed_pod(sid, mission="produce_food", storage_current=100.0)
    r = get_scan_range(sid)
    out_of_range_x = r + 5
    set_pod_mission("U_P1", pod, "scan", target_x=out_of_range_x, target_y=0, target_z=0)
    end_of_turn()
    conn = get_connection()
    sector = conn.execute(
        "SELECT id FROM sectors WHERE coord_x=? AND coord_y=0 AND coord_z=0",
        (out_of_range_x,)).fetchone()
    alert = conn.execute(
        "SELECT payload FROM events WHERE event_type='alert.scan_out_of_range'").fetchone()
    conn.close()
    assert sector is None
    assert alert is not None

def test_rescanning_known_sector_refreshes_confidence_without_altering_resources():
    """Re-scanning an already-revealed sector must not re-randomize its
    resources (reveal_sector is idempotent -- it's a fresh look at whatever
    is currently there, not a reset), and should refresh the player's
    confidence even if it had decayed to near-zero in the meantime."""
    from engine.turn import CONFIDENCE_DECAY
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, mission="scan")
    seed_pod(sid, mission="produce_food", storage_current=100.0)
    set_pod_mission("U_P1", pod, "scan", target_x=1, target_y=0, target_z=0)
    end_of_turn()  # first scan: reveals the sector, stamps then decays confidence

    conn = get_connection()
    sector = conn.execute(
        "SELECT id, energy_capacity FROM sectors WHERE coord_x=1 AND coord_y=0 AND coord_z=0").fetchone()
    original_capacity = sector["energy_capacity"]
    # simulate it having decayed further, e.g. several unoccupied turns since
    conn.execute("UPDATE player_sectors SET confidence=5 WHERE player_id=? AND sector_id=?",
                 (pid, sector["id"]))
    conn.commit(); conn.close()

    set_pod_mission("U_P1", pod, "scan", target_x=1, target_y=0, target_z=0)  # re-scan same target
    end_of_turn()

    conn = get_connection()
    resector = conn.execute("SELECT energy_capacity FROM sectors WHERE id=?", (sector["id"],)).fetchone()
    ps = conn.execute("SELECT confidence FROM player_sectors WHERE player_id=? AND sector_id=?",
                      (pid, sector["id"])).fetchone()
    conn.close()
    assert resector["energy_capacity"] == original_capacity  # not re-randomized
    # stamped back to 100 by the re-scan, then decays once more within this
    # same end_of_turn() pass since the org still doesn't occupy it
    assert ps["confidence"] == round(100 * CONFIDENCE_DECAY)

def test_scan_out_of_range_still_costs_food():
    """The scan attempt still costs its food upkeep even though it fails --
    only the reveal is gated by range, not the resource cost."""
    pid = seed_player(); oid = seed_sector(0,0,0); sid = seed_ship(pid, oid)
    pod = seed_pod(sid, mission="scan")
    seed_pod(sid, mission="produce_food", storage_current=100.0)
    r = get_scan_range(sid)
    set_pod_mission("U_P1", pod, "scan", target_x=r+5, target_y=0, target_z=0)
    end_of_turn()
    conn = get_connection()
    food = conn.execute("SELECT SUM(food_stored) s FROM pods WHERE org_id=?", (sid,)).fetchone()["s"]
    conn.close()
    assert food < 100.0
