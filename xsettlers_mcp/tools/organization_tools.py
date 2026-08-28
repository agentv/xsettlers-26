from xsettlers_mcp.tools.registry import mcp_tool
from db.events import record_event
from db.orgs import org_position
from xsettlers_mcp.tools.session import player_tool, ORG_NOT_OWNED, POD_NOT_OWNED
from engine.turn import get_current_turn
from engine.missions import apply_colonize
from engine.transfers import apply_transfer_order, VALID_RESOURCES as VALID_TRANSFER_RESOURCES
from engine.movement import move_params_error
from engine.actions import ACTION_NAMES, UPON_DEPARTURE_ACTIONS, TRIGGER_PHASES
from engine.pod_tasking import apply_set_pod_task
from engine.scanning import (apply_set_org_scan_bearing, apply_set_pod_scan_bearing,
                             check_aim, check_range, is_aiming, resolve_aim,
                             resolve_or_clear)
from xsettlers_mcp.tools.navigation_tools import confirm_move
import json


VALID_ORG_MISSIONS = {"idle", "move", "colonize", "defend", "attack"}

# Combat is designed but not built: engine/turn.py's step 5 dispatches these
# two to stubs. set_mission is the only door -- queue_command's whitelist and
# the NPC layer reach the same four actions -- so one gate covers it.
# Building combat is deleting this set and filling the stubs.
UNIMPLEMENTED_MISSIONS = {"defend", "attack"}
WEAPONS_INOPERABLE = "Weapons are inoperable — combat is not implemented in this build"
VALID_POD_TASKS = {"idle", "produce_energy", "produce_food", "produce_goods", "scan"}
VALID_TRIGGER_PHASES = TRIGGER_PHASES

AIM_KEYS = ("bearing", "offset_x", "offset_y", "offset_z")
AIM_NOT_A_SCAN = "Only the scan task takes an aim; '{task}' does not"


def _aim_args(params: dict) -> dict:
    """The four aim fields out of a queued command's params, as keyword
    arguments for check_aim."""
    return {k: params.get(k) for k in AIM_KEYS}


def _params_aiming(params: dict) -> bool:
    """Whether a queued command's params carry an aim (see is_aiming)."""
    return is_aiming(**_aim_args(params))


def _aimed_sector(cur, org_id: int, offset):
    """Absolute coordinates an org's aim currently points at, or None if it
    is in transit (no position to offset from)."""
    org = org_position(cur, org_id)
    if not org:
        return None
    return (org["coord_x"] + offset[0], org["coord_y"] + offset[1], org["coord_z"] + offset[2])


def _write_aim(sess, org_id: int, subject_id: int, offset, clearing: bool,
               apply_aim, identity: dict):
    """
    The half of aiming a scanner that does not care what carries it: check the
    range, write the aim, and report where it now points.

    `org_id` is the org whose position the aim is measured from -- the org
    itself, or a scan pod's parent.
    """
    if clearing:
        apply_aim(sess.cur, subject_id, sess.player_id, None, get_current_turn())
        return {"ok": True, **identity, "cleared": True}
    status, err = check_range(org_id, offset)
    if err:
        return err
    apply_aim(sess.cur, subject_id, sess.player_id, offset, get_current_turn(),
              bearing=status["bearing"])
    aimed = _aimed_sector(sess.cur, org_id, offset)
    return {"ok": True, **identity, "cleared": False,
            "aimed_at": list(aimed) if aimed else None, **status}


@mcp_tool(
    "Set an organization's mission (idle/move/colonize). 'defend' and "
    "'attack' are part of the design but not built -- ordering one is "
    "refused, not silently accepted. For "
    "mission='move', params must include dest_x/dest_y/dest_z -- delegates "
    "to the same confirm_move flow as the dedicated tool, so prefer "
    "preview_move first to check travel time.")
