"""
NPC strategy execution. Each is_npc=1 player with a row in npc_profiles gets
its registered strategy function invoked once per turn, early in
end_of_turn() (see run_npc_decisions, called from engine/turn.py before any
turn-resolution processing). A strategy decides and acts by calling the
exact same tool functions (confirm_move today; set_pod_task/set_mission
as more strategies need them) any human player calls through the MCP
interface -- no special-cased write path, no direct mutation of
organizations/pods from here. Each tool call commits its own connection
before the next NPC is processed, so nothing is left open when
end_of_turn()'s own turn-resolution transaction opens right after this
returns (see the comment at that call site).

Per-NPC working memory (what player2_policy.py, this strategy's prototype,
used to track in a stray .player2_plan.json file) lives in
npc_profiles.memory, a JSON blob mutated turn to turn -- same pattern
pods.task_params already uses for per-pod working state.
"""
import json
from db.connection import get_connection
from xsettlers_mcp.tools.navigation_tools import confirm_move
from xsettlers_mcp.tools.organization_tools import queue_command, set_mission, set_org_scan_bearing

def _fan_out_consolidate(player_id: int, player_token: str, config: dict, memory: dict) -> dict:
    """
    Ported from player2_policy.py: fan out in 4 directions (2 ships per
    direction, first 8 ships found), then one ship per pair (the "mover")
    jumps `leg_distance` further out in the same direction while the other
    (the "stayer") stays put. From there everyone just settles in and
    produces -- no further decisions after the second leg.
    Config: leg_distance (default 3), jump_range_per_turn (default 1).
    The second leg is driven entirely by the ship's log (see
    engine/ship_log.py): each mover's leg-2 move is queued at opening-dispatch
    time as an 'after_arrival' command, so it self-fires one end_of_turn()
    pass after that mover lands leg 1 -- one turn of harvesting at the
    intermediate stop, fixed by the after_arrival phase's own definition (see
    docs/TODO.md), not a configurable hold_turns. (Previously a bespoke
    memory["second_leg_turn"] poll -- this strategy was the TODO note's
    motivating example for generalizing that into org_command_queue.)
    Requires at least 8 of the player's organizations to be ships; if fewer
    are available the strategy marks itself done without acting (nothing
    sensible to fan out) rather than retrying every turn indefinitely.
    Single-phase now: once the opening moves and their queued second legs are
    dispatched, this strategy has nothing further to decide.
    """
    leg = config.get("leg_distance", 3)
    jump_range = config.get("jump_range_per_turn", 1)

    if memory.get("opening_dispatched"):
        return memory

    conn = get_connection(); cur = conn.cursor()
    ship_ids = [r["id"] for r in cur.execute(
        "SELECT id FROM organizations WHERE player_id=? AND org_type='ship' ORDER BY id",
        (player_id,)).fetchall()]
    if len(ship_ids) < 8:
        conn.close()
        memory["opening_dispatched"] = True
        return memory
    home = cur.execute("""SELECT s.coord_x,s.coord_y,s.coord_z FROM organizations o
        JOIN sectors s ON s.id=o.sector_id WHERE o.id=?""", (ship_ids[0],)).fetchone()
    conn.close()
    hx, hy, hz = home["coord_x"], home["coord_y"], home["coord_z"]

    groups = {
        "north": (ship_ids[0], ship_ids[1], (hx, hy+leg, hz), (0, leg, 0)),
        "south": (ship_ids[2], ship_ids[3], (hx, hy-leg, hz), (0, -leg, 0)),
        "east":  (ship_ids[4], ship_ids[5], (hx+leg, hy, hz), (leg, 0, 0)),
        "west":  (ship_ids[6], ship_ids[7], (hx-leg, hy, hz), (-leg, 0, 0)),
    }
    plan = {"stayer": {}, "mover": {}, "second_dest": {}}
    for name, (stayer, mover, dest, vec) in groups.items():
        confirm_move(player_token, stayer, *dest, jump_range_per_turn=jump_range)
        mover_result = confirm_move(player_token, mover, *dest, jump_range_per_turn=jump_range)
        second_dest = [dest[0]+vec[0], dest[1]+vec[1], dest[2]+vec[2]]
        if "error" not in mover_result:
            queue_command(player_token, mover, "after_arrival", "move",
                          {"dest_x": second_dest[0], "dest_y": second_dest[1], "dest_z": second_dest[2],
                           "jump_range_per_turn": jump_range})
        plan["stayer"][name] = stayer
        plan["mover"][name] = mover
        plan["second_dest"][name] = second_dest
    memory.update(plan)
    memory["opening_dispatched"] = True
    return memory

