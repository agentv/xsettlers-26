"""
NPC turn entry point. Each is_npc=1 player with a row in npc_profiles gets its
strategy advanced once per turn, early in end_of_turn() (see
run_npc_decisions, called from engine/turn.py before any turn-resolution
processing).

A strategy acts by calling the exact same tool functions a human player calls
through MCP -- no special-cased write path, no direct mutation of
organizations or pods from here. Each tool call commits its own connection
before the next NPC is processed, so nothing is left open when
end_of_turn()'s own turn-resolution transaction opens right after this
returns.

**Every strategy is data.** There is one model: a document in
config/npc_strategies/ (npc/library.py), walked by npc/strategy.py. There is
no Python-function strategy and no registry of them -- a strategy that needs
to read the board and decide uses a `decide` step, which is the whole reason
that step exists.

Per-NPC working state -- program counter and bindings -- lives in
npc_profiles.memory, a JSON blob rewritten each turn, the same pattern
pods.task_params uses for per-pod working state.
"""
import json

from db.connection import connection, read_all
from npc.library import load_strategies, strategy_names  # noqa: F401  (re-exported)
from npc.strategy import run_strategy


def run_npc_decisions():
    """
    Advance every NPC player's strategy by one turn. Called as step 0 of
    engine/turn.py's end_of_turn(), before that turn's own resolution begins.

    A profile naming a strategy the library no longer has is skipped rather
    than raising -- a since-renamed strategy shouldn't crash the whole turn
    for every player, including the humans.
    """
    npcs = read_all("""
        SELECT pl.id AS player_id, pl.player_token, np.strategy_name, np.config, np.memory
        FROM players pl JOIN npc_profiles np ON np.player_id = pl.id
        WHERE pl.is_npc = 1
    """)

    strategies = load_strategies()
    for row in npcs:
        document = strategies.get(row["strategy_name"])
        if document is None:
            continue
        memory = run_strategy(row["player_id"], row["player_token"],
                              document, json.loads(row["memory"] or "{}"),
                              config_override=json.loads(row["config"] or "{}"))
        with connection() as conn:
            conn.execute("UPDATE npc_profiles SET memory=? WHERE player_id=?",
                         (json.dumps(memory), row["player_id"]))
