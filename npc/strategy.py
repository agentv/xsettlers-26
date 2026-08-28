"""
NPC strategies as inert data: a document of steps, and the engine that walks
it. Documents live in config/npc_strategies/*.yaml (see npc/library.py).

A document is a list of steps, executed in order, with a program counter kept
per player. Two kinds of step:

    steps:
      - order:  {ships: all, action: move, params: {d_x: 0, d_y: -2, d_z: 0}}
      - decide: {await: all_scans_resolved, from: scan_targets,
                 rank_by: energy_capacity, pick: max, bind: target}
      - order:  {ships: all, action: move, params: {dest: $target}}

`order` gives some of the player's ships one order -- the same four actions a
human can give (engine/actions.py), dispatched the same two ways. `decide`
is the hook that lets a strategy react to information that only exists
mid-game: it evaluates a gate, ranks a candidate set, and binds the winner to
a name that later steps substitute with `$name`.

**A step that cannot proceed is not an error.** A `decide` whose gate has not
opened leaves the program counter where it is and is retried next turn. That
is how waiting is expressed -- there is no wait verb.

`loop: true` on the document restarts the counter after the last step, for
strategies with no terminal state. A looping document still executes at most
one pass per turn.
"""
import json

from db.connection import connection, read_all
from engine.actions import ACTION_NAMES, UPON_DEPARTURE_ACTIONS, TRIGGER_PHASES
from engine.movement import move_params_error, resolve_move_destination
from npc import decide
from xsettlers_mcp.tools.navigation_tools import confirm_move
from xsettlers_mcp.tools.organization_tools import (
    queue_command, set_mission, set_pod_task, set_org_scan_bearing,
    transfer_resources, VALID_POD_TASKS, VALID_TRANSFER_RESOURCES, _aim_args)

DOCUMENT_KEYS = {"name", "description", "config", "loop", "steps"}
ORDER_KEYS = {"ships", "when", "action", "params", "repeat_each"}
DECIDE_KEYS = {"await", "from", "rank_by", "pick", "bind"}
STEP_KINDS = {"order", "decide"}
ARRIVAL_RELATIVE = {"upon_arrival"}
NO_POD_AT_INDEX = "org {org_id} has no pod at the requested index"
NO_ORG_AT_INDEX = "org {org_id}'s player has no organization at to_index {index}"


# ---------------------------------------------------------------- selection

def _fleet(player_id: int) -> list:
    """
    This player's ships, in id order, each carrying the index that order gives
    it. `ORDER BY id` is load-bearing: bootstrap numbers every player's ships
    before any colony (db/bootstrap.py), so index 0 is reliably the first ship
    of the starting fleet, and a document's params cycle is stable across
    games.
    """
    rows = read_all("""SELECT id, sector_id, mission FROM organizations
                       WHERE player_id=? AND org_type='ship' ORDER BY id""",
                    (player_id,))
    return [{"id": r["id"], "index": i, "sector_id": r["sector_id"], "mission": r["mission"]}
            for i, r in enumerate(rows)]


def select(selector, fleet: list) -> list:
    """
    The ships an order applies to: 'all', 'idle', or a slice/stride/offset
    over the fleet.

    'idle' means landed and unoccupied -- not in transit, not mid-colonization
    -- which is what a strategy with no terminal state re-checks every turn.
    It is a live filter, so the set changes turn to turn; that is why params
    cycle on a ship's fleet index rather than its position in the selection
    (see params_for), or a ship would be handed a different direction every
    time its neighbours happened to be busy.
    """
    if selector == "idle":
        return [s for s in fleet if s["sector_id"] != -1 and s["mission"] == "idle"]
    if selector in (None, "all"):
        chosen = list(range(len(fleet)))
    else:
        start, end = selector.get("slice", [0, len(fleet)])
        chosen = list(range(max(0, start), min(end, len(fleet))))
    stride = selector.get("stride", 1) if isinstance(selector, dict) else 1
    offset = selector.get("offset", 0) if isinstance(selector, dict) else 0
    return [fleet[i] for i in chosen[offset::stride]]


