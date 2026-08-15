import json
from db.events import record_event_direct
from engine.scanning import check_range

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
        status, err = check_range(org_id, offset)
        if err:
            return err
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