def _turtle(player_id: int, player_token: str, config: dict, memory: dict) -> dict:
    """
    Hold still. Take no action, ever. The baseline no-op strategy from
    docs/TODO.md's fleet-strategy taxonomy -- no config, no state, nothing
    to dispatch. Already has one real data point: this is exactly what
    "Player Two" did in the Diaspora mock-run comparison (a pure-idle
    opponent), scoring 2240 vs. a burst-and-colonize opponent's 2574.
    """
    return memory

def _burst_and_colonize(player_id: int, player_token: str, config: dict, memory: dict) -> dict:
    """
    Fast, simultaneous multi-direction departure, plus committing a fixed
    fraction of the fleet to colonizing immediately rather than staying
    mobile -- betting the colony production multiplier compounding over the
    rest of the game beats full flexibility. Single-shot, single-phase: one
    opening dispatch, nothing further to decide afterward (unlike
    fan_out_consolidate/_fan_out, there's no second leg or scan-driven
    follow-up here -- "burst" means everyone commits at once).
    Config: leg_distance (default 3), jump_range_per_turn (default 1),
    colonize_fraction (default 0.25 -- rounded, minimum 1 ship if there's
    more than one to work with).
    This is the exact pattern run as "Vincent" in the Diaspora mock-run
    comparison earlier (6 ships fanned out, 2 colonized turn 1) -- the
    winning side, 2574 vs. a turtle opponent's 2240.
    """
    if memory.get("dispatched"):
        return memory
    leg = config.get("leg_distance", 3)
    jump_range = config.get("jump_range_per_turn", 1)
    colonize_fraction = config.get("colonize_fraction", 0.25)

    conn = get_connection(); cur = conn.cursor()
    ship_ids = [r["id"] for r in cur.execute(
        "SELECT id FROM organizations WHERE player_id=? AND org_type='ship' ORDER BY id",
        (player_id,)).fetchall()]
    if not ship_ids:
        conn.close()
        memory["dispatched"] = True
        return memory
    home = cur.execute("""SELECT s.coord_x,s.coord_y,s.coord_z FROM organizations o
        JOIN sectors s ON s.id=o.sector_id WHERE o.id=?""", (ship_ids[0],)).fetchone()
    conn.close()
    hx, hy, hz = home["coord_x"], home["coord_y"], home["coord_z"]

    num_colonize = max(1, round(len(ship_ids) * colonize_fraction)) if len(ship_ids) > 1 else 0
    colonize_ids = ship_ids[:num_colonize]
    fan_ids = ship_ids[num_colonize:]

    directions = [(0, -leg, 0), (0, leg, 0), (leg, 0, 0), (-leg, 0, 0)]  # N, S, E, W
    for i, ship_id in enumerate(fan_ids):
        dx, dy, dz = directions[i % 4]
        confirm_move(player_token, ship_id, hx+dx, hy+dy, hz+dz, jump_range_per_turn=jump_range)
    for ship_id in colonize_ids:
        set_mission(player_token, ship_id, "colonize")

    memory["dispatched"] = True
    memory["colonized"] = colonize_ids
    memory["fanned_out"] = fan_ids
    return memory

