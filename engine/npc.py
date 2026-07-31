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
from engine.turn import get_current_turn
from xsettlers_mcp.tools.navigation_tools import confirm_move

def _fan_out_consolidate(player_id: int, player_token: str, config: dict, memory: dict) -> dict:
    """
    Ported from player2_policy.py: fan out in 4 directions (2 ships per
    direction, first 8 ships found), hold and harvest for `hold_turns`, then
    one ship per pair (the "mover") jumps `leg_distance` further out in the
    same direction while the other (the "stayer") stays put. From there
    everyone just settles in and produces -- no further decisions after the
    second leg.
    Config: leg_distance (default 3), jump_range_per_turn (default 1),
    hold_turns (default 2).
    Requires at least 8 of the player's organizations to be ships; if fewer
    are available the strategy marks itself done without acting (nothing
    sensible to fan out) rather than retrying every turn indefinitely.
    """
    leg = config.get("leg_distance", 3)
    jump_range = config.get("jump_range_per_turn", 1)
    hold_turns = config.get("hold_turns", 2)

    if not memory.get("opening_dispatched"):
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
        arrival_turn = None
        plan = {"stayer": {}, "mover": {}, "second_dest": {}}
        for name, (stayer, mover, dest, vec) in groups.items():
            for ship_id in (stayer, mover):
                r = confirm_move(player_token, ship_id, *dest, jump_range_per_turn=jump_range)
                if "error" not in r:
                    arrival_turn = r["arrival_turn"]
            plan["stayer"][name] = stayer
            plan["mover"][name] = mover
            plan["second_dest"][name] = [dest[0]+vec[0], dest[1]+vec[1], dest[2]+vec[2]]
        memory.update(plan)
        memory["opening_dispatched"] = True
        memory["second_leg_dispatched"] = False
        memory["second_leg_turn"] = (arrival_turn if arrival_turn is not None else get_current_turn()) + hold_turns
        return memory

    if memory.get("second_leg_dispatched"):
        return memory
    if get_current_turn() < memory.get("second_leg_turn", 0):
        return memory

    for name, mover in memory["mover"].items():
        dest = memory["second_dest"][name]
        confirm_move(player_token, mover, *dest, jump_range_per_turn=jump_range)
    memory["second_leg_dispatched"] = True
    return memory

STRATEGIES = {
    "fan_out_consolidate": _fan_out_consolidate,
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
