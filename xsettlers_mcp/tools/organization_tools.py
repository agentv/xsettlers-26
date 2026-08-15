from db.events import record_event
from xsettlers_mcp.tools.session import player_tool, ORG_NOT_OWNED, POD_NOT_OWNED
from engine.turn import get_current_turn
from engine.missions import apply_colonize
from engine.org_scanning import apply_set_org_scan_bearing
from engine.ship_log import ACTIONS as SHIP_LOG_ACTIONS
from engine.pod_tasking import resolve_aim, aim_status, apply_set_pod_task
from xsettlers_mcp.tools.navigation_tools import confirm_move
import json


VALID_ORG_MISSIONS = {"idle", "move", "colonize", "defend", "attack"}
VALID_POD_TASKS = {"idle", "produce_energy", "produce_food", "produce_goods", "scan"}
VALID_TRIGGER_PHASES = {"during_transit", "before_arrival", "after_arrival", "at_turn"}


def _aimed_sector(cur, org_id: int, offset):
    """Absolute coordinates an org's aim currently points at, or None if it
    is in transit (no position to offset from)."""
    org = cur.execute("""SELECT s.coord_x, s.coord_y, s.coord_z FROM organizations o
        JOIN sectors s ON s.id = o.sector_id WHERE o.id=? AND o.sector_id!=-1""",
        (org_id,)).fetchone()
    if not org:
        return None
    return (org["coord_x"] + offset[0], org["coord_y"] + offset[1], org["coord_z"] + offset[2])


@player_tool
def set_mission(sess, org_id: int, mission: str, params: dict = None) -> dict:
    """
    Set an organization's mission. Validates ownership and mission type, and
    enforces the three org-lock states (see Data Model & Storage Design):
      - in transit (sector_id == -1): locked entirely, must cancel_move first
      - colony: locked against 'move' only — defend/attack/idle remain assignable
      - mid-colonization (ship, is_mobile == 0, not in transit): locked entirely,
        committed for the 3-turn transition window
    Setting mission='colonize' costs COLONIZATION_ENERGY_COST energy, drawn
    from the ship's pooled stock and charged in full at commitment; a ship
    that cannot pay is refused outright and left untouched. On payment it
    flips is_mobile to 0 immediately and schedules a colonize_complete event
    3 turns out for engine/turn.py to resolve.
    Setting mission='move' delegates entirely to navigation_tools.confirm_move
    (params must include dest_x/dest_y/dest_z, optionally jump_range_per_turn)
    rather than writing mission='move' onto the row directly -- confirm_move is
    what actually parks the org at the sentinel sector and queues the
    arrival_queue row; without it the org would sit forever with mission='move'
    and nothing to ever resolve it.
    """
    if mission not in VALID_ORG_MISSIONS:
        return {"error": f"Invalid mission '{mission}'. Valid: {sorted(VALID_ORG_MISSIONS)}"}
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
                             params["dest_x"], params["dest_y"],
                             params["dest_z"], params.get("jump_range_per_turn", 1))
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

DURING_TRANSIT_ACTIONS = {"set_pod_task"}


