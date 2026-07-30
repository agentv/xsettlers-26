import json
from db.connection import get_connection

def assign_npc_profile(player_id: int, strategy_name: str, config: dict = None) -> dict:
    """
    Mark a player as NPC-controlled and give it a named strategy profile
    (see engine/npc.py's STRATEGIES registry for valid names). Reassigning a
    strategy to a player that already has one replaces it and resets memory
    to {} -- a strategy's working memory is only meaningful to that strategy,
    so carrying it over to a different one would be a foreign, unparseable
    blob rather than a useful head start.
    Dev/test-only: not exposed as an MCP tool (see docs/TODO.md).
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE players SET is_npc=1 WHERE id=?", (player_id,))
    cur.execute("""
        INSERT INTO npc_profiles (player_id, strategy_name, config, memory)
        VALUES (?, ?, ?, '{}')
        ON CONFLICT(player_id) DO UPDATE SET
            strategy_name = excluded.strategy_name,
            config = excluded.config,
            memory = '{}'
    """, (player_id, strategy_name, json.dumps(config or {})))
    conn.commit(); conn.close()
    return {"ok": True, "player_id": player_id, "strategy_name": strategy_name}