@player_tool
def set_mission(sess, org_id: int, mission: str, params: dict = None) -> dict:
    """
    Set an organization's mission. 'defend' and 'attack' are refused (see
    UNIMPLEMENTED_MISSIONS): they stay in the vocabulary so a player asking
    is told combat is not built yet, rather than handed a mission the engine
    will never resolve. Validates ownership and mission type, and enforces
    the three org-lock states:
      - in transit (sector_id == -1): locked entirely, must cancel_move first
      - colony: locked against 'move' only — every other mission stands
      - mid-colonization (ship, is_mobile == 0, not in transit): locked
        entirely, committed for the 3-turn transition window

    mission='colonize' costs COLONIZATION_ENERGY_COST energy from the ship's
    pooled stock, charged in full at commitment; a ship that cannot pay is
    refused and left untouched. On payment it flips is_mobile to 0 and
    schedules a colonize_complete event 3 turns out.

    mission='move' delegates entirely to navigation_tools.confirm_move rather
    than writing mission='move' onto the row -- confirm_move is what parks the
    org at the sentinel sector and queues the arrival_queue row. Without it
    the org would sit forever with mission='move' and nothing to resolve it.
    """
    if mission not in VALID_ORG_MISSIONS:
        return {"error": f"Invalid mission '{mission}'. Valid: {sorted(VALID_ORG_MISSIONS)}"}
    if mission in UNIMPLEMENTED_MISSIONS:
        return {"error": WEAPONS_INOPERABLE}
    cur = sess.cur
    cur.execute("SELECT id,is_mobile,org_type,sector_id FROM organizations WHERE id=? AND player_id=?",
                (org_id, sess.player_id))
    org = cur.fetchone()
    if not org:
        return {"error": ORG_NOT_OWNED}
    if org["sector_id"] == -1:
        return {"error": "This organization is currently in transit — call cancel_move before assigning a new mission"}
    if org["org_type"] == "colony":
        if mission == "move":
            return {"error": "Colonies cannot move"}
    elif not org["is_mobile"]:
        return {"error": "This ship is committed to colonizing and cannot be reassigned until the transition resolves"}
    if mission == "move":
        params = params or {}
        missing = [k for k in ("dest_x", "dest_y", "dest_z") if k not in params]
        if missing:
            return {"error": f"mission='move' requires params: {', '.join(missing)}"}
        # confirm_move is itself a @player_tool and opens its own connection,
        # so this one has to be out of the way first or the two collide
        # ("database is locked" -- see PlayerSession.release).
        sess.release()
        return confirm_move(sess.player["player_token"], org_id,
                             params["dest_x"], params["dest_y"], params["dest_z"])
    # Colonization is bought, not merely ordered, and every rule about that
    # purchase lives in engine.missions.apply_colonize -- the same function
    # the ship's log dispatches a queued 'colonize' through, so a scheduled
    # conversion and a hand-ordered one cannot drift apart. It refuses a ship
    # that cannot pay without writing any event, leaving no trace of the
    # attempt.
    if mission == "colonize":
        return apply_colonize(cur, org_id, sess.player_id, org["sector_id"],
                              get_current_turn(), params)
    record_event(
        event_type="mission.set",
        payload={"org_id": org_id, "mission": mission, "params": params or {}},
        actor_id=sess.player_id, subject_id=org_id, subject_type="organization")
    cur.execute("UPDATE organizations SET mission=?, mission_params=? WHERE id=?",
                (mission, json.dumps(params) if params else None, org_id))
    return {"ok": True, "org_id": org_id, "mission": mission}

@mcp_tool(
    "Order one of your organizations to hand a resource to another of your "
    "own organizations. A push -- no request or accept on the receiving "
    "end; the two sharing a sector is the consent. Both must be yours and "
    "must currently occupy the same real sector (not in transit). One "
    "resource type (energy/food/goods) and a positive amount. Nothing is "
    "held aside: the amount stays spendable in the giver's economy until "
    "the transfer resolves one tick later, when the giver sends whatever it "
    "still holds up to the amount ordered, the receiver takes what fits, "
    "and any excess is destroyed. If the two are no longer together at "
    "resolution, nothing moves.")
@player_tool
def transfer_resources(sess, from_org_id: int, to_org_id: int, resource: str,
                       amount: float) -> dict:
    """
    Queue a resource transfer between two of the caller's own organizations,
    resolved one tick later, just after that turn's arrivals settle -- so a
    ship that only lands at the receiver's sector on the resolving turn still
    completes a transfer it ordered while inbound.

    Every rule about the transfer lives in engine.transfers.apply_transfer_order:
    both orgs owned by the caller, distinct, sharing a real sector (equal
    sector_id, neither -1). Nothing is charged here; the giver's stock stays
    live until resolution, capped there at the amount ordered, and the
    receiver's gain is capped at its free capacity with the rest destroyed.
    """
    return apply_transfer_order(sess.cur, sess.player_id, from_org_id, to_org_id,
                                resource, amount, get_current_turn())