def _frontier_map_stay_frosty(player_id: int, player_token: str, config: dict, memory: dict) -> dict:
    """
    Continuous reconnaissance: ships never settle. Every call, any currently
    idle (landed, not colonized, not already moving) mobile ship gets sent
    further out in its assigned cardinal direction, scanner aimed further
    ahead in the same direction. No colonizing, ever -- mobility is the
    identity. A ship's aim is set on an offset relative to wherever it
    currently is (see set_org_scan_bearing), re-evaluated fresh at scan
    resolution each turn, so aiming "further ahead" before a move already
    resolves correctly once the ship lands -- no ship's-log queuing needed
    for that part.
    No second-leg/opening-vs-followup split like the other strategies: this
    one has no terminal state, it just runs the same land-then-redirect
    check every time run_npc_decisions() calls it (once per turn, per
    engine/turn.py's step 0) -- a ship gets roughly one turn of production
    at each waypoint before being redirected again, since arrival resolution
    (which resets mission to 'idle') happens in step 2, one step after this.
    Config: leg_distance (default 3), jump_range_per_turn (default 1).
    Assigns directions round-robin across whichever ships are seen first;
    remembered per ship_id in memory so a ship keeps heading the same way
    turn over turn rather than being reassigned randomly.
    Scan aim is fixed at the "X2" bearings (2 sectors -- the maximum
    SCAN_RANGE allows) regardless of leg_distance: movement has no range cap
    but scanning does, so the two are deliberately decoupled rather than
    trying to make an aim match however far the ship happens to jump.
    """
    leg = config.get("leg_distance", 3)
    jump_range = config.get("jump_range_per_turn", 1)
    bearing_cycle = ["N", "S", "E", "W"]
    offsets = {"N": (0, -leg, 0), "S": (0, leg, 0), "E": (leg, 0, 0), "W": (-leg, 0, 0)}

    conn = get_connection(); cur = conn.cursor()
    ships = cur.execute("""SELECT o.id, s.coord_x, s.coord_y, s.coord_z
        FROM organizations o JOIN sectors s ON s.id=o.sector_id
        WHERE o.player_id=? AND o.org_type='ship' AND o.sector_id!=-1 AND o.mission='idle'
        ORDER BY o.id""", (player_id,)).fetchall()
    conn.close()

    directions = memory.setdefault("directions", {})
    for ship in ships:
        key = str(ship["id"])
        if key not in directions:
            directions[key] = bearing_cycle[len(directions) % len(bearing_cycle)]
        bearing = directions[key]
        dx, dy, dz = offsets[bearing]
        confirm_move(player_token, ship["id"], ship["coord_x"]+dx, ship["coord_y"]+dy, ship["coord_z"]+dz,
                    jump_range_per_turn=jump_range)
        set_org_scan_bearing(player_token, ship["id"], bearing=bearing+"2")
    return memory

