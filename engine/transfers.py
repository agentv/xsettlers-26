"""
Resource transfer between two of one player's own organizations.

A transfer is a push: the giving org orders it, and co-location is the
receiver's implied consent -- there is no request, accept or handshake. Both
organizations belong to the caller; there is no transfer across an ownership
boundary in either direction, because overflow is destroyed and shoving cargo
into a rival's full colony would be a weapon behind a tool with no combat.

Same split, and the same reason, as engine/missions.py: the tool wrapper
(xsettlers_mcp.tools.organization_tools.transfer_resources) owns the
connection and the ownership check; this module owns the state change and
operates on an already-open cur/transaction, so end_of_turn() can resolve a
transfer inside its own transaction without a second connection deadlocking.
current_turn arrives as a parameter so nothing here imports engine.turn.

Nothing is escrowed at order time -- `amount` stays live in the giver's
economy, spendable by its own production and upkeep, right up to resolution
one tick later. Resolution runs just after this turn's arrivals settle
(engine/turn.py step 2.3), so a ship that only reaches the receiver's sector
on the resolving turn still completes a transfer ordered while it was inbound.
"""
from db.events import record_event_direct
from engine.org_resources import (available_org_resource, drain_org_resource,
                                  store_org_resource)
from engine.production import RESOURCE_STORAGE_COLUMN

VALID_RESOURCES = tuple(RESOURCE_STORAGE_COLUMN)


def apply_transfer_order(cur, player_id: int, from_org_id: int, to_org_id: int,
                         resource: str, amount: float, current_turn: int) -> dict:
    """
    Queue a transfer for resolution one tick from now. Validates that both
    organizations belong to `player_id`, that they are different, that
    `resource` and `amount` are sane, and -- the check most likely to be
    written wrong -- that the two currently share a *real* sector: both
    sector_ids equal AND not the -1 sentinel, which is a parking slot rather
    than a place, so two ships in transit are never co-located.

    Nothing is charged or escrowed here. Returns the tool-shaped {"error": ...}
    on any failure, leaving no queue row and no event.
    """
    if resource not in RESOURCE_STORAGE_COLUMN:
        return {"error": f"Invalid resource '{resource}'. Valid: {list(VALID_RESOURCES)}"}
    if amount <= 0:
        return {"error": "amount must be positive"}
    if from_org_id == to_org_id:
        return {"error": "An organization cannot transfer to itself"}

    orgs = {row["id"]: row for row in cur.execute(
        "SELECT id, sector_id FROM organizations WHERE id IN (?,?) AND player_id=?",
        (from_org_id, to_org_id, player_id)).fetchall()}
    if from_org_id not in orgs:
        return {"error": "Giving organization not found or not owned by you"}
    if to_org_id not in orgs:
        return {"error": "Receiving organization not found or not owned by you"}

    from_sector, to_sector = orgs[from_org_id]["sector_id"], orgs[to_org_id]["sector_id"]
    if from_sector == -1 or to_sector == -1:
        return {"error": "Both organizations must be in a sector, not in transit"}
    if from_sector != to_sector:
        return {"error": "The two organizations must currently share a sector"}

    resolve_turn = current_turn + 1
    cur.execute("""INSERT INTO transfer_queue
        (from_org_id, to_org_id, resource, amount, ordered_turn, resolve_turn)
        VALUES (?,?,?,?,?,?)""",
        (from_org_id, to_org_id, resource, amount, current_turn, resolve_turn))
    record_event_direct(cur, current_turn, "transfer.ordered",
        actor_id=player_id, subject_id=from_org_id, subject_type="organization",
        payload={"from_org_id": from_org_id, "to_org_id": to_org_id,
                 "resource": resource, "amount": amount,
                 "resolves_at_turn": resolve_turn})
    return {"ok": True, "from_org_id": from_org_id, "to_org_id": to_org_id,
            "resource": resource, "amount": amount, "resolves_at_turn": resolve_turn}


def resolve_due_transfers(cur, current_turn: int):
    """
    Resolve every transfer scheduled for this tick. Called right after
    end_of_turn() settles this turn's arrivals and before the ship's log runs
    (engine/turn.py step 2.3), so co-location is judged on where each org sits
    once inbound ships have landed -- a transfer ordered while the giver or
    receiver was still in transit completes the turn that ship arrives.

    `current_turn` is the pre-increment turn value, so a transfer ordered on
    turn T (resolve_turn T+1) is due on the pass where current_turn == T --
    the same `<= current_turn + 1` convention arrival_queue uses.

    Per row: if the two are no longer co-located, nothing happens and the
    giver keeps everything. If they are, the giver loses whatever of the
    resource it still holds, capped at the amount ordered; the receiver gains
    that, capped at its free capacity; anything past that is destroyed --
    reported only on the transfer.resolved event, never returned or held.
    Rows are deleted as they resolve, which is what makes this idempotent.
    """
    due = cur.execute(
        "SELECT * FROM transfer_queue WHERE resolve_turn <= ? ORDER BY id",
        (current_turn + 1,)).fetchall()
    for row in due:
        _resolve_one(cur, current_turn, row)
        cur.execute("DELETE FROM transfer_queue WHERE id=?", (row["id"],))


def _resolve_one(cur, current_turn: int, row):
    frm = cur.execute("SELECT player_id, sector_id FROM organizations WHERE id=?",
                      (row["from_org_id"],)).fetchone()
    to = cur.execute("SELECT player_id, sector_id FROM organizations WHERE id=?",
                     (row["to_org_id"],)).fetchone()
    resource, ordered = row["resource"], row["amount"]
    base = {"from_org_id": row["from_org_id"], "to_org_id": row["to_org_id"],
            "resource": resource, "ordered": ordered}
    actor = frm["player_id"] if frm else (to["player_id"] if to else None)

    def resolved(**extra):
        record_event_direct(cur, current_turn, "transfer.resolved", actor_id=actor,
            subject_id=row["from_org_id"], subject_type="organization",
            payload={**base, **extra})

    if not frm or not to:
        resolved(completed=False, reason="organization no longer exists")
        return
    if frm["player_id"] != to["player_id"]:
        resolved(completed=False, reason="organizations no longer share an owner")
        return
    if frm["sector_id"] == -1 or frm["sector_id"] != to["sector_id"]:
        resolved(completed=False, reason="not co-located at resolution")
        return

    held = available_org_resource(cur, row["from_org_id"], resource)
    sent = min(held, ordered)
    drain_org_resource(cur, row["from_org_id"], resource, sent)
    stored = store_org_resource(cur, row["to_org_id"], None, resource, sent)
    resolved(completed=True, sent=sent, stored=stored, destroyed=sent - stored)
