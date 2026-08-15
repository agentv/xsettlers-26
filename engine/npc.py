"""
NPC strategy execution. Each is_npc=1 player with a row in npc_profiles gets
its strategy invoked once per turn, early in end_of_turn() (see
run_npc_decisions, called from engine/turn.py before any turn-resolution
processing). A strategy decides and acts by calling the exact same tool
functions any human player calls through the MCP interface -- no
special-cased write path, no direct mutation of organizations/pods from here.
Each tool call commits its own connection before the next NPC is processed, so
nothing is left open when end_of_turn()'s own turn-resolution transaction
opens right after this returns (see the comment at that call site).

**A strategy is either data or code, and most are data.** Openings whose whole
sequence is decided in advance live in config/npc_programs/*.yaml and run
through engine/npc_script.py (turtle, burst_and_colonize, fan_out_consolidate).
What lives in this module is the strategies a program cannot express: ones
that read the world each turn and decide from it. fan_out waits for every
scout's scan to resolve and then converges the whole fleet on the richest
sector found; frontier_map_stay_frosty has no terminal state at all. Both need
conditions, repetition and cross-org coordination, which is a rule engine
rather than a command queue -- see engine/npc_script.py's module docstring for
where that line sits and why.

Write a new strategy as a program first. Only reach for a function here when
it has to look at the board before it can choose.

Per-NPC working memory lives in npc_profiles.memory, a JSON blob mutated turn
to turn -- the same pattern pods.task_params uses for per-pod working state.
"""
import json
from db.connection import get_connection
from engine.npc_programs import load_programs
from engine.npc_script import run_program
from xsettlers_mcp.tools.navigation_tools import confirm_move
from xsettlers_mcp.tools.organization_tools import set_org_scan_bearing

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
    No opening/follow-up split: this strategy has no terminal state, it just
    runs the same land-then-redirect check every time run_npc_decisions()
    calls it (once per turn, per
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
    Distribute outward, scan, then converge on the single best find --
    distinct from fan_out_consolidate's blind fixed-offset second leg in that
    the fleet compares every scout's reading before committing.
    Opening: every ship takes a short scout hop in its assigned cardinal
    direction, aimed scanner pointed further out the same way. Once a scout
    lands (mission back to 'idle') and its aimed scan has actually resolved
    (a player_sectors row with confidence>0 exists at the aim target), its
    find (coords + energy_capacity) is recorded but nobody moves yet. Only
    once every scout's scan has resolved does the fleet act: every ship --
    not just whichever one found it -- moves to the single sector with the
    highest energy_capacity among everything the fleet collectively found.
    Two scouts sharing a bearing (8 ships over 4 directions) share an aim
    too, so they'll independently confirm the same reading for that
    direction -- redundant, not wrong, and cheap insurance against losing
    a direction's data to one scout's own delay.
    Config: scout_distance (default 2 -- matches scan range, so the aimed
    scan lands exactly on the scout's next hop), jump_range_per_turn
    (default 1).
    """
    scout_distance = config.get("scout_distance", 2)
    jump_range = config.get("jump_range_per_turn", 1)
    bearing_cycle = ["N", "S", "E", "W"]
    offsets = {"N": (0, -scout_distance, 0), "S": (0, scout_distance, 0),
               "E": (scout_distance, 0, 0), "W": (-scout_distance, 0, 0)}

    if memory.get("converged"):
        return memory

    if not memory.get("opening_dispatched"):
        conn = get_connection(); cur = conn.cursor()
        ship_ids = [r["id"] for r in cur.execute(
            "SELECT id FROM organizations WHERE player_id=? AND org_type='ship' ORDER BY id",
            (player_id,)).fetchall()]
        if not ship_ids:
            conn.close()
            memory["opening_dispatched"] = True
            memory["converged"] = True
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
            scouts[str(ship_id)] = {"bearing": bearing}
        memory["scouts"] = scouts
        memory["found"] = {}
        memory["opening_dispatched"] = True
        return memory

    conn = get_connection(); cur = conn.cursor()
    for ship_id_str in memory.get("scouts", {}):
        if ship_id_str in memory["found"]:
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
        found = cur.execute("""SELECT s.energy_capacity FROM sectors s JOIN player_sectors ps ON ps.sector_id=s.id
            WHERE s.coord_x=? AND s.coord_y=? AND s.coord_z=? AND ps.player_id=? AND ps.confidence>0""",
            (tx, ty, tz, player_id)).fetchone()
        if not found:
            continue  # scan hasn't resolved yet -- wait another turn
        memory["found"][ship_id_str] = {"x": tx, "y": ty, "z": tz,
                                        "energy_capacity": found["energy_capacity"]}

    if len(memory["found"]) < len(memory["scouts"]):
        conn.close()
        return memory  # still waiting on at least one scout's scan

    best = max(memory["found"].values(), key=lambda f: f["energy_capacity"])
    for ship_id_str in memory["scouts"]:
        confirm_move(player_token, int(ship_id_str), best["x"], best["y"], best["z"],
                     jump_range_per_turn=jump_range)
    memory["destination"] = best
    memory["converged"] = True
    conn.close()
    return memory

def _scripted(player_id: int, player_token: str, config: dict, memory: dict) -> dict:
    """
    Run whatever program this profile carries in its own config -- the entry
    point for a program that has no name in the library, which is what an NPC
    builder will produce. A named program from config/npc_programs/ arrives
    here too, resolved by run_npc_decisions below.
    Config: program (see engine/npc_script.py for the step format).
    """
    return run_program(player_id, player_token, config.get("program"), memory)

# Code strategies: the ones that read the world and decide, which a program
# cannot express (see engine/npc_script.py's module docstring on why that line
# is drawn where it is rather than filled in). Everything scripted lives in
# config/npc_programs/ instead.
STRATEGIES = {
    "frontier_map_stay_frosty": _frontier_map_stay_frosty,
    "fan_out": _fan_out,
    "scripted": _scripted,
}

def strategy_names() -> list:
    """Every strategy_name that can be assigned: code strategies plus the named
    programs. This union is the contract GameHouse validates a roster against
    and the field scripts/run_tournament.py plays, so which side of the
    data/code line a strategy sits on is invisible to both."""
    return sorted(set(STRATEGIES) | set(load_programs()))

def run_npc_decisions():
    """
    Run every NPC player's registered strategy once. Called as step 0 of
    engine/turn.py's end_of_turn(), before that turn's own resolution
    begins. Unknown strategy_name values are skipped rather than raising --
    a profile pointing at a since-removed/renamed strategy shouldn't crash
    the whole turn for every player.

    A name is resolved against the code strategies first, then the program
    library; a named program runs through the same scripted runner as a
    profile carrying its own inline program.
    """
    conn = get_connection(); cur = conn.cursor()
    npcs = cur.execute("""
        SELECT pl.id AS player_id, pl.player_token, np.strategy_name, np.config, np.memory
        FROM players pl JOIN npc_profiles np ON np.player_id = pl.id
        WHERE pl.is_npc = 1
    """).fetchall()
    conn.close()

    programs = load_programs()
    for row in npcs:
        name = row["strategy_name"]
        config = json.loads(row["config"] or "{}")
        memory = json.loads(row["memory"] or "{}")
        strategy = STRATEGIES.get(name)
        if strategy is not None:
            memory = strategy(row["player_id"], row["player_token"], config, memory)
        elif name in programs:
            memory = run_program(row["player_id"], row["player_token"],
                                 programs[name], memory)
        else:
            continue
        conn = get_connection()
        conn.execute("UPDATE npc_profiles SET memory=? WHERE player_id=?",
                     (json.dumps(memory), row["player_id"]))
        conn.commit(); conn.close()