def _normalize_queued_params(cur, org_id: int, action: str, params: dict):
    """
    Validate a queued command's params at QUEUE time and return them
    normalized, as (params, None) -- or (None, error) if the order could
    never work.

    The dispatchers (engine/ship_log.py, engine/movement._dispatch_during_transit)
    index straight into the stored params dict and hand it to helpers that do
    no ownership check and no task-validity check of their own, so this is
    where those checks live. Without them: a bad `task` string reaches the
    pods CHECK constraint and raises sqlite3.IntegrityError *inside
    end_of_turn()*, aborting the turn for every player and re-firing on every
    subsequent tick (the queue row is deleted only after its handler returns);
    a missing dest_y/dest_z raises KeyError the same way; and a `pod_id`
    belonging to another player's org would be retasked.

    Validating here rather than at dispatch is also what the player needs: an
    order rejected three turns later by a background clock has no one to tell.
    Refusing it in the tool call that created it puts the error in front of
    the player while they can still fix it -- the same reasoning set_mission
    uses to charge colonization up front.

    Normalization serves the same end: a compass `bearing` is resolved to
    offset_x/y/z here, because both dispatchers read only the offsets and
    would otherwise silently drop an aim the player was told they could pass.
    """
    if action == "move":
        absolute = [k for k in ("dest_x", "dest_y", "dest_z") if params.get(k) is not None]
        relative = [k for k in ("d_x", "d_y", "d_z") if params.get(k) is not None]
        if absolute and relative:
            return None, {"error": "action='move' takes either dest_x/dest_y/dest_z "
                                   "(absolute) or d_x/d_y/d_z (relative to wherever "
                                   "the org is when the order fires), not both"}
        if relative:
            if len(relative) < 3:
                missing = [k for k in ("d_x", "d_y", "d_z") if params.get(k) is None]
                return None, {"error": f"action='move' requires params: {', '.join(missing)}"}
            # No negative check here: a relative move's absolute destination
            # depends on where the org ends up, which is unknowable now and is
            # the whole point of the form. engine.ship_log.resolve_destination
            # checks it at fire time instead.
            return params, None
        if len(absolute) < 3:
            missing = [k for k in ("dest_x", "dest_y", "dest_z") if params.get(k) is None]
            return None, {"error": f"action='move' requires params: {', '.join(missing)}"}
        if any(params[k] < 0 for k in ("dest_x", "dest_y", "dest_z")):
            return None, {"error": "Destination coordinates cannot be negative "
                                   "-- space has no negative indices"}
        return params, None

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
        aiming = params.get("bearing") is not None or any(
            params.get(c) is not None for c in ("offset_x", "offset_y", "offset_z"))
        if not aiming:
            return {}, None  # clears the org's aim
        offset, err = resolve_aim(params.get("bearing"), params.get("offset_x"),
                                  params.get("offset_y"), params.get("offset_z"))
        if err:
            return None, err
        status = aim_status(org_id, offset)
        if not status["in_range"]:
            return None, {"error": f"Aim reaches {status['distance']:.2f} sectors; scan range is "
                                   f"{status['scan_range']}", **status}
        return {"offset_x": offset[0], "offset_y": offset[1], "offset_z": offset[2],
                "bearing": status["bearing"]}, None

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

    aiming = params.get("bearing") is not None or any(
        params.get(c) is not None for c in ("offset_x", "offset_y", "offset_z"))
    if not aiming:
        return params, None
    if task != "scan":
        return None, {"error": f"Only the scan task takes an aim; '{task}' does not"}
    offset, err = resolve_aim(params.get("bearing"), params.get("offset_x"),
                              params.get("offset_y"), params.get("offset_z"))
    if err:
        return None, err
    status = aim_status(org_id, offset)
    if not status["in_range"]:
        return None, {"error": f"Aim reaches {status['distance']:.2f} sectors; scan range is "
                               f"{status['scan_range']}", **status}
    normalized = {k: v for k, v in params.items() if k != "bearing"}
    normalized.update(offset_x=offset[0], offset_y=offset[1], offset_z=offset[2])
    return normalized, None

