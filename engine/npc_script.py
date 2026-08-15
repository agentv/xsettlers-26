"""
Scripted NPC strategies: fleet behavior expressed as data rather than as a
Python function.

A *program* is a list of steps. Each step picks some of the player's ships and
gives them one order, either immediately or queued on the ship's log for a
later trigger. That is deliberately the whole vocabulary -- it covers openings
(a fixed sequence of orders whose timing is computable in advance), which is
what `turtle`, `fan_out_consolidate` and `burst_and_colonize` are. It does
not cover strategies that read the world and decide (`fan_out`'s
wait-for-every-scout-then-converge, `frontier_map_stay_frosty`'s endless
land-and-redirect); those stay as Python functions in engine/npc.py and sit
beside these in the same STRATEGIES registry. Adding conditions and repetition
here would be building a rule engine inside a command queue.

    program:
      - ships: {slice: [0, 8]}      # 'all', or slice/stride/offset over the
        when: now                   #   player's ships ordered by id
        action: move
        repeat_each: 2              # each params entry covers 2 consecutive ships
        params:
          - {d_x: 0, d_y: 3, d_z: 0}
          - {d_x: 0, d_y: -3, d_z: 0}

`params` as a list is cycled round-robin across the selected ships. That one
idea replaces the bespoke direction bookkeeping every fan-out strategy grew
its own copy of (`directions[i % 4]`, four-entry group dicts) without
introducing a compass vocabulary this layer would then have to own.

Programs are validated when they are *assigned* (see validate_program, called
from db/npc_profiles.assign_npc_profile), not when they run. Same reasoning as
queue_command's own up-front param validation, one level up: a program is
authored by a person -- eventually in a builder UI -- and an error has to
reach them while they are still looking at it, not surface three turns later
inside a background clock tick with no one to tell.
"""
from db.connection import get_connection
from xsettlers_mcp.tools.navigation_tools import confirm_move
from xsettlers_mcp.tools.organization_tools import (
    queue_command, set_mission, set_pod_task, set_org_scan_bearing,
    VALID_POD_TASKS)

ACTIONS = {"move", "colonize", "set_pod_task", "aim_scan"}
QUEUED_PHASES = {"during_transit", "before_arrival", "after_arrival", "at_turn"}
ARRIVAL_RELATIVE = {"before_arrival", "after_arrival"}
STEP_KEYS = {"ships", "when", "action", "params", "repeat_each"}


def _select(selector, ship_ids: list) -> list:
    """
    The ships a step applies to. `ORDER BY id` on the caller's query is
    load-bearing and so is this indexing: bootstrap numbers every player's
    ships before any colony (db/bootstrap.py), so index 0 is reliably the
    first ship of the starting fleet.
    """
    if selector in (None, "all"):
        chosen = list(range(len(ship_ids)))
    else:
        start, end = selector.get("slice", [0, len(ship_ids)])
        chosen = list(range(max(0, start), min(end, len(ship_ids))))
    stride = selector.get("stride", 1) if isinstance(selector, dict) else 1
    offset = selector.get("offset", 0) if isinstance(selector, dict) else 0
    return [ship_ids[i] for i in chosen[offset::stride]]