def _normalize_queued_params(cur, org_id: int, action: str, params: dict):
    """
    Validate a queued command's params at QUEUE time and return them
    normalized, as (params, None) -- or (None, error) if the order could
    never work.

    The dispatchers (engine/ship_log.py, engine/movement._dispatch_upon_departure)
    index straight into the stored params dict and hand it to helpers that do
    no ownership check and no task-validity check of their own, so this is
    where those checks live. Without them: a bad `task` string reaches the
    pods CHECK constraint and raises sqlite3.IntegrityError *inside
    end_of_turn()*, aborting the turn for every player and re-firing on every
    subsequent tick (the queue row is deleted only after its handler returns);
    a missing dest_y/dest_z raises KeyError the same way; and a `pod_id`
    belonging to another player's org would be retasked.

    Normalization serves the same end: a compass `bearing` is resolved to
    offset_x/y/z here, because both dispatchers read only the offsets and
    would otherwise silently drop an aim the player was told they could pass.
    """
    if action == "move":
        problem = move_params_error(params)
        return (None, {"error": problem}) if problem else (params, None)

    if action == "colonize":
        # Takes no params at all -- which ship is colonizing is the org the
        # command is attached to, and where is wherever it happens to be
        # standing when the order fires. Whether it can afford the conversion
        # is deliberately NOT checked here: the answer now says nothing about
        # the answer then, since the ship goes on producing and spending in
        # between. engine.missions.apply_colonize decides at fire time and a
        # refusal is logged as such (see db.events.record_command_refused).
        return {}, None

    if action == "aim_scan":
        if not _params_aiming(params):
            return {}, None  # clears the org's aim
        offset, status, err = check_aim(org_id, **_aim_args(params))
        if err:
            return None, err
        return {"offset_x": offset[0], "offset_y": offset[1], "offset_z": offset[2],
                "bearing": status["bearing"]}, None

    if action == "transfer":
        # Co-location is deliberately NOT checked here -- the whole point of a
        # queued 'upon_arrival' transfer is that the giver is not there yet.
        # engine.transfers.apply_transfer_order rechecks it at fire time and a
        # miss is logged as a refusal, like a queued colonize that can no
        # longer pay. What must be sound now is the shape and the target's
        # ownership, since the dispatcher indexes straight into these fields.
        to_org_id, resource, amount = (params.get("to_org_id"),
                                       params.get("resource"), params.get("amount"))
        missing = [n for n, v in (("to_org_id", to_org_id), ("resource", resource),
                                  ("amount", amount)) if v is None]
        if missing:
            return None, {"error": f"action='transfer' requires params: {', '.join(missing)}"}
        if resource not in VALID_TRANSFER_RESOURCES:
            return None, {"error": f"Invalid resource '{resource}'. "
                                   f"Valid: {sorted(VALID_TRANSFER_RESOURCES)}"}
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            return None, {"error": "amount must be a positive number"}
        if to_org_id == org_id:
            return None, {"error": "An organization cannot transfer to itself"}
        if not cur.execute(
                """SELECT 1 FROM organizations WHERE id=? AND
                   player_id=(SELECT player_id FROM organizations WHERE id=?)""",
                (to_org_id, org_id)).fetchone():
            return None, {"error": "Receiving organization not found or not owned by you"}
        return params, None

    # action == 'set_pod_task' (the only remaining entry in SHIP_LOG_ACTIONS)
    pod_id, task = params.get("pod_id"), params.get("task")
    missing = [name for name, value in (("pod_id", pod_id), ("task", task)) if value is None]
    if missing:
        return None, {"error": f"action='set_pod_task' requires params: {', '.join(missing)}"}
    if task not in VALID_POD_TASKS:
        return None, {"error": f"Invalid pod task '{task}'. Valid: {sorted(VALID_POD_TASKS)}"}
    # The pod must belong to the org the command is attached to -- ownership of
    # the org was already checked by the caller, and this is what makes that
    # check actually cover the pod being retasked.
    if not cur.execute("SELECT id FROM pods WHERE id=? AND org_id=?",
                       (pod_id, org_id)).fetchone():
        return None, {"error": "Pod not found or not part of this organization"}

    if not _params_aiming(params):
        return params, None
    if task != "scan":
        return None, {"error": AIM_NOT_A_SCAN.format(task=task)}
    offset, _status, err = check_aim(org_id, **_aim_args(params))
    if err:
        return None, err
    normalized = {k: v for k, v in params.items() if k != "bearing"}
    normalized.update(offset_x=offset[0], offset_y=offset[1], offset_z=offset[2])
    return normalized, None

