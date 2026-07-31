from config.loader import load_config
from db.connection import get_connection
from db.events import record_event
from engine.production import POD_PRODUCTION
from engine.turn import get_current_turn, get_next_tick_at
from xsettlers_mcp.tools.navigation_tools import confirm_move
from xsettlers_mcp.tools.sector_tools import get_scan_range
import json, math

VALID_ORG_MISSIONS = {"idle", "move", "colonize", "defend", "attack"}
VALID_POD_TASKS = {"idle", "produce_energy", "produce_food", "produce_goods", "scan"}

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

def _org_production(tasking: dict, in_transit: bool) -> dict:
    """
    Gross per-turn production an org's current pod deployment would yield at
    full input availability -- i.e. POD_PRODUCTION's base rate times how many
    pods are tasked to each producing task. This is the "nameplate" figure,
    not a prediction of what end_of_turn() will actually credit: it does not
    account for POD_CONSUMPTION_RECIPE input costs, org upkeep, or storage
    caps, all of which can prorate the real output down (see engine/turn.py).
    One real-world exception is applied here: an org in transit is parked at
    the sentinel sector (id=-1, permanently 0 energy_capacity), so its energy
    production is always 0 regardless of tasking, matching engine/turn.py's
    sector-capacity cap -- food/goods aren't sector-sourced and are unaffected.
    """
    production = {}
    for task, outputs in POD_PRODUCTION.items():
        count = tasking.get(task, 0)
        if not count:
            continue
        for resource, base_amount in outputs.items():
            amount = 0.0 if (resource == "energy" and in_transit) else base_amount * count
            production[resource] = production.get(resource, 0.0) + amount
    return production

def _scan_target_status(cur, org_id: int, target_x: int, target_y: int, target_z: int) -> dict:
    """
    Full picture for a scan target the moment it's set: the org's current
    coordinates (so the player has a reference point without a separate
    lookup), the distance to the target, the org's scan range, and whether
    it's in range -- computed immediately, so the player finds out right
    away rather than only at end-of-turn resolution. This is advisory only:
    engine/turn.py's actual resolution recomputes in-range fresh against the
    org's position at that time (which may have changed -- e.g. the org
    finished a move in the meantime), so it's the authority, not whatever
    was computed here at set-time.
    All fields are None if the org is currently in transit (no position to
    measure from yet).
    """
    org = cur.execute("""SELECT s.coord_x, s.coord_y, s.coord_z FROM organizations o
        JOIN sectors s ON s.id = o.sector_id WHERE o.id=? AND o.sector_id!=-1""",
        (org_id,)).fetchone()
    scan_range = get_scan_range(org_id)
    if not org:
        return {"current_x": None, "current_y": None, "current_z": None,
                "distance": None, "scan_range": scan_range, "in_range": None}
    distance = math.sqrt((target_x-org["coord_x"])**2 + (target_y-org["coord_y"])**2 +
                         (target_z-org["coord_z"])**2)
    return {"current_x": org["coord_x"], "current_y": org["coord_y"], "current_z": org["coord_z"],
            "distance": distance, "scan_range": scan_range, "in_range": distance <= scan_range}