def _params_for(step: dict, position: int) -> dict:
    """This ship's params: the step's single dict, or its list cycled
    round-robin, with repeat_each consecutive ships sharing an entry."""
    params = step.get("params") or {}
    if isinstance(params, dict):
        return params
    repeat_each = step.get("repeat_each", 1)
    return params[(position // repeat_each) % len(params)]


def _resolve_now_destination(cur, org_id: int, params: dict):
    """Absolute destination for an immediate move, from either params form.
    The relative form resolves against where the ship is standing right now --
    the same rule engine.ship_log.resolve_destination applies at fire time for
    a queued move, just evaluated at once because 'now' is the trigger."""
    if params.get("dest_x") is not None:
        return params["dest_x"], params["dest_y"], params["dest_z"]
    org = cur.execute("""SELECT s.coord_x, s.coord_y, s.coord_z
        FROM organizations o JOIN sectors s ON s.id=o.sector_id
        WHERE o.id=? AND o.sector_id!=-1""", (org_id,)).fetchone()
    if not org:
        return None
    return (org["coord_x"] + params["d_x"], org["coord_y"] + params["d_y"],
            org["coord_z"] + params["d_z"])


def _pod_id_for(cur, org_id: int, params: dict):
    """A program cannot name pod ids -- they differ per ship and per game -- so
    set_pod_task steps address a pod by `pod_index` within its own org,
    resolved here. An explicit pod_id is still honoured for a hand-written
    profile that happens to know one."""
    if params.get("pod_id") is not None:
        return params["pod_id"]
    pods = cur.execute("SELECT id FROM pods WHERE org_id=? ORDER BY id", (org_id,)).fetchall()
    index = params.get("pod_index", 0)
    return pods[index]["id"] if index < len(pods) else None


def validate_program(program) -> dict | None:
    """
    Check a program's shape before it is ever stored. Returns an {"error": ...}
    dict, or None when the program is sound.

    What is checkable here is structure, not outcome: fleet size, ship
    positions and energy stocks all belong to a game that may not have started
    yet. So this rejects programs that could never work for anyone -- unknown
    actions, malformed selectors, a params list that is empty -- and leaves
    per-ship questions to queue_command, which already validates them properly
    at queue time.
    """
    if program is None:
        return None
    if not isinstance(program, list):
        return {"error": "program must be a list of steps"}
    seen_now_move = False
    for i, step in enumerate(program):
        where = f"program step {i}"
        if not isinstance(step, dict):
            return {"error": f"{where}: each step must be a mapping"}
        unknown = set(step) - STEP_KEYS
        if unknown:
            return {"error": f"{where}: unknown keys {sorted(unknown)}. "
                             f"Valid: {sorted(STEP_KEYS)}"}
        action = step.get("action")
        if action not in ACTIONS:
            return {"error": f"{where}: invalid action '{action}'. Valid: {sorted(ACTIONS)}"}

        when = step.get("when", "now")
        if isinstance(when, dict):
            if set(when) != {"at_turn"} or not isinstance(when["at_turn"], int):
                return {"error": f"{where}: the only mapping form of 'when' is "
                                 f"{{at_turn: <turn number>}}"}
            phase = "at_turn"
        elif when == "now":
            phase = "now"
        elif when in QUEUED_PHASES:
            if when == "at_turn":
                return {"error": f"{where}: 'at_turn' needs a turn number -- "
                                 f"write it as {{at_turn: N}}"}
            phase = when
        else:
            return {"error": f"{where}: invalid 'when' value '{when}'. Valid: 'now', "
                             f"'during_transit', 'before_arrival', 'after_arrival', "
                             f"{{at_turn: N}}"}
        if phase == "during_transit" and action != "set_pod_task":
            return {"error": f"{where}: 'during_transit' only supports set_pod_task -- "
                             f"pod tasking is the one thing a departing org does not lock"}
        # An arrival-relative order can only be queued against a move that is
        # already under way (see queue_command), so a program that opens with
        # one has nothing to anchor to and would be refused ship by ship at
        # run time. Caught here instead, while the author can still reorder.
        if phase in ARRIVAL_RELATIVE and not seen_now_move:
            return {"error": f"{where}: '{when}' fires relative to a move in progress, "
                             f"so an earlier step must have sent these ships moving "
                             f"(when: now, action: move)"}
        if phase == "now" and action == "move":
            seen_now_move = True

        err = _validate_selector(step.get("ships"), where)
        if err:
            return err
        err = _validate_params(step, action, where)
        if err:
            return err
    return None


def _validate_selector(selector, where: str) -> dict | None:
    if selector in (None, "all"):
        return None
    if not isinstance(selector, dict):
        return {"error": f"{where}: 'ships' must be 'all' or a mapping with "
                         f"slice/stride/offset"}
    unknown = set(selector) - {"slice", "stride", "offset"}
    if unknown:
        return {"error": f"{where}: unknown ships keys {sorted(unknown)}"}
    if "slice" in selector:
        s = selector["slice"]
        if (not isinstance(s, list) or len(s) != 2
                or not all(isinstance(v, int) for v in s)):
            return {"error": f"{where}: 'slice' must be [start, end]"}
        if s[0] < 0 or s[1] < s[0]:
            return {"error": f"{where}: 'slice' {s} is empty or negative"}
    if "stride" in selector and (not isinstance(selector["stride"], int)
                                 or selector["stride"] < 1):
        return {"error": f"{where}: 'stride' must be a positive integer"}
    if "offset" in selector and (not isinstance(selector["offset"], int)
                                 or selector["offset"] < 0):
        return {"error": f"{where}: 'offset' must be a non-negative integer"}
    return None


def _validate_params(step: dict, action: str, where: str) -> dict | None:
    params = step.get("params")
    if isinstance(params, list):
        if not params:
            return {"error": f"{where}: 'params' list is empty -- there is nothing to "
                             f"cycle across the selected ships"}
        entries = params
    elif isinstance(params, dict) or params is None:
        entries = [params or {}]
    else:
        return {"error": f"{where}: 'params' must be a mapping or a list of mappings"}

    repeat_each = step.get("repeat_each", 1)
    if not isinstance(repeat_each, int) or repeat_each < 1:
        return {"error": f"{where}: 'repeat_each' must be a positive integer"}

    for entry in entries:
        if not isinstance(entry, dict):
            return {"error": f"{where}: every 'params' entry must be a mapping"}
        if action == "move":
            absolute = [k for k in ("dest_x", "dest_y", "dest_z") if entry.get(k) is not None]
            relative = [k for k in ("d_x", "d_y", "d_z") if entry.get(k) is not None]
            if absolute and relative:
                return {"error": f"{where}: a move takes dest_x/dest_y/dest_z or "
                                 f"d_x/d_y/d_z, not both"}
            if not absolute and not relative:
                return {"error": f"{where}: a move needs a destination -- "
                                 f"dest_x/dest_y/dest_z or d_x/d_y/d_z"}
            if (absolute and len(absolute) < 3) or (relative and len(relative) < 3):
                return {"error": f"{where}: a move needs all three coordinates"}
        elif action == "set_pod_task":
            task = entry.get("task")
            if task not in VALID_POD_TASKS:
                return {"error": f"{where}: invalid pod task '{task}'. "
                                 f"Valid: {sorted(VALID_POD_TASKS)}"}
    return None


def run_program(player_id: int, player_token: str, program, memory: dict) -> dict:
    """
    Execute a program once. Single-phase by construction: a program is a fixed
    opening, so once its steps are dispatched there is nothing further to
    decide and later turns are a no-op.

    Runs from run_npc_decisions(), which completes before end_of_turn() opens
    its own transaction -- so steps call the ordinary @player_tool wrappers,
    the same ones a human player's MCP call goes through, each committing its
    own connection. Nothing here mutates organizations or pods directly.
    """
    if memory.get("dispatched"):
        return memory
    memory["dispatched"] = True
    if not program:
        return memory

    conn = get_connection(); cur = conn.cursor()
    ship_ids = [r["id"] for r in cur.execute(
        "SELECT id FROM organizations WHERE player_id=? AND org_type='ship' ORDER BY id",
        (player_id,)).fetchall()]
    conn.close()
    if not ship_ids:
        return memory

    errors = []
    for step_index, step in enumerate(program):
        selected = _select(step.get("ships"), ship_ids)
        for position, org_id in enumerate(selected):
            params = dict(_params_for(step, position))
            result = _dispatch_step(player_token, org_id, step, params)
            if isinstance(result, dict) and "error" in result:
                errors.append({"step": step_index, "org_id": org_id,
                               "error": result["error"]})
    if errors:
        # Kept rather than raised: one ship failing its order is not a reason to
        # abandon the rest of the fleet's opening, and the profile row is the
        # only place a record of it would survive the tick.
        memory["errors"] = errors
    return memory


def _dispatch_step(player_token: str, org_id: int, step: dict, params: dict):
    when = step.get("when", "now")
    action = step["action"]
    if when == "now":
        return _dispatch_now(player_token, org_id, action, params)

    if isinstance(when, dict):
        phase, turn = "at_turn", when["at_turn"]
    else:
        phase, turn = when, None
    if action == "set_pod_task":
        conn = get_connection(); cur = conn.cursor()
        params["pod_id"] = _pod_id_for(cur, org_id, params)
        conn.close()
        params.pop("pod_index", None)
        if params["pod_id"] is None:
            return {"error": f"org {org_id} has no pod at the requested index"}
    return queue_command(player_token, org_id, phase, action, params, turn=turn)


def _dispatch_now(player_token: str, org_id: int, action: str, params: dict):
    if action == "move":
        conn = get_connection(); cur = conn.cursor()
        dest = _resolve_now_destination(cur, org_id, params)
        conn.close()
        if dest is None:
            return {"error": f"org {org_id} is in transit -- no position to move from"}
        if any(c < 0 for c in dest):
            return {"error": f"move lands at {dest} -- space has no negative indices"}
        return confirm_move(player_token, org_id, dest[0], dest[1], dest[2],
                            jump_range_per_turn=params.get("jump_range_per_turn", 1))
    if action == "colonize":
        return set_mission(player_token, org_id, "colonize")
    if action == "aim_scan":
        return set_org_scan_bearing(player_token, org_id, bearing=params.get("bearing"),
                                    offset_x=params.get("offset_x"),
                                    offset_y=params.get("offset_y"),
                                    offset_z=params.get("offset_z"))
    # action == 'set_pod_task'
    conn = get_connection(); cur = conn.cursor()
    pod_id = _pod_id_for(cur, org_id, params)
    conn.close()
    if pod_id is None:
        return {"error": f"org {org_id} has no pod at the requested index"}
    return set_pod_task(player_token, pod_id, params["task"],
                        bearing=params.get("bearing"),
                        offset_x=params.get("offset_x"),
                        offset_y=params.get("offset_y"),
                        offset_z=params.get("offset_z"))
