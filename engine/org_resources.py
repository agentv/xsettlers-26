"""
An organization's pooled resource stock: reading it, drawing from it, adding
to it. Storage is per-pod but an org spends as one purse -- these three
functions are the only place that pooling rule is implemented.

A leaf module so that code the turn engine *dispatches into* can use it:
engine/turn.py imports engine/ship_log.py, so anything ship_log dispatches to
(engine/missions.py's apply_colonize, which has to charge the colonization
cost) cannot import from engine.turn without a cycle --
turn -> ship_log -> missions -> turn.
"""
from engine.production import RESOURCE_STORAGE_COLUMN

def available_org_resource(cur, org_id: int, resource: str) -> float:
    """
    An org's pooled stock of a resource: summed across ALL of that org's
    pods' <resource>_stored column, regardless of each pod's current task
    -- storage is generic per pod, so retasking a pod never hides whatever
    it already has stored (see RESOURCE_STORAGE_COLUMN).
    """
    col = RESOURCE_STORAGE_COLUMN[resource]
    return cur.execute(
        f"SELECT COALESCE(SUM({col}),0) AS total FROM pods WHERE org_id=?",
        (org_id,)).fetchone()["total"]

def drain_org_resource(cur, org_id: int, resource: str, amount: float):
    """Drain amount of a resource from an org's pooled stock, sequentially
    (by pod id) across whichever of its pods currently hold that resource --
    regardless of their current task."""
    if amount <= 0:
        return
    col = RESOURCE_STORAGE_COLUMN[resource]
    remaining = amount
    source_pods = cur.execute(
        f"SELECT id, {col} AS have FROM pods WHERE org_id=? AND {col}>0 ORDER BY id",
        (org_id,)).fetchall()
    for sp in source_pods:
        if remaining <= 0:
            break
        draw = min(sp["have"], remaining)
        if draw > 0:
            cur.execute(f"UPDATE pods SET {col}={col}-? WHERE id=?", (draw, sp["id"]))
            remaining -= draw

def store_org_resource(cur, org_id: int, producing_pod_id, resource: str,
                       amount: float) -> float:
    """
    Add amount of a resource to storage: fills the producing pod's own free
    space first, then spills into other pods in the same org that still
    have free space (by pod id), then is lost if no pod in the org has room
    left. Free space on a pod = storage_capacity minus everything currently
    stored there (energy+food+goods combined) -- storage is one shared
    container per pod, not resource-specific, so a pod already full of one
    resource has no room for another regardless of type.

    `producing_pod_id` may be None -- a resource transfer credits an org with
    no pod of its own to prefer, so it fills purely by pod id. Returns how
    much was actually stored (amount minus whatever the org had no room for),
    so a caller crediting from outside the economy can report what it
    destroyed.
    """
    if amount <= 0:
        return 0.0
    col = RESOURCE_STORAGE_COLUMN[resource]
    remaining = amount
    # Producing pod first (id != producing_pod_id sorts to 0/False first), then
    # by id. With producing_pod_id None the expression is NULL for every row,
    # so the ordering falls through to id alone.
    pods = cur.execute(
        """SELECT id, storage_capacity, energy_stored, food_stored, goods_stored
           FROM pods WHERE org_id=? ORDER BY (id != ?), id""",
        (org_id, producing_pod_id)).fetchall()
    for p in pods:
        if remaining <= 0:
            break
        free = p["storage_capacity"] - (p["energy_stored"] + p["food_stored"] + p["goods_stored"])
        add = min(free, remaining)
        if add > 0:
            cur.execute(f"UPDATE pods SET {col}={col}+? WHERE id=?", (add, p["id"]))
            remaining -= add
    # Anything still remaining here means the whole org is full -- lost.
    return amount - remaining