@player_tool
def queue_command(sess, org_id: int, trigger_phase: str, action: str,
                  params: dict = None, turn: int = None) -> dict:
    """
    Queue a one-shot command for this org, resolved automatically by the
    engine rather than requiring the player to call the underlying tool
    again by hand, at whichever of four fixed primitives is named:
      - 'during_transit': fires the instant this org enters transit (event-
        triggered by the next confirm_move on this org, not turn-based --
        dispatched from engine.movement.apply_confirm_move, not the turn
        sweep). Only 'set_pod_task' is a legal action here -- pod tasking is
        the one thing not locked by an org entering transit, which is the
        whole reason this phase exists.
      - 'before_arrival': fires the same tick this org's current move
        resolves. Requires the org to already be in transit -- there must be
        a pending arrival_queue row to anchor the resolve turn to.
      - 'after_arrival': fires exactly one end_of_turn() pass later. Same
        in-transit requirement as before_arrival.
      - 'at_turn': fires at an explicit absolute turn (the `turn` param,
        required for this phase only) -- independent of any move, for orders
        that don't fit the arrival-relative phases at all (e.g. "on turn 7,
        jump somewhere else"). No in-transit requirement.
    Action whitelist, all valid for before_arrival/after_arrival/at_turn
    (during_transit is restricted to set_pod_task only):
      - 'move' -- either dest_x/dest_y/dest_z (absolute, the same shape
        confirm_move takes) or d_x/d_y/d_z (relative to wherever this org is
        standing when the order fires), never both, plus optional
        jump_range_per_turn. The relative form is what makes an order portable
        between starting positions -- "three further out the way I'm heading"
        rather than a fixed coordinate.
      - 'set_pod_task' -- pod_id, task, optionally bearing/offset_x/y/z, same
        shape set_pod_task itself takes.
      - 'colonize' -- no params. Commits this ship to becoming a colony, at
        whatever sector it occupies when the order fires. Alone among these,
        it can be refused at fire time rather than failing: colonizing is paid
        for in energy, and a ship that cannot pay when the moment arrives is
        declined and left untouched (logged as alert.queued_command_refused).
      - 'aim_scan' -- optionally bearing/offset_x/y/z, same shape
        set_org_scan_bearing takes; pass none of them to clear the aim.
    If a player gives the org new orders before a before_arrival/after_arrival/
    at_turn command fires, the queued command is silently dropped rather than
    clobbering them (see engine.ship_log.dispatch_due_commands).

    Params are fully validated here, when the order is given, not when the
    clock fires it (see _normalize_queued_params): the pod must belong to this
    org, the task must be a real task, a move needs all three non-negative
    destination coordinates, and a scan aim must be in range. An order that
    could never work is refused outright -- nothing is queued, the pod keeps
    whatever mission it already had, and the player is told why while they can
    still do something about it.
    """
    if trigger_phase not in VALID_TRIGGER_PHASES:
        return {"error": f"Invalid trigger_phase '{trigger_phase}'. Valid: {sorted(VALID_TRIGGER_PHASES)}"}
    if action not in SHIP_LOG_ACTIONS:
        return {"error": f"Invalid action '{action}'. Valid: {sorted(SHIP_LOG_ACTIONS)}"}
    if trigger_phase == "during_transit" and action not in DURING_TRANSIT_ACTIONS:
        return {"error": f"'during_transit' only supports: {sorted(DURING_TRANSIT_ACTIONS)}"}
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
    if trigger_phase in ("before_arrival", "after_arrival"):
        cur.execute("SELECT arrival_turn FROM arrival_queue WHERE org_id=?", (org_id,))
        arrival = cur.fetchone()
        if not arrival:
            return {"error": f"'{trigger_phase}' requires the organization to currently be "
                             f"in transit (no pending arrival found)"}
        resolve_turn = arrival["arrival_turn"] + (0 if trigger_phase == "before_arrival" else 1)
    elif trigger_phase == "at_turn":
        resolve_turn = turn
    # during_transit: resolve_turn stays None -- dispatched by org_id+phase
    # lookup inside apply_confirm_move, not the resolve_turn sweep.

    current_turn = get_current_turn()
    cur.execute("""INSERT INTO org_command_queue (org_id,trigger_phase,resolve_turn,action,params,created_turn)
                   VALUES (?,?,?,?,?,?)""",
                (org_id, trigger_phase, resolve_turn, action, json.dumps(params or {}), current_turn))
    command_id = cur.lastrowid
    return {"ok": True, "command_id": command_id, "org_id": org_id, "trigger_phase": trigger_phase,
            "action": action, "resolve_turn": resolve_turn}

@player_tool
def set_pod_task(sess, pod_id: int, task: str,
                 bearing: str = None,
                 offset_x: int = None, offset_y: int = None, offset_z: int = None) -> dict:
    """
    Set a pod's task -- what its crew does, as distinct from the parent
    organization's `mission`, which is what the vehicle does. Validates
    ownership and task type.

    For task='scan', optionally aim it in the same call with either a compass
    `bearing` ("N", "NE", "W2" -- see sector_tools.SCAN_BEARINGS) or an
    explicit `offset_x/y/z`. Aim is relative to the pod's own organization, not
    absolute coordinates, so it survives the ship moving. Out-of-range aims are
    rejected here rather than accepted-and-warned: an offset's range is fixed,
    so there is nothing that could later make an illegal aim legal.

    A scan pod with no aim costs its food and reveals nothing -- call
    set_pod_scan_bearing before end of turn, or aim it here.
    """
    if task not in VALID_POD_TASKS:
        return {"error": f"Invalid pod task '{task}'. Valid: {sorted(VALID_POD_TASKS)}"}
    aiming = bearing is not None or any(c is not None for c in (offset_x, offset_y, offset_z))
    offset = None
    if aiming:
        if task != "scan":
            return {"error": f"Only the scan task takes an aim; '{task}' does not"}
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