def _fan_out(player_id: int, player_token: str, config: dict, memory: dict) -> dict:
    """
    Distribute outward AND find opportunity -- distinct from
    fan_out_consolidate's blind fixed-offset second leg. Opening: every
    ship takes a short scout hop in its assigned cardinal direction, aimed
    scanner pointed further out the same way. Once a scout lands (mission
    back to 'idle') and its aimed scan has actually resolved (a
    player_sectors row with confidence>0 exists at the aim target), it
    commits its final move TO that revealed sector -- what was actually
    found steers the destination, not a hardcoded further offset. Until the
    scan resolves, the scout just waits in place (one more turn of
    production at the scout stop) rather than guessing.
    v1 simplification, worth being honest about: commits to whatever sector
    was found regardless of quality (no energy_capacity threshold or
    comparison against alternatives) -- "find opportunity" here means
    "react to what scouting reveals" rather than "evaluate and optimize
    among discoveries." A smarter version is a real future step, not this one.
    Config: scout_distance (default 2 -- matches scan range, so the aimed
    scan lands exactly on the scout's next hop), jump_range_per_turn
    (default 1).
    """
    scout_distance = config.get("scout_distance", 2)
    jump_range = config.get("jump_range_per_turn", 1)
    bearing_cycle = ["N", "S", "E", "W"]
    offsets = {"N": (0, -scout_distance, 0), "S": (0, scout_distance, 0),
               "E": (scout_distance, 0, 0), "W": (-scout_distance, 0, 0)}

    if not memory.get("opening_dispatched"):
        conn = get_connection(); cur = conn.cursor()
        ship_ids = [r["id"] for r in cur.execute(
            "SELECT id FROM organizations WHERE player_id=? AND org_type='ship' ORDER BY id",
            (player_id,)).fetchall()]
        if not ship_ids:
            conn.close()
            memory["opening_dispatched"] = True
            return memory
        home = cur.execute("""SELECT s.coord_x,s.coord_y,s.coord_z FROM organizations o
            JOIN sectors s ON s.id=o.sector_id WHERE o.id=?""", (ship_ids[0],)).fetchone()
        conn.close()
        hx, hy, hz = home["coord_x"], home["coord_y"], home["coord_z"]

        scouts = {}
        for i, ship_id in enumerate(ship_ids):
            bearing = bearing_cycle[i % len(bearing_cycle)]
            dx, dy, dz = offsets[bearing]
            confirm_move(player_token, ship_id, hx+dx, hy+dy, hz+dz, jump_range_per_turn=jump_range)
            # Aim is the "X2" bearing (2 sectors -- SCAN_RANGE's max), not the
            # plain 1-sector bearing: with scout_distance defaulting to 2 this
            # coincidentally lines up, but the aim distance is governed by
            # scan range, not by however far scout_distance happens to be set.
            set_org_scan_bearing(player_token, ship_id, bearing=bearing+"2")
            scouts[str(ship_id)] = {"bearing": bearing, "committed": False}
        memory["scouts"] = scouts
        memory["opening_dispatched"] = True
        return memory

    conn = get_connection(); cur = conn.cursor()
    for ship_id_str, info in memory.get("scouts", {}).items():
        if info["committed"]:
            continue
        ship_id = int(ship_id_str)
        org = cur.execute("""SELECT o.sector_id, o.mission, s.coord_x, s.coord_y, s.coord_z,
                                    o.scan_offset_x, o.scan_offset_y, o.scan_offset_z
            FROM organizations o JOIN sectors s ON s.id=o.sector_id WHERE o.id=?""",
            (ship_id,)).fetchone()
        if not org or org["sector_id"] == -1 or org["mission"] != "idle" or org["scan_offset_x"] is None:
            continue  # still travelling, or nothing aimed -- nothing to evaluate yet
        tx = org["coord_x"] + org["scan_offset_x"]
        ty = org["coord_y"] + org["scan_offset_y"]
        tz = org["coord_z"] + org["scan_offset_z"]
        found = cur.execute("""SELECT 1 FROM sectors s JOIN player_sectors ps ON ps.sector_id=s.id
            WHERE s.coord_x=? AND s.coord_y=? AND s.coord_z=? AND ps.player_id=? AND ps.confidence>0""",
            (tx, ty, tz, player_id)).fetchone()
        if not found:
            continue  # scan hasn't resolved yet -- wait another turn
        confirm_move(player_token, ship_id, tx, ty, tz, jump_range_per_turn=jump_range)
        info["committed"] = True
    conn.close()
    return memory

STRATEGIES = {
    "fan_out_consolidate": _fan_out_consolidate,
    "turtle": _turtle,
    "burst_and_colonize": _burst_and_colonize,
    "frontier_map_stay_frosty": _frontier_map_stay_frosty,
    "fan_out": _fan_out,
}

def run_npc_decisions():
    """
    Run every NPC player's registered strategy once. Called as step 0 of
    engine/turn.py's end_of_turn(), before that turn's own resolution
    begins. Unknown strategy_name values are skipped rather than raising --
    a profile pointing at a since-removed/renamed strategy shouldn't crash
    the whole turn for every player.
    """
    conn = get_connection(); cur = conn.cursor()
    npcs = cur.execute("""
        SELECT pl.id AS player_id, pl.player_token, np.strategy_name, np.config, np.memory
        FROM players pl JOIN npc_profiles np ON np.player_id = pl.id
        WHERE pl.is_npc = 1
    """).fetchall()
    conn.close()

    for row in npcs:
        strategy = STRATEGIES.get(row["strategy_name"])
        if strategy is None:
            continue
        config = json.loads(row["config"] or "{}")
        memory = json.loads(row["memory"] or "{}")
        memory = strategy(row["player_id"], row["player_token"], config, memory)
        conn = get_connection()
        conn.execute("UPDATE npc_profiles SET memory=? WHERE player_id=?",
                     (json.dumps(memory), row["player_id"]))
        conn.commit(); conn.close()