@mcp_tool(
    "Queue a one-shot command for an organization, resolved automatically "
    "by the engine instead of you having to call the underlying tool again "
    "by hand. Three trigger_phase values: 'upon_departure' fires the instant "
    "this org next enters transit (only action='set_pod_task' is legal here "
    "-- pod tasking is the one thing not locked by a departing org); "
    "'upon_arrival' fires the same tick this org's current move resolves, "
    "after the landing and before that turn's production, and requires the "
    "org to already be in transit; 'at_turn' fires at an explicit absolute "
    "turn (pass `turn`), independent of any move (e.g. \"on turn 7, jump "
    "somewhere else\"). "
    "Action whitelist: 'move' (either dest_x/dest_y/dest_z absolute or "
    "d_x/d_y/d_z relative to wherever the org is when the order fires, "
    "never both -- a move takes no speed, that is the hull's); 'set_pod_task' "
    "(params: pod_id, task, optionally bearing/offset_x/y/z -- same shape "
    "set_pod_task takes); 'colonize' (no params -- commits the ship at "
    "whatever sector it occupies when the order fires, and is refused if it "
    "cannot afford the energy by then); 'aim_scan' (optionally "
    "bearing/offset_x/y/z, same shape set_org_scan_bearing takes; pass none "
    "to clear the aim); 'transfer' (params: to_org_id, resource, amount -- "
    "hands a resource to another of your organizations; co-location is "
    "checked when it fires, not now, so a queued transfer is refused if the "
    "two are not together then). If you give the org new orders before a "
    "upon_arrival/at_turn command fires, the queued one is "
    "silently dropped rather than overriding your manual orders.")
@player_tool
def queue_command(sess, org_id: int, trigger_phase: str, action: str,
                  params: dict = None, turn: int = None) -> dict:
    """
    Queue a one-shot command for this org, resolved automatically by the
    engine at whichever of three fixed primitives is named.

    Three, because these are the moments a player actually names: getting
    under way, landing, or a turn they choose. A fourth phase fired one pass
    after landing and was removed -- anyone wanting it knows the arrival turn
    and can say at_turn, and an extra primitive is another word the grammar
    has to teach for no reach it did not already have.
      - 'upon_departure': fires the instant this org enters transit --
        event-triggered by the next confirm_move, not the turn sweep
        (engine.movement.apply_confirm_move). Only 'set_pod_task' is legal
        here; pod tasking is the one thing not locked by entering transit.
      - 'upon_arrival': fires the same tick this org's current move
        resolves -- after the arrival lands and before that turn's
        production, so a retasked pod produces at its new task the turn it
        gets there. Requires the org to already be in transit: there must be
        a pending arrival_queue row to anchor the resolve turn to.
      - 'at_turn': fires at an explicit absolute turn (the `turn` param,
        required for this phase only), independent of any move. No
        in-transit requirement.

    Action whitelist, all valid for upon_arrival/at_turn:
      - 'move' -- either dest_x/dest_y/dest_z or d_x/d_y/d_z (relative to
        wherever this org is standing when the order fires), never both. No
        speed: how fast the ship covers the distance is its hull's business.
      - 'set_pod_task' -- pod_id, task, optionally bearing/offset_x/y/z.
      - 'colonize' -- no params. Alone among these it can be refused at fire
        time rather than failing: a ship that cannot pay when the moment
        arrives is declined and left untouched
        (logged as alert.queued_command_refused).
      - 'aim_scan' -- optionally bearing/offset_x/y/z; pass none to clear.
      - 'transfer' -- to_org_id, resource, amount. Pushes a resource to
        another of your organizations. Like colonize it can be refused at
        fire time: co-location is rechecked then (the point of queueing it
        'upon_arrival' is that the giver is still inbound now).

    If a player gives the org new orders before a queued command fires, the
    command is silently dropped rather than clobbering them (see
    engine.ship_log.dispatch_due_commands).
    """
    if trigger_phase not in VALID_TRIGGER_PHASES:
        return {"error": f"Invalid trigger_phase '{trigger_phase}'. Valid: {sorted(VALID_TRIGGER_PHASES)}"}
    if action not in ACTION_NAMES:
        return {"error": f"Invalid action '{action}'. Valid: {sorted(ACTION_NAMES)}"}
    if trigger_phase == "upon_departure" and action not in UPON_DEPARTURE_ACTIONS:
        return {"error": f"'upon_departure' only supports: {sorted(UPON_DEPARTURE_ACTIONS)}"}
    if trigger_phase == "at_turn" and turn is None:
        return {"error": "'at_turn' requires a turn parameter (the absolute turn number to fire on)"}
    cur = sess.cur
    cur.execute("SELECT id FROM organizations WHERE id=? AND player_id=?", (org_id, sess.player_id))
    if not cur.fetchone():
        return {"error": ORG_NOT_OWNED}

    # Validate the payload now, not when the clock fires it (see
    # _normalize_queued_params): a malformed queued order surfacing as an
    # exception inside end_of_turn() would stop the turn for everyone.
    params, err = _normalize_queued_params(cur, org_id, action, params or {})
    if err:
        return err

    resolve_turn = None
    if trigger_phase == "upon_arrival":
        cur.execute("SELECT arrival_turn FROM arrival_queue WHERE org_id=?", (org_id,))
        arrival = cur.fetchone()
        if not arrival:
            return {"error": f"'{trigger_phase}' requires the organization to currently be "
                             f"in transit (no pending arrival found)"}
        resolve_turn = arrival["arrival_turn"]
    elif trigger_phase == "at_turn":
        resolve_turn = turn
    # upon_departure: resolve_turn stays None -- dispatched by org_id+phase
    # lookup inside apply_confirm_move, not the resolve_turn sweep.

    current_turn = get_current_turn()
    cur.execute("""INSERT INTO org_command_queue (org_id,trigger_phase,resolve_turn,action,params,created_turn)
                   VALUES (?,?,?,?,?,?)""",
                (org_id, trigger_phase, resolve_turn, action, json.dumps(params or {}), current_turn))
    command_id = cur.lastrowid
    return {"ok": True, "command_id": command_id, "org_id": org_id, "trigger_phase": trigger_phase,
            "action": action, "resolve_turn": resolve_turn}

