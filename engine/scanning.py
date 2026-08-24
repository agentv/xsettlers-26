"""
Scanning: what a legal aim is, and how an aim gets written down.

The apply_* functions operate on an already-open cur/transaction and do no
ownership check (the caller's job). That split is what lets engine/ship_log.py
dispatch a queued aim from inside end_of_turn()'s own transaction --
db/connection.py sets no busy_timeout, so a self-connecting call there would
fail immediately with "database is locked".

Range is deliberately NOT re-checked by the apply_* functions: an aim is a
relative offset whose reach never changes with position (see
engine/bearings.py), so an aim validated when the order was given is still in
range wherever the ship ends up.
"""
import json
import math

from db.events import record_event_direct
from engine.bearings import (SCAN_BEARINGS, bearing_name, get_scan_range,
                             resolve_bearing)

OUT_OF_RANGE = "Aim reaches {distance:.2f} sectors; scan range is {scan_range}"


def resolve_aim(bearing, offset_x, offset_y, offset_z):
    """
    Turn either a compass bearing ("NE") or an explicit offset into
    (dx, dy, dz), or return an error dict. Exactly one form must be given.
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


def is_aiming(bearing=None, offset_x=None, offset_y=None, offset_z=None) -> bool:
    """Whether the caller supplied an aim at all, as opposed to clearing one or
    setting a task that takes none. Passing nothing is how a player stops
    paying for a scanner, so it has to be distinguishable from an aim of
    (0,0,0)."""
    return bearing is not None or any(c is not None for c in (offset_x, offset_y, offset_z))


def resolve_or_clear(bearing=None, offset_x=None, offset_y=None, offset_z=None):
    """
    (offset, clearing, None) for a well-formed call, or (None, None, error).

    Passing no aim at all is a legitimate request -- it is how a player stops
    paying for a scanner -- so it comes back as clearing=True rather than as a
    malformed call. Both scan-bearing tools open with this, which is what keeps
    "no aim means clear it" from being decided twice.
    """
    if not is_aiming(bearing, offset_x, offset_y, offset_z):
        return None, True, None
    offset, err = resolve_aim(bearing, offset_x, offset_y, offset_z)
    if err:
        return None, None, err
    return offset, False, None


def aim_status(org_id: int, offset) -> dict:
    """
    Describe an aim: its offset, its compass name if it has one, the distance
    it reaches, and whether that is within scan range.
    """
    dx, dy, dz = offset
    distance = math.sqrt(dx*dx + dy*dy + dz*dz)
    scan_range = get_scan_range(org_id)
    return {"offset_x": dx, "offset_y": dy, "offset_z": dz,
            "bearing": bearing_name(dx, dy, dz),
            "distance": distance, "scan_range": scan_range,
            "in_range": distance <= scan_range}


def offset_from_params(params: dict):
    """The (dx,dy,dz) aim carried in a stored params dict, or None if it has
    no aim. Queued commands store aims already resolved to offsets (see
    queue_command's normalization), so every dispatcher reads them this way."""
    if all(k in params for k in ("offset_x", "offset_y", "offset_z")):
        return (params["offset_x"], params["offset_y"], params["offset_z"])
    return None


def aim_label(offset) -> str:
    """
    How an aim is written for a person: its compass name when it lands on one
    of the 12 named bearings, else the raw "(dx,dy,dz)". One helper because a
    report's bearing column and a map's aim marker must name the same aim the
    same way.
    """
    dx, dy, dz = offset
    return bearing_name(dx, dy, dz) or f"({dx},{dy},{dz})"


def scanners_on(cur, org: dict) -> list:
    """
    Every scanner this org carries -- its innate sensors (organizations.
    scan_offset_*) plus every pod on the scan task -- as a list of
    {"source", "pod_id", "offset"} dicts, ordered sensors-first then by pod id.

    `offset` is the (dx, dy, dz) the scanner is aimed at, or None for a scan
    pod that has been given the task but no aim. An unaimed pod still costs
    its food and reveals nothing, so it is listed and flagged rather than
    dropped (see set_pod_task)."""
    scanners = []
    if org["scan_offset_x"] is not None:
        scanners.append({"source": "sensors", "pod_id": None,
                         "offset": (org["scan_offset_x"], org["scan_offset_y"],
                                    org["scan_offset_z"])})
    cur.execute("SELECT id, task_params FROM pods WHERE org_id=? AND task='scan' ORDER BY id",
                (org["id"],))
    for pod in cur.fetchall():
        offset = offset_from_params(json.loads(pod["task_params"])) if pod["task_params"] else None
        scanners.append({"source": f"pod {pod['id']}", "pod_id": pod["id"],
                         "offset": offset})
    return scanners


def check_range(org_id: int, offset):
    """
    (status, None) when an aim is in range, (status, error) when it overreaches.
    For callers that already hold a resolved offset.
    """
    status = aim_status(org_id, offset)
    if status["in_range"]:
        return status, None
    return status, {"error": OUT_OF_RANGE.format(**status), **status}


def check_aim(org_id: int, bearing=None, offset_x=None, offset_y=None, offset_z=None):
    """
    Resolve an aim and confirm it reaches: (offset, status, None), or
    (None, None, error) if the bearing is unknown, the offset is malformed, or
    it overreaches.

    Every path that accepts an aim runs this -- the two scan-bearing tools, pod
    tasking, and queue_command validating an order before it is stored -- so
    "what is a legal aim" is answered in one place and a player gets the same
    refusal whichever way they asked.
    """
    offset, err = resolve_aim(bearing, offset_x, offset_y, offset_z)
    if err:
        return None, None, err
    status, err = check_range(org_id, offset)
    if err:
        return None, None, err
    return offset, status, None


def apply_set_org_scan_bearing(cur, org_id: int, player_id: int, offset,
                               current_turn: int, bearing: str = None):
    """
    Point an org's sensors at `offset` (a relative x/y/z triple), or clear the
    aim entirely when offset is None.

    `bearing` is the compass name for the offset when the caller happens to
    know it; it is payload decoration only, never used to compute the aim.
    """
    if offset is None:
        record_event_direct(cur, current_turn, "organization.scan_bearing_cleared",
            payload={"org_id": org_id},
            actor_id=player_id, subject_id=org_id, subject_type="organization")
        cur.execute("""UPDATE organizations
            SET scan_offset_x=NULL, scan_offset_y=NULL, scan_offset_z=NULL WHERE id=?""",
            (org_id,))
        return
    record_event_direct(cur, current_turn, "organization.scan_bearing_set",
        payload={"org_id": org_id, "offset_x": offset[0], "offset_y": offset[1],
                 "offset_z": offset[2], "bearing": bearing},
        actor_id=player_id, subject_id=org_id, subject_type="organization")
    cur.execute("""UPDATE organizations
        SET scan_offset_x=?, scan_offset_y=?, scan_offset_z=? WHERE id=?""",
        (offset[0], offset[1], offset[2], org_id))


def apply_set_pod_scan_bearing(cur, pod_id: int, player_id: int, offset,
                               current_turn: int, bearing: str = None):
    """
    Aim a scan pod at `offset`, or clear its aim when offset is None. The pod's
    task is left alone -- this only writes task_params."""
    if offset is None:
        record_event_direct(cur, current_turn, "pod.scan_bearing_cleared",
            payload={"pod_id": pod_id},
            actor_id=player_id, subject_id=pod_id, subject_type="pod")
        cur.execute("UPDATE pods SET task_params=NULL WHERE id=?", (pod_id,))
        return
    params = {"offset_x": offset[0], "offset_y": offset[1], "offset_z": offset[2]}
    record_event_direct(cur, current_turn, "pod.scan_bearing_set",
        payload={"pod_id": pod_id, **params, "bearing": bearing},
        actor_id=player_id, subject_id=pod_id, subject_type="pod")
    cur.execute("UPDATE pods SET task_params=? WHERE id=?", (json.dumps(params), pod_id))
