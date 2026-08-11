"""
Core mutation logic for org missions that the ship's log can dispatch --
today just colonization.

Same split, and the same reason, as engine/movement.py: the tool wrapper
(xsettlers_mcp.tools.organization_tools.set_mission) owns the connection, the
ownership check and the org-lock rules; this module owns the state change and
operates on an already-open cur/transaction, so engine/ship_log.py can call it
from inside end_of_turn()'s own transaction without a second connection
deadlocking on "database is locked". current_turn arrives as a parameter so
nothing here imports engine.turn.
"""
import json
from db.events import record_event_direct
from engine.production import COLONIZATION_ENERGY_COST
from engine.org_resources import available_org_resource, drain_org_resource

def apply_colonize(cur, org_id: int, player_id: int, sector_id: int,
                   current_turn: int, params: dict = None) -> dict:
    """
    Commit a ship to becoming a colony: charge the energy, lock the hull, and
    schedule the colonize_complete event engine/turn.py resolves 3 turns out.
    No ownership check and no org-lock check (the caller's job).

    Colonization is bought, not merely ordered -- a ship that cannot pay is
    refused and left completely untouched, with no event written at all, so a
    refused conversion leaves no trace. Unlike every other ship's-log action
    this one can therefore fail for a reason that is nobody's mistake: the
    order was valid when it was given and the ship simply spent or never
    earned the energy before it fired. Callers get an {"error": ...} dict, the
    same shape set_mission returns, rather than an exception.
    """
    available_energy = available_org_resource(cur, org_id, "energy")
    if available_energy < COLONIZATION_ENERGY_COST:
        return {"error": f"Colonizing costs {COLONIZATION_ENERGY_COST:g} energy; "
                         f"this ship has {available_energy:g}",
                "required_energy": COLONIZATION_ENERGY_COST,
                "available_energy": available_energy}
    record_event_direct(cur, current_turn, "mission.set",
        payload={"org_id": org_id, "mission": "colonize", "params": params or {}},
        actor_id=player_id, subject_id=org_id, subject_type="organization")
    resolve_turn = current_turn + 3
    record_event_direct(cur, current_turn, "colonize_complete",
        payload={"org_id": org_id, "sector_id": sector_id,
                 "energy_cost": COLONIZATION_ENERGY_COST},
        actor_id=player_id, subject_id=org_id, subject_type="organization",
        resolve_at_turn=resolve_turn)
    # Charge the whole conversion cost up front, at commitment rather than at
    # completion. The ship is locked for the 3-turn transition with no cancel
    # path, so there is nothing to refund and no way to dodge the bill by
    # changing your mind -- and paying now means the player sees the hit while
    # they can still reason about what caused it.
    drain_org_resource(cur, org_id, "energy", COLONIZATION_ENERGY_COST)
    cur.execute("UPDATE organizations SET mission='colonize', mission_params=?, "
                "is_mobile=0 WHERE id=?",
                (json.dumps(params) if params else None, org_id))
    return {"ok": True, "org_id": org_id, "mission": "colonize",
            "energy_spent": COLONIZATION_ENERGY_COST, "completes_at_turn": resolve_turn}