@mcp_tool(
    "Set a pod's task -- what its crew does, as distinct from the parent "
    "organization's mission, which is what the vehicle does "
    "(idle/produce_energy/produce_food/produce_goods/scan). For scan, "
    "optionally aim it in the same call with a compass bearing "
    "(N/NE/E/SE/S/SW/W/NW/N2/E2/S2/W2) or explicit offset_x/y/z -- aim is "
    "relative to the pod's organization and survives a move.")
@player_tool
def set_pod_task(sess, pod_id: int, task: str,
                 bearing: str = None,
                 offset_x: int = None, offset_y: int = None, offset_z: int = None) -> dict:
    """
    Set a pod's task -- what its crew does, as distinct from the parent
    organization's `mission`, which is what the vehicle does. Validates
    ownership and task type.

    For task='scan', optionally aim it in the same call with either a compass
    `bearing` ("N", "NE", "W2" -- see bearings.SCAN_BEARINGS) or an
    explicit `offset_x/y/z`. Aim is relative to the pod's own organization, not
    absolute coordinates, so it survives the ship moving. Out-of-range aims are
    rejected here rather than accepted-and-warned: an offset's range is fixed,
    so there is nothing that could later make an illegal aim legal.

    A scan pod with no aim costs its food and reveals nothing -- call
    set_pod_scan_bearing before end of turn, or aim it here.
    """
    if task not in VALID_POD_TASKS:
        return {"error": f"Invalid pod task '{task}'. Valid: {sorted(VALID_POD_TASKS)}"}
    offset = None
    if is_aiming(bearing, offset_x, offset_y, offset_z):
        if task != "scan":
            return {"error": AIM_NOT_A_SCAN.format(task=task)}
        offset, err = resolve_aim(bearing, offset_x, offset_y, offset_z)
        if err:
            return err
    cur = sess.cur
    pod = sess.own_pod(pod_id)
    if not pod:
        return {"error": POD_NOT_OWNED}
    current_turn = get_current_turn()
    result = apply_set_pod_task(cur, pod_id, pod["org_id"], sess.player_id, task, offset, current_turn)
    if "error" in result:
        return result
    return result


@mcp_tool(
    "Aim a pod already on the scan task. Same rules and same bearing "
    "vocabulary as set_org_scan_bearing. Relative aim, persists across turns, out-of- "
    "range rejected. Pass neither bearing nor offset to clear.")
