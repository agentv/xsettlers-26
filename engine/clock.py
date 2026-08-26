import asyncio, os
from datetime import datetime, timedelta, timezone
from db.connection import connection, read_value
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
    with connection() as conn:
        conn.execute("UPDATE game_state SET next_tick_at=? WHERE id=1",
                     (next_tick.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",))

def is_frozen() -> bool:
    return bool(read_value("SELECT frozen FROM game_state WHERE id=1"))

def _game_is_active() -> bool:
    """
    Whether a scenario has been bootstrapped. The clock process runs from
    server startup, but the turn interval does not start accumulating until
    there is a game: otherwise turn 1 inherits whatever was left of the
    window already in progress when the game was created, and a player can
    lose most of their first turn to a countdown that started before they
    existed. Read straight from `games` rather than through
    xsettlers_mcp.game_select -- engine/ never imports upward.
    """
    return read_value("SELECT id FROM games WHERE id=1") is not None

def freeze():
    _set_frozen(1)

def unfreeze():
    _set_frozen(0)

def _set_frozen(value: int):
    with connection() as conn:
        conn.execute("UPDATE game_state SET frozen=? WHERE id=1", (value,))

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
    had_game = False
    _stamp_next_tick_at(TICK_SECONDS)
    while True:
        await asyncio.sleep(POLL_SECONDS)
        # No game, no turns. end_of_turn() would no-op anyway, but letting
        # elapsed run would carry a partial window into the game's first
        # turn -- see _game_is_active.
        if not _game_is_active():
            elapsed = 0
            had_game = False
            continue
        if not had_game:
            print("Game bootstrapped — turn 1 begins now.")
            elapsed = 0
            _stamp_next_tick_at(TICK_SECONDS)
        had_game = True
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
