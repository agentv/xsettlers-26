"""
Assigning a strategy document to an NPC player.

Filed under npc/ rather than db/ because validating at assign time needs
strategy.validate_strategy -- under db/ that import would drag the whole NPC
layer back beneath the tool layer and close the cycle the layering forbids.
"""
import json
from db.connection import connection
from npc.library import get_strategy, strategy_names
from npc.strategy import validate_strategy

def assign_npc_profile(player_id: int, strategy_name: str, config: dict = None) -> dict:
    """
    Mark a player as NPC-controlled and point it at a strategy from the
    library (npc/library.py). Reassigning replaces the strategy and resets
    memory to {} -- memory holds a program counter and bindings into a
    specific document, so carrying it into a different one would leave the new
    strategy starting partway through steps it never had.
    Dev/test-only: not exposed as an MCP tool (see docs/dev_history.md).

    The strategy must exist and its document must be sound, both checked here,
    before anything is written. This is assignment-time validation for the
    same reason queue_command validates a single order up front: a document is
    authored by a person -- eventually in a builder, eventually by a player
    trading one -- so an error has to reach them while they are still holding
    it, not three turns later inside a clock tick with no one to tell.
    """
    document = get_strategy(strategy_name)
    if document is None:
        return {"error": f"Unknown strategy '{strategy_name}'. Valid: {strategy_names()}"}
    invalid = validate_strategy(document)
    if invalid:
        return {"error": f"Strategy '{strategy_name}' is malformed: {invalid['error']}"}
    with connection() as conn:
        conn.execute("UPDATE players SET is_npc=1 WHERE id=?", (player_id,))
        conn.execute("""
            INSERT INTO npc_profiles (player_id, strategy_name, config, memory)
            VALUES (?, ?, ?, '{}')
            ON CONFLICT(player_id) DO UPDATE SET
                strategy_name = excluded.strategy_name,
                config = excluded.config,
                memory = '{}'
        """, (player_id, strategy_name, json.dumps(config or {})))
    return {"ok": True, "player_id": player_id, "strategy_name": strategy_name}