@player_tool
def set_pod_scan_bearing(sess, pod_id: int, bearing: str = None,
                         offset_x: int = None, offset_y: int = None,
                         offset_z: int = None) -> dict:
    """
    Aim a pod already on the scan task, by compass bearing or explicit offset.
    Identical rules to an organization's own sensors (set_org_scan_bearing).

    The aim is relative and persists across turns, so a ship keeps scanning
    the same bearing after it moves, with no re-aiming. Pass no bearing and no
    offset to clear the aim and stop paying for it.
    """
    offset, clearing, err = resolve_or_clear(bearing, offset_x, offset_y, offset_z)
    if err:
        return err
    pod = sess.own_pod(pod_id, columns="p.id, p.task, p.org_id")
    if not pod:
        return {"error": POD_NOT_OWNED}
    if pod["task"] != "scan":
        return {"error": "Pod is not on the scan task — set its task to 'scan' first"}
    return _write_aim(sess, pod["org_id"], pod_id, offset, clearing,
                      apply_set_pod_scan_bearing, {"pod_id": pod_id})


MAX_ORG_NAME_LENGTH = 24

@mcp_tool(
    "Give one of your own ships or colonies a name of your choosing (max 24 "
    "chars). Names are how a player refers to a unit, so they must be "
    "unique among your own organizations; defaults are short and sayable "
    "(S1..Sn for ships, C1 for a colony).")
@player_tool
def rename_organization(sess, org_id: int, name: str) -> dict:
    """
    Give one of your own ships or colonies a name of your choosing.

    Bounded at MAX_ORG_NAME_LENGTH characters and stripped of surrounding
    whitespace, because the name has to fit a fleet-report column on a phone.
    Empty names are rejected rather than silently restoring the bootstrap
    default -- clearing a name is not a thing you can do; renaming is.
    """
    name = (name or "").strip()
    if not name:
        return {"error": "Name cannot be empty"}
    if len(name) > MAX_ORG_NAME_LENGTH:
        return {"error": f"Name is {len(name)} characters; limit is {MAX_ORG_NAME_LENGTH}"}
    cur = sess.cur
    cur.execute("SELECT id, name FROM organizations WHERE id=? AND player_id=?",
                (org_id, sess.player_id))
    org = cur.fetchone()
    if not org:
        return {"error": ORG_NOT_OWNED}
    cur.execute("""SELECT id FROM organizations
        WHERE player_id=? AND id!=? AND name=? COLLATE NOCASE""",
        (sess.player_id, org_id, name))
    if cur.fetchone():
        return {"error": f"You already have an organization named '{name}'"}
    previous = org["name"]
    record_event(
        event_type="organization.renamed",
        payload={"org_id": org_id, "from": previous, "to": name},
        actor_id=sess.player_id, subject_id=org_id, subject_type="organization")
    cur.execute("UPDATE organizations SET name=? WHERE id=?", (name, org_id))
    return {"ok": True, "org_id": org_id, "previous_name": previous, "name": name}


@mcp_tool(
    "Aim an organization's own sensors. Every ship and colony can scan one "
    "sector per turn on its own account -- a ship's bridge, a colony's "
    "headquarters -- without dedicating a pod to it. Identical "
    "rules to a scan pod (same food cost, range, transit "
    "suppression). Aim by compass bearing (N/NE/E/SE/S/SW/W/NW, or "
    "N2/E2/S2/W2 for two sectors out) or by explicit offset_x/y/z. The aim "
    "is RELATIVE to the org's own sector and persists across turns, so it "
    "survives a move with no re-aiming. Out-of-range aims are rejected "
    "outright. Pass neither bearing nor offset to clear it.")
@player_tool
def set_org_scan_bearing(sess, org_id: int, bearing: str = None,
                         offset_x: int = None, offset_y: int = None,
                         offset_z: int = None) -> dict:
    """
    Aim an organization's own sensors. Every ship and colony can scan one
    sector per turn on its own account -- a ship's bridge, a colony's
    headquarters -- without dedicating a pod to it.

    Aim by compass `bearing` ("N", "NE", "W2" -- see bearings.SCAN_BEARINGS)
    or by explicit `offset_x/y/z`. The aim is relative to the org's own sector
    and persists across turns, so a ship keeps scanning the same bearing after
    it moves -- set a pattern once and it travels with the hull. Pass neither
    to clear the aim and stop paying for it.
    """
    offset, clearing, err = resolve_or_clear(bearing, offset_x, offset_y, offset_z)
    if err:
        return err
    org = sess.own_org(org_id, columns="id, name")
    if not org:
        return {"error": ORG_NOT_OWNED}
    return _write_aim(sess, org_id, org_id, offset, clearing,
                      apply_set_org_scan_bearing,
                      {"org_id": org_id, "name": org["name"]})
