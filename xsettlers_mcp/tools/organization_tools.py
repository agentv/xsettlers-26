from config.loader import load_config
from db.connection import get_connection
from db.events import record_event
from engine.production import (POD_PRODUCTION, get_production_multiplier,
                               COLONIZATION_ENERGY_COST)
# _available_org_resource/_drain_org_resource are engine internals, imported
# here so the colonization charge draws from an org's pooled stock by exactly
# the same rule the turn engine uses for every other cost -- a second
# implementation would be a second place for the pooling rule to drift.
from engine.turn import (get_current_turn, get_next_tick_at, get_final_scores,
                         _available_org_resource, _drain_org_resource)
from engine.ship_log import ACTIONS as SHIP_LOG_ACTIONS
from engine.pod_tasking import resolve_aim, aim_status, apply_set_pod_task
from xsettlers_mcp.tools.navigation_tools import confirm_move
from xsettlers_mcp.tools.sector_tools import bearing_name
from datetime import datetime, timezone
import json
import os

def _tick_countdown_display(next_tick_at: str | None) -> str:
    """
    "MM:SS" until the next clock tick, or "--:--" when there's nothing
    ticking -- next_tick_at is None before any scenario is selected or
    whenever the clock process isn't the one refreshing it (see
    get_next_tick_at's docstring). Unlike scripts/status.py's _clock_status,
    this has no way to health-check a separate process -- it runs inside the
    same process the clock does, so a None value here already means "not
    currently running," no liveness probe needed.
    """
    if not next_tick_at:
        return "--:--"
    next_dt = datetime.strptime(next_tick_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    remaining = max(0, round((next_dt - datetime.now(timezone.utc)).total_seconds()))
    minutes, seconds = divmod(remaining, 60)
    return f"{minutes:02d}:{seconds:02d}"

VALID_ORG_MISSIONS = {"idle", "move", "colonize", "defend", "attack"}
VALID_POD_TASKS = {"idle", "produce_energy", "produce_food", "produce_goods", "scan"}
VALID_TRIGGER_PHASES = {"during_transit", "before_arrival", "after_arrival", "at_turn"}

# Presentation hints for status-report tools (show_civilization_status,
# show_game_status): offloads simple, repetitive formatting work onto the
# server rather than every client (LLM or not) reinventing it. Raw fields
# are always still present alongside these -- a client that wants its own
# presentation is free to ignore all of this and build from the raw data.
RESOURCE_ABBREV = {"energy": "E", "food": "F", "goods": "G"}
# Legacy bootstrap names ("Ship-P1-01", "Colony-P1"). Defaults are short
# now (S1..Sn, C1) and players can rename freely, so this only still
# matters for games bootstrapped before 2026-07-31.
_NAME_PREFIXES_TO_STRIP = ("Ship-", "Colony-")

# Locked MVP cargo-table format for a single org's status (see show_organization):
# columns are Task, Count, Energy, Food, Goods, Capacity -- Capacity as a
# "current/total" string rather than a bare number. This is a starting point,
# not a final design -- richer/graphical presentations are expected later
# (see docs/ui_and_rendering_design.md), but this is the one clients can
# render today without inventing their own column order or capacity format.
_TASK_DISPLAY = {"produce_energy": "Energy", "produce_food": "Food",
                  "produce_goods": "Goods", "idle": "Idle", "scan": "Scan"}

# Spelled-out compass names for the scanners footer (see _scanner_summary) --
# distinct from the terse codes (SCAN_BEARINGS' "N"/"NE"/"N2") used everywhere
# else, since this is a summary line meant to read in plain English rather
# than a compact table cell.
_BEARING_FULL_NAME = {
    "N": "North", "NE": "Northeast", "E": "East", "SE": "Southeast",
    "S": "South", "SW": "Southwest", "W": "West", "NW": "Northwest",
    "N2": "North (2)", "E2": "East (2)", "S2": "South (2)", "W2": "West (2)",
}

def _scanner_summary(cur, org: dict) -> tuple[list, str | None]:
    """
    Every currently-active scanner on this org -- its own innate sensor (see
    organizations.scan_offset_*) plus any pod on the scan task -- as a list of
    {"source", "bearing", "aimed"} dicts, and a ready-to-render footer line
    ("Scans: North, South, Southeast") for clients that just want the text.

    An aimed scanner shows its compass name (or the raw offset if it doesn't
    land on one of the 12 named bearings); an unaimed scan pod still costs its
    food and reveals nothing, so it's counted and flagged rather than silently
    dropped (see set_pod_task's docstring). Returns ([], None) when the org
    has no scanning capacity in use at all -- nothing to show, not an empty
    line.
    """
    scanners = []
    if org["scan_offset_x"] is not None:
        name = bearing_name(org["scan_offset_x"], org["scan_offset_y"], org["scan_offset_z"])
        display = name or f"({org['scan_offset_x']},{org['scan_offset_y']},{org['scan_offset_z']})"
        scanners.append({"source": "sensors", "bearing": display, "aimed": True})
    cur.execute("SELECT id, task_params FROM pods WHERE org_id=? AND task='scan' ORDER BY id",
                (org["id"],))
    for pod in cur.fetchall():
        if pod["task_params"]:
            p = json.loads(pod["task_params"])
            name = bearing_name(p["offset_x"], p["offset_y"], p["offset_z"])
            display = name or f"({p['offset_x']},{p['offset_y']},{p['offset_z']})"
            scanners.append({"source": f"pod {pod['id']}", "bearing": display, "aimed": True})
        else:
            scanners.append({"source": f"pod {pod['id']}", "bearing": None, "aimed": False})
    if not scanners:
        return [], None
    aimed = [_BEARING_FULL_NAME.get(s["bearing"], s["bearing"]) for s in scanners if s["aimed"]]
    unaimed = sum(1 for s in scanners if not s["aimed"])
    footer = f"Scans: {', '.join(aimed)}" if aimed else "Scans: none aimed"
    if unaimed:
        footer += f" (+{unaimed} unaimed)"
    return scanners, footer

def _short_name(name: str) -> str:
    """"Ship-P1-01" -> "P1-01", "Colony-P1" -> "P1" -- a ready-to-display
    label so clients don't need their own name-shortening rule."""
    for prefix in _NAME_PREFIXES_TO_STRIP:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name

def _tasking_summary(tasking: dict) -> str:
    """{"produce_energy": 2, "produce_food": 2} -> "E:2, F:2" -- a
    ready-to-display string using RESOURCE_ABBREV, so clients don't need to
    do the mission-name-to-abbreviation lookup themselves."""
    return ", ".join(f"{RESOURCE_ABBREV[m.replace('produce_', '')]}:{n}"
                     for m, n in tasking.items() if m.replace("produce_", "") in RESOURCE_ABBREV)

def _resource_summary(values: dict) -> str:
    """{"energy": 20.0, "food": 20.0} -> "E:20, F:20" -- same abbreviation
    convention as _tasking_summary, for any resource-keyed dict (e.g. a
    production breakdown) rather than a mission-keyed one."""
    return ", ".join(f"{RESOURCE_ABBREV[r]}:{v:g}" for r, v in values.items() if r in RESOURCE_ABBREV)

def _org_production(tasking: dict, in_transit: bool, org_type: str = "ship") -> dict:
    """
    Gross per-turn production an org's current pod deployment would yield at
    full input availability -- i.e. POD_PRODUCTION's base rate times how many
    pods are tasked to each producing task, times the org-type multiplier
    (see get_production_multiplier: colonies out-produce ships). This is the
    "nameplate" figure, not a prediction of what end_of_turn() will actually
    credit: it does not account for POD_CONSUMPTION_RECIPE input costs, org
    upkeep, or storage caps, all of which can prorate the real output down
    (see engine/turn.py).
    One real-world exception is applied here: an org in transit is parked at
    the sentinel sector (id=-1, permanently 0 energy_capacity), so its energy
    production is always 0 regardless of tasking, matching engine/turn.py's
    sector-capacity cap -- food/goods aren't sector-sourced and are unaffected.
    """
    multiplier = get_production_multiplier(org_type)
    production = {}
    for task, outputs in POD_PRODUCTION.items():
        count = tasking.get(task, 0)
        if not count:
            continue
        for resource, base_amount in outputs.items():
            amount = (0.0 if (resource == "energy" and in_transit)
                      else base_amount * count * multiplier)
            production[resource] = production.get(resource, 0.0) + amount
    return production

def _aimed_sector(cur, org_id: int, offset):
    """Absolute coordinates an org's aim currently points at, or None if it
    is in transit (no position to offset from)."""
    org = cur.execute("""SELECT s.coord_x, s.coord_y, s.coord_z FROM organizations o
        JOIN sectors s ON s.id = o.sector_id WHERE o.id=? AND o.sector_id!=-1""",
        (org_id,)).fetchone()
    if not org:
        return None
    return (org["coord_x"] + offset[0], org["coord_y"] + offset[1], org["coord_z"] + offset[2])


def set_mission(player_token: str, org_id: int, mission: str, params: dict = None) -> dict:
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
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("SELECT id,is_mobile,org_type,sector_id FROM organizations WHERE id=? AND player_id=?",
                (org_id, player["id"]))
    org = cur.fetchone()
    if not org:
        conn.close(); return {"error": "Organization not found or not owned by player"}
    if org["sector_id"] == -1:
        conn.close()
        return {"error": "This organization is currently in transit — call cancel_move before assigning a new mission"}
    if org["org_type"] == "colony":
        if mission == "move":
            conn.close(); return {"error": "Colonies cannot move"}
    elif not org["is_mobile"]:
        conn.close()
        return {"error": "This ship is committed to colonizing and cannot be reassigned until the transition resolves"}
    if mission == "move":
        conn.close()
        params = params or {}
        missing = [k for k in ("dest_x", "dest_y", "dest_z") if k not in params]
        if missing:
            return {"error": f"mission='move' requires params: {', '.join(missing)}"}
        return confirm_move(player_token, org_id, params["dest_x"], params["dest_y"],
                             params["dest_z"], params.get("jump_range_per_turn", 1))
    # Colonization is bought, not merely ordered. Checked before any event is
    # recorded so a refused conversion leaves no trace at all -- the ship is
    # untouched and the player can try again once it has fuelled up.
    if mission == "colonize":
        available_energy = _available_org_resource(cur, org_id, "energy")
        if available_energy < COLONIZATION_ENERGY_COST:
            conn.close()
            return {"error": f"Colonizing costs {COLONIZATION_ENERGY_COST:g} energy; "
                             f"this ship has {available_energy:g}",
                    "required_energy": COLONIZATION_ENERGY_COST,
                    "available_energy": available_energy}
    record_event(
        event_type="mission.set",
        payload={"org_id": org_id, "mission": mission, "params": params or {}},
        actor_id=player["id"], subject_id=org_id, subject_type="organization")
    if mission == "colonize":
        resolve_turn = get_current_turn() + 3
        record_event(
            event_type="colonize_complete",
            payload={"org_id": org_id, "sector_id": org["sector_id"],
                     "energy_cost": COLONIZATION_ENERGY_COST},
            actor_id=player["id"], subject_id=org_id, subject_type="organization",
            resolve_at_turn=resolve_turn)
        # Charge the whole conversion cost up front, at commitment rather than
        # at completion. The ship is locked for the 3-turn transition with no
        # cancel path, so there is nothing to refund and no way to dodge the
        # bill by changing your mind -- and paying now means the player sees
        # the hit while they can still reason about what caused it.
        # Drained after the record_event calls, not before: record_event opens
        # its own connection and commits, so an uncommitted write held on this
        # one would collide with it.
        _drain_org_resource(cur, org_id, "energy", COLONIZATION_ENERGY_COST)
        cur.execute("UPDATE organizations SET mission=?, mission_params=?, is_mobile=0 WHERE id=?",
                    (mission, json.dumps(params) if params else None, org_id))
    else:
        cur.execute("UPDATE organizations SET mission=?, mission_params=? WHERE id=?",
                    (mission, json.dumps(params) if params else None, org_id))
    conn.commit(); conn.close()
    result = {"ok": True, "org_id": org_id, "mission": mission}
    if mission == "colonize":
        result["energy_spent"] = COLONIZATION_ENERGY_COST
        result["completes_at_turn"] = resolve_turn
    return result

DURING_TRANSIT_ACTIONS = {"set_pod_task"}

def queue_command(player_token: str, org_id: int, trigger_phase: str, action: str,
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
    Action whitelist: 'move' (params: dest_x, dest_y, dest_z, optional
    jump_range_per_turn -- same shape confirm_move itself takes) and
    'set_pod_task' (params: pod_id, task, optionally bearing/offset_x/y/z --
    same shape set_pod_task itself takes), valid for before_arrival/
    after_arrival/at_turn; during_transit is restricted to set_pod_task only.
    If a player gives the org new orders before a before_arrival/after_arrival/
    at_turn command fires, the queued command is silently dropped rather than
    clobbering them (see engine.ship_log.dispatch_due_commands).
    """
    if trigger_phase not in VALID_TRIGGER_PHASES:
        return {"error": f"Invalid trigger_phase '{trigger_phase}'. Valid: {sorted(VALID_TRIGGER_PHASES)}"}
    if action not in SHIP_LOG_ACTIONS:
        return {"error": f"Invalid action '{action}'. Valid: {sorted(SHIP_LOG_ACTIONS)}"}
    if trigger_phase == "during_transit" and action not in DURING_TRANSIT_ACTIONS:
        return {"error": f"'during_transit' only supports: {sorted(DURING_TRANSIT_ACTIONS)}"}
    if trigger_phase == "at_turn" and turn is None:
        return {"error": "'at_turn' requires a turn parameter (the absolute turn number to fire on)"}
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("SELECT id FROM organizations WHERE id=? AND player_id=?", (org_id, player["id"]))
    if not cur.fetchone():
        conn.close(); return {"error": "Organization not found or not owned by player"}

    resolve_turn = None
    if trigger_phase in ("before_arrival", "after_arrival"):
        cur.execute("SELECT arrival_turn FROM arrival_queue WHERE org_id=?", (org_id,))
        arrival = cur.fetchone()
        if not arrival:
            conn.close()
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
    conn.commit(); conn.close()
    return {"ok": True, "command_id": command_id, "org_id": org_id, "trigger_phase": trigger_phase,
            "action": action, "resolve_turn": resolve_turn}

def set_pod_task(player_token: str, pod_id: int, task: str,
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
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""SELECT p.id, p.org_id FROM pods p JOIN organizations o ON o.id=p.org_id
        WHERE p.id=? AND o.player_id=?""", (pod_id, player["id"]))
    pod = cur.fetchone()
    if not pod:
        conn.close(); return {"error": "Pod not found or not owned by player"}
    current_turn = get_current_turn()
    result = apply_set_pod_task(cur, pod_id, pod["org_id"], player["id"], task, offset, current_turn)
    if "error" in result:
        conn.close(); return result
    conn.commit(); conn.close()
    return result


def set_pod_scan_bearing(player_token: str, pod_id: int, bearing: str = None,
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
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""SELECT p.id, p.task, p.org_id FROM pods p JOIN organizations o ON o.id=p.org_id
        WHERE p.id=? AND o.player_id=?""", (pod_id, player["id"]))
    pod = cur.fetchone()
    if not pod:
        conn.close(); return {"error": "Pod not found or not owned by player"}
    if pod["task"] != "scan":
        conn.close(); return {"error": "Pod is not on the scan task — set its task to 'scan' first"}
    if clearing:
        cur.execute("UPDATE pods SET task_params=NULL WHERE id=?", (pod_id,))
        conn.commit(); conn.close()
        return {"ok": True, "pod_id": pod_id, "cleared": True}
    status = aim_status(pod["org_id"], offset)
    if not status["in_range"]:
        conn.close()
        return {"error": f"Aim reaches {status['distance']:.2f} sectors; scan range is "
                         f"{status['scan_range']}", **status}
    params = {"offset_x": offset[0], "offset_y": offset[1], "offset_z": offset[2]}
    record_event(
        event_type="pod.scan_bearing_set",
        payload={"pod_id": pod_id, **params, "bearing": status["bearing"]},
        actor_id=player["id"], subject_id=pod_id, subject_type="pod")
    cur.execute("UPDATE pods SET task_params=? WHERE id=?", (json.dumps(params), pod_id))
    aimed = _aimed_sector(cur, pod["org_id"], offset)
    conn.commit(); conn.close()
    return {"ok": True, "pod_id": pod_id, "cleared": False,
            "aimed_at": list(aimed) if aimed else None, **status}


MAX_ORG_NAME_LENGTH = 24

def rename_organization(player_token: str, org_id: int, name: str) -> dict:
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
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("SELECT id, name FROM organizations WHERE id=? AND player_id=?",
                (org_id, player["id"]))
    org = cur.fetchone()
    if not org:
        conn.close(); return {"error": "Organization not found or not owned by player"}
    cur.execute("""SELECT id FROM organizations
        WHERE player_id=? AND id!=? AND name=? COLLATE NOCASE""",
        (player["id"], org_id, name))
    if cur.fetchone():
        conn.close(); return {"error": f"You already have an organization named '{name}'"}
    previous = org["name"]
    record_event(
        event_type="organization.renamed",
        payload={"org_id": org_id, "from": previous, "to": name},
        actor_id=player["id"], subject_id=org_id, subject_type="organization")
    cur.execute("UPDATE organizations SET name=? WHERE id=?", (name, org_id))
    conn.commit(); conn.close()
    return {"ok": True, "org_id": org_id, "previous_name": previous, "name": name}


def set_org_scan_bearing(player_token: str, org_id: int, bearing: str = None,
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
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("SELECT id, name FROM organizations WHERE id=? AND player_id=?",
                (org_id, player["id"]))
    org = cur.fetchone()
    if not org:
        conn.close(); return {"error": "Organization not found or not owned by player"}
    if clearing:
        cur.execute("""UPDATE organizations
            SET scan_offset_x=NULL, scan_offset_y=NULL, scan_offset_z=NULL WHERE id=?""", (org_id,))
        conn.commit(); conn.close()
        return {"ok": True, "org_id": org_id, "name": org["name"], "cleared": True}
    status = aim_status(org_id, offset)
    if not status["in_range"]:
        conn.close()
        return {"error": f"Aim reaches {status['distance']:.2f} sectors; scan range is "
                         f"{status['scan_range']}", **status}
    record_event(
        event_type="organization.scan_bearing_set",
        payload={"org_id": org_id, "offset_x": offset[0], "offset_y": offset[1],
                 "offset_z": offset[2], "bearing": status["bearing"]},
        actor_id=player["id"], subject_id=org_id, subject_type="organization")
    cur.execute("""UPDATE organizations
        SET scan_offset_x=?, scan_offset_y=?, scan_offset_z=? WHERE id=?""",
        (offset[0], offset[1], offset[2], org_id))
    aimed = _aimed_sector(cur, org_id, offset)
    conn.commit(); conn.close()
    return {"ok": True, "org_id": org_id, "name": org["name"], "cleared": False,
            "aimed_at": list(aimed) if aimed else None, **status}


def show_organization(player_token: str, org_id: int) -> dict:
    """
    Return the complete properties of one of the player's own organizations:
    org record (type, mission, mission_params, is_mobile, sector location)
    plus pods grouped by task — count of pods on each task, total
    capacity of that group, and what's actually stored there broken down by
    resource type (energy/food/goods). Storage is generic per pod and
    independent of current task (see engine/turn.py), so a task group's
    contents don't necessarily match its own task -- e.g. produce_goods pods
    can still be holding energy leftover from before a retask. Individual
    pods aren't listed separately; a ship with 6 pods reads as up to 3 task
    rows, not 6 pod rows.
    display: presentation hints (see RESOURCE_ABBREV/_short_name/_TASK_DISPLAY
    module docstring) -- a ready-to-render header ("<name> — at (x,y,z), <mission>")
    and the locked MVP column order for the cargo table (Task, Count, Energy,
    Food, Goods, Capacity). Each task row also gets task_display (e.g.
    "produce_energy" -> "Energy") and capacity_display ("current/total", e.g.
    "200/200") alongside the raw fields -- all raw fields stay present, this
    is purely additive. `display.rows_key` ("tasks") names which top-level
    field holds the row list, for a generic renderer -- see views/render.py.
    `scanners` (see _scanner_summary) lists every active scanner on this org
    -- innate sensors plus scan-task pods -- and `display.footer`, when
    present, is that same information as a ready-to-render line below the
    table ("Scans: North, South, Southeast").
    Ownership-gated — only the calling player's orgs are accessible.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}
    cur.execute("""
        SELECT o.id, o.org_type, o.name, o.mission, o.mission_params,
               o.is_mobile, o.sector_id,
               o.scan_offset_x, o.scan_offset_y, o.scan_offset_z,
               s.coord_x, s.coord_y, s.coord_z
        FROM organizations o
        LEFT JOIN sectors s ON s.id = o.sector_id
        WHERE o.id = ? AND o.player_id = ?""", (org_id, player["id"]))
    org = cur.fetchone()
    if not org:
        conn.close(); return {"error": "Organization not found or not owned by player"}
    result = dict(org)
    cur.execute("""
        SELECT task, COUNT(*) AS count,
               SUM(storage_capacity) AS capacity,
               SUM(energy_stored) AS energy,
               SUM(food_stored) AS food,
               SUM(goods_stored) AS goods
        FROM pods WHERE org_id = ? GROUP BY task""", (org_id,))
    result["tasks"] = [dict(t) for t in cur.fetchall()]
    for t in result["tasks"]:
        t["task_display"] = _TASK_DISPLAY.get(t["task"], t["task"])
        current = (t["energy"] or 0) + (t["food"] or 0) + (t["goods"] or 0)
        t["capacity_display"] = f"{current:.0f}/{t['capacity']:.0f}"
    status = (f"at ({org['coord_x']},{org['coord_y']},{org['coord_z']})"
              if org["sector_id"] != -1 else "in transit")
    result["short_name"] = _short_name(org["name"])
    result["status"] = status
    scanners, scanners_footer = _scanner_summary(cur, org)
    result["scanners"] = scanners
    result["display"] = {
        "header": f"{org['name']} — {status}, {org['mission']}",
        "resource_abbrev": RESOURCE_ABBREV,
        "rows_key": "tasks",
        "columns": ["task_display", "count", "energy", "food", "goods", "capacity_display"],
        "column_labels": {"task_display": "Task", "capacity_display": "Utilization"},
    }
    if scanners_footer:
        result["display"]["footer"] = scanners_footer
    conn.close()
    return result

def show_civilization_status(player_token: str) -> dict:
    """
    Return a player-scoped fleet/status report (aliases: "fleet status",
    "my status") -- the full roster (ships and colonies) plus fleet-wide
    aggregates in one call:
    - Turn context: current turn, turn limit, and next_tick_at (ISO8601, from
      engine/clock.py -- None if the clock has never run or is paused; a
      caller also needs to check the server is actually live before trusting
      it, since a paused clock leaves a stale value -- see get_next_tick_at()
      and scripts/status.py for how the CLI report reconciles the two)
    - All player organizations (ships and colonies) with name, org_type, mission,
      sector location, a per-org cargo summary (current/capacity summed across
      that org's pods), a per-org storage breakdown (energy/food/goods
      currently held, summed across that org's pods regardless of each pod's
      own task -- storage is generic per pod, see RESOURCE_STORAGE_COLUMN),
      a tasking breakdown (pod count per task, e.g.
      {"produce_energy": 2, "produce_food": 2, "produce_goods": 2} -- this is
      the pod-deployment picture), and a production breakdown (see
      _org_production -- gross per-turn output at full input availability,
      not netted against consumption costs). Ships in
      transit are marked with in_transit=True, destination sector, expected
      arrival turn, and turns_remaining as raw fields -- arrival_turn is the
      turn the ship is actually free to act, not the turn whose end_of_turn()
      pass performs the landing (that happens one turn earlier; see
      engine/turn.py's arrival resolution). turns_remaining counts down to
      that same turn, so it only reaches 0 once the ship has actually landed
      and can take a new mission. The
      display `status` string itself is intentionally terse -- just "in
      transit", full stop, no destination or ETA -- a player asked for this
      view to be that minimal; dest_sector/turns_remaining/arrival_turn are
      still real raw fields on the entry for a client that wants to build a
      richer status string itself.
    - Accumulated assets: aggregate energy, food, goods, and total across all
      pods, plus total capacity and percent_full.
    - display: presentation hints (see RESOURCE_ABBREV/_short_name/_tasking_summary
      module docstring) -- every org also carries ready-to-use short_name,
      status, cargo_display, tasking_summary, and production_summary fields
      alongside the raw ones, so a client with no LLM in the loop doesn't have
      to build its own formatting logic. `display.rows_key` names which
      top-level field holds the row list ("organizations", here) and
      `display.columns` lists which of each row's fields to render, in order
      -- see views/render.py's render_status() for a renderer driven entirely
      by these hints, with no per-tool special-casing. All raw fields are
      still present; this is purely additive.
    Ownership-gated — only the calling player's data.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    player = cur.fetchone()
    if not player:
        conn.close(); return {"error": "Player not found"}

    # Turn context
    cur.execute("SELECT current_turn FROM game_state WHERE id=1")
    current_turn = cur.fetchone()["current_turn"]
    turn_limit = int(os.getenv("TURN_LIMIT", 20))
    next_tick_at = get_next_tick_at()

    # Organizations
    cur.execute("""
        SELECT o.id, o.name, o.org_type, o.mission, o.mission_params,
               o.sector_id, s.coord_x, s.coord_y, s.coord_z
        FROM organizations o
        LEFT JOIN sectors s ON s.id = o.sector_id
        WHERE o.player_id = ?
        ORDER BY o.org_type, o.name""", (player["id"],))
    orgs_raw = cur.fetchall()
    orgs = []
    for o in orgs_raw:
        cargo = cur.execute("""SELECT SUM(energy_stored+food_stored+goods_stored) AS current,
            SUM(storage_capacity) AS capacity,
            SUM(energy_stored) AS energy, SUM(food_stored) AS food, SUM(goods_stored) AS goods
            FROM pods WHERE org_id=?""",
            (o["id"],)).fetchone()
        storage = {"energy": cargo["energy"] or 0.0, "food": cargo["food"] or 0.0,
                   "goods": cargo["goods"] or 0.0}
        tasks = cur.execute("SELECT task, COUNT(*) AS n FROM pods WHERE org_id=? GROUP BY task",
                           (o["id"],)).fetchall()
        tasking = {t["task"]: t["n"] for t in tasks}
        in_transit = o["sector_id"] == -1
        production = _org_production(tasking, in_transit, o["org_type"])
        entry = {
            "id": o["id"],
            "name": o["name"],
            "short_name": _short_name(o["name"]),
            "org_type": o["org_type"],
            "mission": o["mission"],
            "cargo": {"current": cargo["current"] or 0.0, "capacity": cargo["capacity"] or 0.0},
            "cargo_display": f"{cargo['current'] or 0.0:.0f}/{cargo['capacity'] or 0.0:.0f}",
            "storage": storage,
            "storage_summary": _resource_summary(storage),
            "tasking": tasking,
            "tasking_summary": _tasking_summary(tasking),
            "production": production,
            "production_summary": _resource_summary(production),
        }
        if o["sector_id"] == -1:
            # In transit — fetch destination and arrival turn from arrival_queue.
            # No join to sectors: the destination may not exist as a row yet
            # (sectors are lazily instantiated, see db/sectors.py) until arrival.
            cur.execute("""
                SELECT dest_x, dest_y, dest_z, arrival_turn
                FROM arrival_queue WHERE org_id = ?""", (o["id"],))
            aq = cur.fetchone()
            entry["in_transit"] = True
            if aq:
                entry["dest_sector"] = {
                    "coords": [aq["dest_x"], aq["dest_y"], aq["dest_z"]]
                }
                entry["arrival_turn"] = aq["arrival_turn"]
                # arrival_turn is the turn the ship is actually free to act
                # -- landing itself happens one turn earlier, during the
                # end_of_turn() pass for arrival_turn-1 (see engine/turn.py).
                # turns_remaining counts down to arrival_turn directly, so it
                # reaches 0 exactly when the ship can take a new mission, not
                # one turn before.
                # turns_remaining/arrival_turn/dest_sector stay as raw fields
                # -- no longer folded into the display string. A player asked
                # for the display status to just say "in transit", full stop
                # -- destination and ETA are still on the entry for a client
                # that wants to build its own richer view from the raw data.
                entry["turns_remaining"] = max(0, aq["arrival_turn"] - current_turn)
                entry["status"] = "in transit"
            else:
                entry["status"] = "in transit"
        else:
            entry["in_transit"] = False
            entry["sector"] = {
                "id": o["sector_id"],
                "coords": [o["coord_x"], o["coord_y"], o["coord_z"]]
            }
            entry["status"] = f"at ({o['coord_x']},{o['coord_y']},{o['coord_z']})"
        orgs.append(entry)

    # Accumulated assets — aggregate across all pods for this player
    cur.execute("""
        SELECT
            SUM(p.energy_stored) AS energy,
            SUM(p.food_stored) AS food,
            SUM(p.goods_stored) AS goods,
            SUM(p.energy_stored+p.food_stored+p.goods_stored) AS total,
            SUM(p.storage_capacity) AS capacity
        FROM pods p
        JOIN organizations o ON o.id = p.org_id
        WHERE o.player_id = ?""", (player["id"],))
    assets_row = cur.fetchone()
    total = assets_row["total"] or 0
    capacity = assets_row["capacity"] or 0
    assets = {
        "energy":       round(assets_row["energy"] or 0, 2),
        "food":         round(assets_row["food"]   or 0, 2),
        "goods":        round(assets_row["goods"]  or 0, 2),
        "total":        round(total, 2),
        "capacity":     round(capacity, 2),
        "percent_full": round(total / capacity * 100, 1) if capacity else 0.0,
    }

    conn.close()
    return {
        "turn": current_turn,
        "turn_limit": turn_limit,
        "next_tick_at": next_tick_at,
        "organizations": orgs,
        "assets": assets,
        "display": {
            "resource_abbrev": RESOURCE_ABBREV,
            "rows_key": "organizations",
            "columns": ["short_name", "status", "cargo_display", "storage_summary",
                        "tasking_summary", "production_summary"],
            "column_labels": {
                "short_name": "Unit", "status": "Status", "cargo_display": "Cargo",
                "storage_summary": "Storage", "tasking_summary": "Tasking",
                "production_summary": "Production/turn",
            },
        },
    }

def show_game_status(player_token: str) -> dict:
    """
    Return the public scoreboard -- turn context (including next_tick_at,
    see show_civilization_status's docstring for the caveat about a paused
    clock, and next_tick_countdown -- the same value pre-formatted "MM:SS",
    or "--:--" when next_tick_at is None) plus every player's aggregate
    resource totals, side by side.
    Unlike show_civilization_status
    (or every other tool in this module), this is NOT ownership-gated to the
    caller's own data: aggregate totals are treated as public standing, the
    same information _calculate_final_scores already reveals at game-over,
    just available on demand throughout the game instead of only at the end.
    player_token is only used to confirm the caller is a real player in this
    game -- it does not filter or restrict what's returned. Detailed fleet
    composition, position, and tasking of other players is NOT included here
    and stays private -- only aggregate resource totals are public.
    `score` is the actual game score, not just another resource total:
    energy/food/goods currently held are weighted by `config/game_config.yaml`'s
    `score_weights` (as of 2026-07-30: 0/1/2 respectively -- energy is a means
    of production, not a scored asset; goods score double food) and summed.
    Same formula `engine/turn.py`'s `_calculate_final_scores()` uses to decide
    the actual winner at game-over -- this tool just makes it checkable on
    demand throughout the game instead of only at the end. Standings are
    ranked by `score` (highest first, "rank" field included), NOT by the raw
    `total` (an unweighted sum, still included for capacity/fullness context).
    Carries a display block (resource_abbrev, rows_key="standings", suggested
    column order) for clients that want a ready-to-use presentation instead
    of building one -- see views/render.py's render_status().
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id FROM players WHERE player_token=?", (player_token,))
    if not cur.fetchone():
        conn.close(); return {"error": "Player not found"}

    cur.execute("SELECT current_turn FROM game_state WHERE id=1")
    current_turn = cur.fetchone()["current_turn"]
    turn_limit = int(os.getenv("TURN_LIMIT", 20))
    next_tick_at = get_next_tick_at()
    weights = load_config().game.score_weights

    cur.execute("""
        SELECT p.id AS player_id, p.display_name,
               SUM(pods.energy_stored) AS energy,
               SUM(pods.food_stored) AS food,
               SUM(pods.goods_stored) AS goods,
               SUM(pods.energy_stored+pods.food_stored+pods.goods_stored) AS total,
               SUM(pods.storage_capacity) AS capacity
        FROM players p
        LEFT JOIN organizations o ON o.player_id = p.id
        LEFT JOIN pods ON pods.org_id = o.id
        GROUP BY p.id""")
    standings = []
    for row in cur.fetchall():
        total = row["total"] or 0
        capacity = row["capacity"] or 0
        energy, food, goods = row["energy"] or 0, row["food"] or 0, row["goods"] or 0
        score = (energy * weights.get("energy", 0) + food * weights.get("food", 0)
                 + goods * weights.get("goods", 0))
        standings.append({
            "player_id": row["player_id"],
            "display_name": row["display_name"],
            "energy": round(energy, 2),
            "food": round(food, 2),
            "goods": round(goods, 2),
            "total": round(total, 2),
            "capacity": round(capacity, 2),
            "utilization": round(total / capacity * 100, 1) if capacity else 0.0,
            "score": round(score, 2),
        })
    standings.sort(key=lambda s: s["score"], reverse=True)
    for rank, s in enumerate(standings, start=1):
        s["rank"] = rank
    conn.close()

    # Once the game is over this becomes the end-of-game scoreboard: the
    # RECORDED result, not a recomputation. get_final_scores() reads the
    # game.final_scores event written at the whistle, so what a player is
    # shown afterwards is what actually happened, and stays right even if
    # anything touches state later.
    final = get_final_scores()
    game_over = final is not None
    if game_over:
        standings = final["standings"]
        for s in standings:
            s.setdefault("utilization",
                         round(s["total"] / s["capacity"] * 100, 1) if s.get("capacity") else 0.0)
    next_tick_countdown = _tick_countdown_display(next_tick_at)
    header = (f"FINAL — game over at turn {final['final_turn']} of {final['turn_limit']}. "
              f"Winner: {final['winner']}" if game_over
              else f"Turn {current_turn} of {turn_limit} ({next_tick_countdown})")

    # Whole-number display variants -- score/energy/food/goods never carry a
    # meaningful fraction (production and upkeep are integer per-turn amounts),
    # so the raw rounded-to-2dp float is noise in a table meant for a phone
    # screen. Additive, like every other _display field: the raw floats above
    # are untouched for a client that wants to compute with them.
    for s in standings:
        s["score_display"] = f"{s['score']:.0f}"
        s["energy_display"] = f"{s['energy']:.0f}"
        s["food_display"] = f"{s['food']:.0f}"
        s["goods_display"] = f"{s['goods']:.0f}"

    return {
        "turn": current_turn,
        "turn_limit": turn_limit,
        "next_tick_at": next_tick_at,
        "next_tick_countdown": next_tick_countdown,
        "game_over": game_over,
        "is_final": game_over,
        "winner": final["winner"] if game_over else None,
        "score_weights": final["score_weights"] if game_over else dict(weights),
        "standings": standings,
        "display": {
            "header": header,
            "resource_abbrev": RESOURCE_ABBREV,
            "rows_key": "standings",
            # utilization deliberately excluded from this report -- still a
            # raw field on every standings row, just not part of the table.
            "columns": ["rank", "display_name", "score_display", "energy_display",
                        "food_display", "goods_display"],
            "column_labels": {"display_name": "Player", "score_display": "score",
                               "energy_display": "energy", "food_display": "food",
                               "goods_display": "goods"},
        },
    }
