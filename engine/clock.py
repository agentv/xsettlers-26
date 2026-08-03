import asyncio, os
from datetime import datetime, timedelta, timezone
from db.connection import get_connection
from engine.turn import end_of_turn
TICK_SECONDS = int(os.getenv("GAME_TICK_SECONDS", 300))

# How often run_clock() wakes up to check the frozen flag. Independent of
# TICK_SECONDS -- this is the responsiveness of freeze/unfreeze (see
# scripts/clock.py), not the game's turn cadence.
POLL_SECONDS = 1

def _stamp_next_tick_at(seconds_remaining: int = TICK_SECONDS):
    """
    Persist when the next tick will fire, so a status report (which runs as
    its own process, not inside the server) can show a countdown without
    needing to ask the live server directly. `seconds_remaining` lets
    run_clock() re-stamp mid-cycle when resuming from a freeze, so the
    countdown reflects progress already made rather than jumping back to a
    full TICK_SECONDS.
    """
    next_tick = datetime.now(timezone.utc) + timedelta(seconds=seconds_remaining)
    conn = get_connection()
    conn.execute("UPDATE game_state SET next_tick_at=? WHERE id=1",
                (next_tick.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",))
    conn.commit(); conn.close()

def is_frozen() -> bool:
    conn = get_connection()
    row = conn.execute("SELECT frozen FROM game_state WHERE id=1").fetchone()
    conn.close()
    return bool(row and row["frozen"])

def freeze():
    conn = get_connection()
    conn.execute("UPDATE game_state SET frozen=1 WHERE id=1")
    conn.commit(); conn.close()

def unfreeze():
    conn = get_connection()
    conn.execute("UPDATE game_state SET frozen=0 WHERE id=1")
    conn.commit(); conn.close()

async def run_clock():
    """
    Polls game_state.frozen every POLL_SECONDS rather than sleeping straight
    through TICK_SECONDS, so a freeze set by another process (scripts/clock.py,
    talking to the same DB) takes effect within ~1s instead of only being
    noticed at the next tick boundary. Time spent frozen doesn't count toward
    the interval -- elapsed only accumulates on unfrozen polls.
    """
    print(f"Game clock started. Tick interval: {TICK_SECONDS}s")
    elapsed = 0
    was_frozen = False
    _stamp_next_tick_at(TICK_SECONDS)
    while True:
        await asyncio.sleep(POLL_SECONDS)
        if is_frozen():
            if not was_frozen:
                print("Clock frozen.")
            was_frozen = True
            continue
        if was_frozen:
            print("Clock resumed.")
            _stamp_next_tick_at(TICK_SECONDS - elapsed)
        was_frozen = False
        elapsed += POLL_SECONDS
        if elapsed < TICK_SECONDS:
            continue
        print("Clock tick — running end of turn.")
        end_of_turn()
        elapsed = 0
        _stamp_next_tick_at(TICK_SECONDS)
