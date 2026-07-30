import asyncio, os
from datetime import datetime, timedelta, timezone
from db.connection import get_connection
from engine.turn import end_of_turn
TICK_SECONDS = int(os.getenv("GAME_TICK_SECONDS", 300))

def _stamp_next_tick_at():
    """
    Persist when the next tick will fire, so a status report (which runs as
    its own process, not inside the server) can show a countdown without
    needing to ask the live server directly. Written fresh each cycle --
    on a restart, run_clock() calls this before its first sleep, so the
    countdown correctly resets to a full TICK_SECONDS from restart time,
    matching how a restart actually behaves.
    """
    next_tick = datetime.now(timezone.utc) + timedelta(seconds=TICK_SECONDS)
    conn = get_connection()
    conn.execute("UPDATE game_state SET next_tick_at=? WHERE id=1",
                (next_tick.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",))
    conn.commit(); conn.close()

async def run_clock():
    print(f"Game clock started. Tick interval: {TICK_SECONDS}s")
    while True:
        _stamp_next_tick_at()
        await asyncio.sleep(TICK_SECONDS)
        print("Clock tick — running end of turn.")
        end_of_turn()