@player_tool
def set_pod_scan_bearing(sess, pod_id: int, bearing: str = None,
                         offset_x: int = None, offset_y: int = None,
                         offset_z: int = None) -> dict:
    """
    Aim a pod already on the scan task, by compass bearing or explicit offset.
    Identical rules to an organization's own sensors (set_org_scan_bearing) --
    scanning is scanning, whoever carries the equipment.

    The aim is relative and persists across turns, so a ship keeps scanning
    the same bearing after it moves, with no re-aiming. Pass no bearing and no
    offset to clear the aim and stop paying for it.
    """
    clearing = bearing is None and all(c is None for c in (offset_x, offset_y, offset_z))
    offset = None
    if not clearing:
        offset, err = resolve_aim(bearing, offset_x, offset_y, offset_z)
        if err:
            return err
    cur = sess.cur
    pod = sess.own_pod(pod_id, columns="p.id, p.task, p.org_id")
    if not pod:
        return {"error": POD_NOT_OWNED}
    if pod["task"] != "scan":
        return {"error": "Pod is not on the scan task — set its task to 'scan' first"}
    if clearing:
        cur.execute("UPDATE pods SET task_params=NULL WHERE id=?", (pod_id,))
        return {"ok": True, "pod_id": pod_id, "cleared": True}
    status = aim_status(pod["org_id"], offset)
    if not status["in_range"]:
        return {"error": f"Aim reaches {status['distance']:.2f} sectors; scan range is "
                         f"{status['scan_range']}", **status}
    params = {"offset_x": offset[0], "offset_y": offset[1], "offset_z": offset[2]}
    record_event(
        event_type="pod.scan_bearing_set",
        payload={"pod_id": pod_id, **params, "bearing": status["bearing"]},
        actor_id=sess.player_id, subject_id=pod_id, subject_type="pod")
    cur.execute("UPDATE pods SET task_params=? WHERE id=?", (json.dumps(params), pod_id))
    aimed = _aimed_sector(cur, pod["org_id"], offset)
    return {"ok": True, "pod_id": pod_id, "cleared": False,
            "aimed_at": list(aimed) if aimed else None, **status}


MAX_ORG_NAME_LENGTH = 24

@player_tool
def rename_organization(sess, org_id: int, name: str) -> dict:
    """
    Give one of your own ships or colonies a name of your choosing.

    Names are how a player actually refers to a unit ("send S3 north", "what
    is Fort Hope holding"), so they are required to be unique among that
    player's own organizations -- an ambiguous name is not a name. Uniqueness
    is per player, not global: two players may each field a "Vanguard", and
    neither can see the other's roster anyway.

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


@player_tool
def set_org_scan_bearing(sess, org_id: int, bearing: str = None,
                         offset_x: int = None, offset_y: int = None,
                         offset_z: int = None) -> dict:
    """
    Aim an organization's own sensors. Every ship and colony can scan one
    sector per turn on its own account -- a ship's bridge, a colony's
    headquarters -- without dedicating a pod to it.

    Scanning is scanning: identical in every rule to a scan pod
    (set_pod_scan_bearing) -- same food cost, same range, same suppression in
    transit, same relative aiming. An org that also carries scan pods gets
    both, and each pays its own way.

    Aim by compass `bearing` ("N", "NE", "W2" -- see sector_tools.SCAN_BEARINGS)
    or by explicit `offset_x/y/z`. The aim is relative to the org's own sector
    and persists across turns, so a ship keeps scanning the same bearing after
    it moves -- set a pattern once and it travels with the hull. Pass neither
    to clear the aim and stop paying for it.
    """
    clearing = bearing is None and all(c is None for c in (offset_x, offset_y, offset_z))
    offset = None
    if not clearing:
        offset, err = resolve_aim(bearing, offset_x, offset_y, offset_z)
        if err:
            return err
    cur = sess.cur
    cur.execute("SELECT id, name FROM organizations WHERE id=? AND player_id=?",
                (org_id, sess.player_id))
    org = cur.fetchone()
    if not org:
        return {"error": ORG_NOT_OWNED}
    if clearing:
        apply_set_org_scan_bearing(cur, org_id, sess.player_id, None, get_current_turn())
        return {"ok": True, "org_id": org_id, "name": org["name"], "cleared": True}
    status = aim_status(org_id, offset)
    if not status["in_range"]:
        return {"error": f"Aim reaches {status['distance']:.2f} sectors; scan range is "
                         f"{status['scan_range']}", **status}
    apply_set_org_scan_bearing(cur, org_id, sess.player_id, offset,
                               get_current_turn(), bearing=status["bearing"])
    aimed = _aimed_sector(cur, org_id, offset)
    return {"ok": True, "org_id": org_id, "name": org["name"], "cleared": False,
            "aimed_at": list(aimed) if aimed else None, **status}
