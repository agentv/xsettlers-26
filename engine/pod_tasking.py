import json, math
from db.events import record_event_direct
from xsettlers_mcp.tools.sector_tools import get_scan_range, resolve_bearing, bearing_name, SCAN_BEARINGS

def resolve_aim(bearing, offset_x, offset_y, offset_z):
    """
    Turn either a compass bearing ("NE") or an explicit offset into
    (dx, dy, dz), or return an error dict. Exactly one form must be given.
    Promoted here (was xsettlers_mcp.tools.organization_tools._resolve_aim)
    so engine.ship_log's dispatch adapters and engine.movement's
    during_transit hook can validate an aim without importing
    organization_tools -- which itself imports engine.ship_log, so that
    direction would be circular.
    """
    named = bearing is not None
    explicit = any(c is not None for c in (offset_x, offset_y, offset_z))
    if named and explicit:
        return None, {"error": "Give either a bearing or offset_x/y/z, not both"}
    if named:
        offset = resolve_bearing(bearing)
        if offset is None:
            return None, {"error": f"Unknown bearing '{bearing}'. Valid: {sorted(SCAN_BEARINGS)}"}
        return offset, None
    if not explicit:
        return None, {"error": "Give a bearing (e.g. 'NE') or offset_x/offset_y/offset_z"}
    if any(c is None for c in (offset_x, offset_y, offset_z)):
        return None, {"error": "offset_x, offset_y, and offset_z must all be provided together"}
    return (int(offset_x), int(offset_y), int(offset_z)), None

def aim_status(org_id: int, offset) -> dict:
    """
    Describe an aim: its offset, its compass name if it has one, the distance
    it reaches, and whether that is within scan range. Promoted alongside
    resolve_aim, same reason.
    """
    dx, dy, dz = offset
    distance = math.sqrt(dx*dx + dy*dy + dz*dz)
    scan_range = get_scan_range(org_id)
    return {"offset_x": dx, "offset_y": dy, "offset_z": dz,
            "bearing": bearing_name(dx, dy, dz),
            "distance": distance, "scan_range": scan_range,
            "in_range": distance <= scan_range}

def apply_set_pod_task(cur, pod_id: int, org_id: int, player_id: int, task: str,
                       offset, current_turn: int) -> dict:
    """
    Core mutation logic behind set_pod_task, operating on an already-open
    cur/transaction -- same split as engine.movement.apply_confirm_move, for
    the same reason (record_event/a fresh connection would collide with an
    already-open write transaction; see that module's docstring). No
    ownership check, no task-validity check (caller's job) -- org_id and
    player_id are passed in rather than looked up, since callers that
    already have them (ship's log dispatch, already inside an org lookup)
    shouldn't pay a redundant query.
    offset is a resolved (dx,dy,dz) tuple or None -- callers do their own
    resolve_aim() first, since that's pure validation with no DB dependency.
    """
    status, params = {}, None
    if offset is not None:
        status = aim_status(org_id, offset)
        if not status["in_range"]:
            return {"error": f"Aim reaches {status['distance']:.2f} sectors; scan range is "
                             f"{status['scan_range']}", **status}
        params = {"offset_x": offset[0], "offset_y": offset[1], "offset_z": offset[2]}
    record_event_direct(cur, current_turn, "pod.task_set",
        payload={"pod_id": pod_id, "task": task, "params": params},
        actor_id=player_id, subject_id=pod_id, subject_type="pod")
    if offset is not None:
        cur.execute("UPDATE pods SET task=?, task_params=? WHERE id=?",
                    (task, json.dumps(params), pod_id))
    else:
        cur.execute("UPDATE pods SET task=? WHERE id=?", (task, pod_id))
    return {"ok": True, "pod_id": pod_id, "task": task, **status}