def params_for(order: dict, fleet_index: int) -> dict:
    """
    This ship's params: the order's single mapping, or its list cycled
    round-robin keyed on the ship's own fleet index, with repeat_each
    consecutive ships sharing an entry."""
    params = order.get("params") or {}
    if isinstance(params, dict):
        return params
    repeat_each = order.get("repeat_each", 1)
    return params[(fleet_index // repeat_each) % len(params)]


# ------------------------------------------------------------ substitution

def _resolve_value(value, bindings: dict, config: dict):
    """A `$name` reference resolved against bindings first, then the
    document's own config. Anything else is returned untouched -- there are no
    expressions, only whole-value substitution."""
    if not (isinstance(value, str) and value.startswith("$")):
        return value
    name = value[1:]
    if name in bindings:
        return bindings[name]
    if name in config:
        return config[name]
    raise KeyError(f"'${name}' is not bound and not in the document's config")


def resolve_params(params: dict, bindings: dict, config: dict) -> dict:
    """
    Substitute `$name` references, then expand `dest` into the three
    coordinates a move actually takes.

    `dest: $target` is the one place a binding is more than a scalar: a
    decide step binds a whole sector, and this is what turns it into the
    dest_x/dest_y/dest_z a move needs, so a document never has to name the
    three fields separately.
    """
    resolved = {k: _resolve_value(v, bindings, config) for k, v in params.items()}
    dest = resolved.pop("dest", None)
    if dest is not None:
        resolved["dest_x"], resolved["dest_y"], resolved["dest_z"] = \
            dest["x"], dest["y"], dest["z"]
    return resolved


# --------------------------------------------------------------- execution

def run_strategy(player_id: int, player_token: str, document: dict, memory: dict,
                 config_override: dict = None) -> dict:
    """
    Advance one player's strategy by one turn.

    Executes at most one pass over the document per turn, so a looping
    document advances rather than spinning.

    The fleet is read ONCE, before the pass, and every step selects against
    that snapshot. That is what lets one pass order the same ships twice --
    "move these, then aim these" -- since the move has already taken them out
    of the idle set by the time the second step runs.

    `config_override` is the profile's own config (npc_profiles.config) laid
    over the document's, so a strategy can be tuned without copying the file.
    """
    steps = (document or {}).get("steps") or []
    if not steps:
        return memory
    fleet = _fleet(player_id)
    if not fleet:
        return memory
    config = {**(document.get("config") or {}), **(config_override or {})}
    bindings = memory.setdefault("bindings", {})
    pc = memory.get("pc", 0)

    # At most one pass per turn: a looping document advances rather than
    # spinning through its steps repeatedly within a single tick.
    for _ in range(len(steps)):
        if pc >= len(steps):
            break
        step = steps[pc]
        if "decide" in step:
            value, reason = decide.evaluate(player_id, step["decide"])
            if value is None:
                # Not an error -- the gate simply has not opened yet. Leave pc
                # where it is and try again next turn.
                memory["waiting"] = reason
                break
            bindings[step["decide"]["bind"]] = value
            memory.pop("waiting", None)
        else:
            _run_order(player_token, step["order"], fleet, bindings, config, memory)
        pc += 1

    # End of the document: a looping strategy rewinds for next turn, a
    # terminal one stays parked past the last step and does nothing further.
    if pc >= len(steps) and document.get("loop"):
        pc = 0

    memory["pc"] = pc
    return memory


def _run_order(player_token: str, order: dict, fleet: list,
               bindings: dict, config: dict, memory: dict):
    for ship in select(order.get("ships"), fleet):
        try:
            params = resolve_params(dict(params_for(order, ship["index"])), bindings, config)
        except KeyError as exc:
            _record(memory, ship["id"], str(exc))
            continue
        result = _dispatch(player_token, ship["id"], order, params)
        if isinstance(result, dict) and "error" in result:
            _record(memory, ship["id"], result["error"])


def _record(memory: dict, org_id: int, error: str):
    """One ship failing its order is not a reason to abandon the rest of the
    fleet, and the profile row is the only place a record of it survives the
    tick."""
    memory.setdefault("errors", []).append({"org_id": org_id, "error": error})


def _dispatch(player_token: str, org_id: int, order: dict, params: dict):
    when = order.get("when", "now")
    action = order["action"]
    if when == "now":
        return IMMEDIATE[action](player_token, org_id, params)

    if isinstance(when, dict):
        phase, turn = "at_turn", when["at_turn"]
    else:
        phase, turn = when, None
    if action == "set_pod_task":
        with connection() as conn:
            params["pod_id"] = _pod_id_for(conn, org_id, params)
        params.pop("pod_index", None)
        if params["pod_id"] is None:
            return {"error": NO_POD_AT_INDEX.format(org_id=org_id)}
    if action == "transfer":
        with connection() as conn:
            params["to_org_id"] = _org_id_at_index(conn, org_id, params.get("to_index"))
        index = params.pop("to_index", None)
        if params["to_org_id"] is None:
            return {"error": NO_ORG_AT_INDEX.format(org_id=org_id, index=index)}
    return queue_command(player_token, org_id, phase, action, params, turn=turn)


def _pod_id_for(cur, org_id: int, params: dict):
    """A document cannot name pod ids -- they differ per ship and per game --
    so set_pod_task addresses a pod by `pod_index` within its own org,
    resolved here. An explicit pod_id is still honoured."""
    if params.get("pod_id") is not None:
        return params["pod_id"]
    pods = cur.execute("SELECT id FROM pods WHERE org_id=? ORDER BY id", (org_id,)).fetchall()
    index = params.get("pod_index", 0)
    return pods[index]["id"] if index < len(pods) else None


def _org_id_at_index(cur, giver_org_id: int, index):
    """
    A transfer's receiver, addressed the same way set_pod_task addresses a pod
    -- by index, since a document cannot name org ids. The index runs over the
    giver's player's own organizations in id order: bootstrap numbers every
    ship before any colony (db/bootstrap.py), so 0 is the first ship of the
    starting fleet and a single colony hub is the last index. None or an
    out-of-range index returns None, which the caller turns into an error.
    """
    if not isinstance(index, int) or index < 0:
        return None
    rows = cur.execute(
        """SELECT id FROM organizations
           WHERE player_id=(SELECT player_id FROM organizations WHERE id=?)
           ORDER BY id""", (giver_org_id,)).fetchall()
    return rows[index]["id"] if index < len(rows) else None


def _now_move(player_token: str, org_id: int, params: dict):
    with connection() as conn:
        dest, err = resolve_move_destination(conn, org_id, params)
    if err:
        return {"error": err}
    return confirm_move(player_token, org_id, dest[0], dest[1], dest[2])


def _now_colonize(player_token: str, org_id: int, params: dict):
    return set_mission(player_token, org_id, "colonize")


def _now_aim_scan(player_token: str, org_id: int, params: dict):
    return set_org_scan_bearing(player_token, org_id, **_aim_args(params))


def _now_set_pod_task(player_token: str, org_id: int, params: dict):
    with connection() as conn:
        pod_id = _pod_id_for(conn, org_id, params)
    if pod_id is None:
        return {"error": NO_POD_AT_INDEX.format(org_id=org_id)}
    return set_pod_task(player_token, pod_id, params["task"], **_aim_args(params))


def _now_transfer(player_token: str, org_id: int, params: dict):
    with connection() as conn:
        to_org_id = _org_id_at_index(conn, org_id, params.get("to_index"))
    if to_org_id is None:
        return {"error": NO_ORG_AT_INDEX.format(org_id=org_id, index=params.get("to_index"))}
    return transfer_resources(player_token, org_id, to_org_id,
                              params.get("resource"), params.get("amount"))


# The immediate binding of engine/actions.py's vocabulary: these go through
# the ordinary @player_tool wrappers, since run_npc_decisions() completes
# before end_of_turn() opens its transaction. engine/ship_log.py holds the
# queued binding of the same names.
IMMEDIATE = {"move": _now_move, "colonize": _now_colonize,
             "aim_scan": _now_aim_scan, "set_pod_task": _now_set_pod_task,
             "transfer": _now_transfer}


# -------------------------------------------------------------- validation

def validate_strategy(document) -> dict | None:
    """
    Check a document's shape before it is ever stored. Returns an
    {"error": ...} dict, or None when the document is sound.

    What is checkable here is structure and vocabulary, not outcome: fleet
    size, ship positions and energy stocks belong to a game that may not have
    started. So this rejects documents that could never work for anyone --
    unknown actions, gates or rank fields, malformed selectors, a `$name` that
    nothing binds -- and leaves per-ship questions to queue_command, which
    validates them properly at queue time.
    """
    if document is None:
        return None
    if not isinstance(document, dict):
        return {"error": "a strategy must be a mapping with a 'steps' list"}
    unknown = set(document) - DOCUMENT_KEYS
    if unknown:
        return {"error": f"unknown document keys {sorted(unknown)}. "
                         f"Valid: {sorted(DOCUMENT_KEYS)}"}
    steps = document.get("steps")
    if steps is None:
        steps = []
    if not isinstance(steps, list):
        return {"error": "'steps' must be a list"}

    config = document.get("config") or {}
    if not isinstance(config, dict):
        return {"error": "'config' must be a mapping"}
    bound = set(config)
    seen_now_move = False

    for i, step in enumerate(steps):
        where = f"step {i}"
        if not isinstance(step, dict):
            return {"error": f"{where}: each step must be a mapping"}
        kinds = set(step) & STEP_KINDS
        if len(kinds) != 1 or set(step) - STEP_KINDS:
            return {"error": f"{where}: each step must carry exactly one of "
                             f"{sorted(STEP_KINDS)}"}
        kind = kinds.pop()
        if kind == "decide":
            err = _validate_decide(step["decide"], where)
            if err:
                return err
            bound.add(step["decide"]["bind"])
            continue
        err = _validate_order(step["order"], where, bound, seen_now_move)
        if err:
            return err
        if step["order"].get("when", "now") == "now" and step["order"].get("action") == "move":
            seen_now_move = True
    return None


def _validate_decide(step, where: str) -> dict | None:
    if not isinstance(step, dict):
        return {"error": f"{where}: 'decide' must be a mapping"}
    unknown = set(step) - DECIDE_KEYS
    if unknown:
        return {"error": f"{where}: unknown decide keys {sorted(unknown)}. "
                         f"Valid: {sorted(DECIDE_KEYS)}"}
    for required in ("from", "rank_by", "pick", "bind"):
        if required not in step:
            return {"error": f"{where}: 'decide' requires '{required}'"}
    if step.get("await") is not None and step["await"] not in decide.GATES:
        return {"error": f"{where}: unknown gate '{step['await']}'. "
                         f"Valid: {sorted(decide.GATES)}"}
    if step["from"] not in decide.SOURCES:
        return {"error": f"{where}: unknown source '{step['from']}'. "
                         f"Valid: {sorted(decide.SOURCES)}"}
    if step["rank_by"] not in decide.RANK_FIELDS:
        return {"error": f"{where}: unknown rank_by '{step['rank_by']}'. "
                         f"Valid: {sorted(decide.RANK_FIELDS)}"}
    if step["pick"] not in decide.PICKS:
        return {"error": f"{where}: unknown pick '{step['pick']}'. "
                         f"Valid: {sorted(decide.PICKS)}"}
    if not isinstance(step["bind"], str) or not step["bind"]:
        return {"error": f"{where}: 'bind' must be a non-empty name"}
    return None


def _validate_order(order, where: str, bound: set, seen_now_move: bool) -> dict | None:
    if not isinstance(order, dict):
        return {"error": f"{where}: 'order' must be a mapping"}
    unknown = set(order) - ORDER_KEYS
    if unknown:
        return {"error": f"{where}: unknown order keys {sorted(unknown)}. "
                         f"Valid: {sorted(ORDER_KEYS)}"}
    action = order.get("action")
    if action not in ACTION_NAMES:
        return {"error": f"{where}: invalid action '{action}'. Valid: {sorted(ACTION_NAMES)}"}

    when = order.get("when", "now")
    if isinstance(when, dict):
        if set(when) != {"at_turn"} or not isinstance(when["at_turn"], int):
            return {"error": f"{where}: the only mapping form of 'when' is "
                             f"{{at_turn: <turn number>}}"}
        phase = "at_turn"
    elif when == "now":
        phase = "now"
    elif when in TRIGGER_PHASES:
        if when == "at_turn":
            return {"error": f"{where}: 'at_turn' needs a turn number -- "
                             f"write it as {{at_turn: N}}"}
        phase = when
    else:
        return {"error": f"{where}: invalid 'when' value '{when}'. Valid: 'now', "
                         f"'upon_departure', 'upon_arrival', "
                         f"{{at_turn: N}}"}
    if phase == "upon_departure" and action not in UPON_DEPARTURE_ACTIONS:
        return {"error": f"{where}: 'upon_departure' only supports set_pod_task -- "
                         f"pod tasking is the one thing a departing org does not lock"}
    # An arrival-relative order can only be queued against a move already
    # under way (see queue_command), so a document that opens with one has
    # nothing to anchor to and would be refused ship by ship at run time.
    # Caught here instead, while the author can still reorder.
    if phase in ARRIVAL_RELATIVE and not seen_now_move:
        return {"error": f"{where}: '{when}' fires relative to a move in progress, "
                         f"so an earlier step must have sent these ships moving "
                         f"(when: now, action: move)"}

    err = _validate_selector(order.get("ships"), where)
    if err:
        return err
    return _validate_params(order, action, where, bound)


def _validate_selector(selector, where: str) -> dict | None:
    if selector in (None, "all", "idle"):
        return None
    if not isinstance(selector, dict):
        return {"error": f"{where}: 'ships' must be 'all', 'idle', or a mapping "
                         f"with slice/stride/offset"}
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


def _refs(entry: dict) -> set:
    return {v[1:] for v in entry.values() if isinstance(v, str) and v.startswith("$")}


def _validate_params(order: dict, action: str, where: str, bound: set) -> dict | None:
    params = order.get("params")
    if isinstance(params, list):
        if not params:
            return {"error": f"{where}: 'params' list is empty -- there is nothing to "
                             f"cycle across the selected ships"}
        entries = params
    elif isinstance(params, dict) or params is None:
        entries = [params or {}]
    else:
        return {"error": f"{where}: 'params' must be a mapping or a list of mappings"}

    repeat_each = order.get("repeat_each", 1)
    if not isinstance(repeat_each, int) or repeat_each < 1:
        return {"error": f"{where}: 'repeat_each' must be a positive integer"}

    for entry in entries:
        if not isinstance(entry, dict):
            return {"error": f"{where}: every 'params' entry must be a mapping"}
        unbound = _refs(entry) - bound
        if unbound:
            return {"error": f"{where}: {sorted('$' + n for n in unbound)} is not bound by "
                             f"an earlier decide step and is not in the document's config"}
        if action == "move":
            # A destination supplied by a binding is only a sector at run
            # time, so the coordinate check that applies to a literal move
            # cannot run here -- resolve_params expands it, and the ordinary
            # move validation catches a bad one at dispatch.
            if "dest" in entry:
                continue
            problem = move_params_error(entry)
            if problem:
                return {"error": f"{where}: {problem}"}
        elif action == "set_pod_task":
            task = entry.get("task")
            if task not in VALID_POD_TASKS:
                return {"error": f"{where}: invalid pod task '{task}'. "
                                 f"Valid: {sorted(VALID_POD_TASKS)}"}
        elif action == "transfer":
            err = _validate_transfer_params(entry, where)
            if err:
                return err
    return None


def _is_ref(value) -> bool:
    return isinstance(value, str) and value.startswith("$")


def _validate_transfer_params(entry: dict, where: str) -> dict | None:
    """
    A transfer names its receiver by `to_index` -- an index into the player's
    own organizations in id order, resolved per game (see _org_id_at_index) --
    plus a `resource` and an `amount`. `amount` may be a `$name` bound
    elsewhere; the others must be literals.
    """
    if not isinstance(entry.get("to_index"), int) or entry["to_index"] < 0:
        return {"error": f"{where}: 'transfer' needs 'to_index' -- a non-negative "
                         f"index into your organizations (0 is your first ship)"}
    if entry.get("resource") not in VALID_TRANSFER_RESOURCES:
        return {"error": f"{where}: 'transfer' needs 'resource' one of "
                         f"{sorted(VALID_TRANSFER_RESOURCES)}"}
    amount = entry.get("amount")
    if not _is_ref(amount) and (isinstance(amount, bool)
                                or not isinstance(amount, (int, float)) or amount <= 0):
        return {"error": f"{where}: 'transfer' needs a positive 'amount'"}
    return None
