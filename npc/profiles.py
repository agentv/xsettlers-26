import json
from db.connection import connection
from npc.script import validate_program

def assign_npc_profile(player_id: int, strategy_name: str, config: dict = None) -> dict:
    """
    Mark a player as NPC-controlled and give it a named strategy profile
    (see npc/strategies.py's strategy_names() for valid names). Reassigning a
    strategy to a player that already has one replaces it and resets memory
    to {} -- a strategy's working memory is only meaningful to that strategy,
    so carrying it over to a different one would be a foreign, unparseable
    blob rather than a useful head start.
    Dev/test-only: not exposed as an MCP tool (see docs/TODO.md).

    A config carrying an inline `program` is validated here and refused if it
    is malformed, before anything is written. This is the assignment-time half
    of the rule queue_command already applies to a single order: a program is
    authored by a person, so it has to be rejected while they are still
    holding it, not three turns later inside a clock tick. Named programs from
    config/npc_programs/ are validated by their own test, not on every assign.
    """
    program_error = validate_program((config or {}).get("program"))
    if program_error:
        return program_error
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