def set_mission(player_token: str, org_id: int, mission: str, params: dict = None) -> dict:
    """
    Set an organization's mission. Validates ownership and mission type, and
    enforces the three org-lock states (see Data Model & Storage Design):
      - in transit (sector_id == -1): locked entirely, must cancel_move first
      - colony: locked against 'move' only — defend/attack/idle remain assignable
      - mid-colonization (ship, is_mobile == 0, not in transit): locked entirely,
        committed for the 3-turn transition window
    Setting mission='colonize' flips is_mobile to 0 immediately and schedules a
    colonize_complete event 3 turns out for engine/turn.py to resolve.
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
    record_event(
        event_type="mission.set",
        payload={"org_id": org_id, "mission": mission, "params": params or {}},
        actor_id=player["id"], subject_id=org_id, subject_type="organization")
    if mission == "colonize":
        resolve_turn = get_current_turn() + 3
        record_event(
            event_type="colonize_complete",
            payload={"org_id": org_id, "sector_id": org["sector_id"]},
            actor_id=player["id"], subject_id=org_id, subject_type="organization",
            resolve_at_turn=resolve_turn)
        cur.execute("UPDATE organizations SET mission=?, mission_params=?, is_mobile=0 WHERE id=?",
                    (mission, json.dumps(params) if params else None, org_id))
    else:
        cur.execute("UPDATE organizations SET mission=?, mission_params=? WHERE id=?",
                    (mission, json.dumps(params) if params else None, org_id))
    conn.commit(); conn.close()
    return {"ok": True, "org_id": org_id, "mission": mission}

def set_pod_task(player_token: str, pod_id: int, task: str,
                   target_x: int = None, target_y: int = None, target_z: int = None) -> dict:
    """
    Set a pod's task -- what its crew does, as distinct from the parent
    organization's `mission`, which is what the vehicle does. Validates
    ownership and task type.
    If mission='scan' and target_x/target_y/target_z are all provided, sets the
    scan target (a coordinate -- sectors are lazily instantiated, see
    db/sectors.py, so the target need not exist yet) in the same call. The
    target is accepted even if out of scan range -- see _scan_target_status
    -- but the response echoes the org's current coordinates plus the
    distance/scan_range/in_range so the player finds out immediately whether
    it's legal, and can fix it before end of turn rather than finding out
    only at resolution.
    If task='scan' and no target is given, the pod enters the scan task with
    no target — set_pod_scan_target must be called separately before end of turn.
    """
    if task not in VALID_POD_TASKS:
        return {"error": f"Invalid pod task '{task}'. Valid: {sorted(VALID_POD_TASKS)}"}
    target_coords = (target_x, target_y, target_z)
    if task == "scan" and any(c is not None for c in target_coords) and \
            any(c is None for c in target_coords):
        return {"error": "target_x, target_y, and target_z must all be provided together"}
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
    params = {}
    status = {"current_x": None, "current_y": None, "current_z": None,
              "distance": None, "scan_range": None, "in_range": None}
    if task == "scan" and target_x is not None:
        params["target_x"], params["target_y"], params["target_z"] = target_x, target_y, target_z
        status = _scan_target_status(cur, pod["org_id"], target_x, target_y, target_z)
        params["in_range"] = status["in_range"]
    record_event(
        event_type="pod.task_set",
        payload={"pod_id": pod_id, "task": task, "params": params},
        actor_id=player["id"], subject_id=pod_id, subject_type="pod")
    cur.execute("UPDATE pods SET task=?, task_params=? WHERE id=?",
                (task, json.dumps(params) if params else None, pod_id))
    conn.commit(); conn.close()
    return {"ok": True, "pod_id": pod_id, "task": task,
            "target_x": target_x, "target_y": target_y, "target_z": target_z, **status}

def set_pod_scan_target(player_token: str, pod_id: int,
                        target_x: int, target_y: int, target_z: int) -> dict:
    """
    Assign or change the scan target coordinate for a pod already in 'scan'
    mission. The target need not already exist as a sector (sectors are
    lazily instantiated, see db/sectors.py) -- re-targeting an already-known
    sector is fine too (e.g. to refresh a stale/decayed view of it, or check
    whether it's changed). The target is accepted even if out of scan range
    -- an out-of-range target still costs the scan's food upkeep at
    resolution but won't reveal anything (see engine/turn.py and the
    alert.scan_out_of_range event) -- but the response echoes the org's
    current coordinates plus the distance/scan_range/in_range so the player
    finds out immediately whether it's legal, and can fix it before end of
    turn rather than finding out only at resolution (no harm, no foul, if
    corrected in time).
    """
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
    status = _scan_target_status(cur, pod["org_id"], target_x, target_y, target_z)
    params = {"target_x": target_x, "target_y": target_y, "target_z": target_z,
              "in_range": status["in_range"]}
    record_event(
        event_type="pod.scan_target_set",
        payload={"pod_id": pod_id, "target_x": target_x, "target_y": target_y, "target_z": target_z,
                 "in_range": status["in_range"]},
        actor_id=player["id"], subject_id=pod_id, subject_type="pod")
    cur.execute("UPDATE pods SET task_params=? WHERE id=?",
                (json.dumps(params), pod_id))
    conn.commit(); conn.close()
    return {"ok": True, "pod_id": pod_id,
            "target_x": target_x, "target_y": target_y, "target_z": target_z, **status}

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
    result["display"] = {
        "header": f"{org['name']} — {status}, {org['mission']}",
        "resource_abbrev": RESOURCE_ABBREV,
        "rows_key": "tasks",
        "columns": ["task_display", "count", "energy", "food", "goods", "capacity_display"],
    }
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
      arrival turn, and turns_remaining as raw fields -- arrival_turn resolves
      when end_of_turn() is called *for* that turn number, so a ship whose
      arrival_turn equals the current turn hasn't landed yet (turns_remaining
      == 0 means "lands when this turn ends," not "already landed"). The
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
    import os
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
        production = _org_production(tasking, in_transit)
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
                # arrival_turn resolves when end_of_turn() is called *for*
                # that turn number -- so if arrival_turn == current_turn, the
                # ship hasn't landed yet even though the player is already on
                # that turn; it lands when this turn is ended, not before.
                # turns_remaining makes that unambiguous instead of implying
                # it already should have happened.
                # turns_remaining/arrival_turn/dest_sector stay as raw fields
                # (still the authority on when a ship whose arrival_turn ==
                # current_turn actually lands -- see the note above) -- just
                # no longer folded into the display string. A player asked
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
        },
    }

def show_game_status(player_token: str) -> dict:
    """
    Return the public scoreboard -- turn context (including next_tick_at,
    see show_civilization_status's docstring for the caveat about a paused
    clock) plus every player's aggregate resource totals, side by side.
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
    import os
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
    return {
        "turn": current_turn,
        "turn_limit": turn_limit,
        "next_tick_at": next_tick_at,
        "standings": standings,
        "display": {
            "resource_abbrev": RESOURCE_ABBREV,
            "rows_key": "standings",
            "columns": ["rank", "display_name", "score", "energy", "food", "goods", "utilization"],
        },
    }
