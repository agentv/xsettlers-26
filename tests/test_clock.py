from datetime import datetime, timezone
from db.connection import connection
from engine.clock import (_stamp_next_tick_at, TICK_SECONDS, is_frozen, freeze,
                          unfreeze, _game_is_active as clock_game_is_active)

def test_stamp_next_tick_at_writes_approx_tick_seconds_ahead():
    _stamp_next_tick_at()
    with connection() as conn:
        row = conn.execute("SELECT next_tick_at FROM game_state WHERE id=1").fetchone()
    next_dt = datetime.strptime(row["next_tick_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    delta = (next_dt - datetime.now(timezone.utc)).total_seconds()
    assert TICK_SECONDS - 2 <= delta <= TICK_SECONDS

def test_stamp_next_tick_at_honors_seconds_remaining():
    _stamp_next_tick_at(seconds_remaining=10)
    with connection() as conn:
        row = conn.execute("SELECT next_tick_at FROM game_state WHERE id=1").fetchone()
    next_dt = datetime.strptime(row["next_tick_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    delta = (next_dt - datetime.now(timezone.utc)).total_seconds()
    assert 8 <= delta <= 10

def test_clock_starts_unfrozen():
    assert is_frozen() is False

def test_freeze_and_unfreeze_round_trip():
    freeze()
    assert is_frozen() is True
    unfreeze()
    assert is_frozen() is False

def test_no_game_means_no_turns_and_no_accumulated_interval(monkeypatch):
    """
    The clock process starts with the server, but a game is bootstrapped at an
    arbitrary moment inside a tick window. Turn 1 has to get a whole window --
    without the gate it inherits whatever was left of the one already running,
    which at a 300s cadence can be a few seconds.
    """
    import asyncio
    import engine.clock as clock
    from tests.conftest import seed_active_game

    calls = []
    monkeypatch.setattr(clock, "TICK_SECONDS", 0.2)
    monkeypatch.setattr(clock, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(clock, "end_of_turn", lambda: calls.append(1))

    async def scenario():
        with connection() as conn:
            conn.execute("DELETE FROM games")
        task = asyncio.create_task(clock.run_clock())
        await asyncio.sleep(0.5)       # several tick windows with no game
        assert calls == [], "a turn resolved before any game existed"

        seed_active_game()
        await asyncio.sleep(0.12)      # inside turn 1's window
        assert calls == [], "turn 1 was cut short by the window already running"

        await asyncio.sleep(0.18)      # past the end of it
        assert len(calls) == 1
        task.cancel()

    asyncio.run(scenario())

def test_game_is_active_tracks_the_games_row():
    assert clock_game_is_active() is True          # conftest seeds one
    with connection() as conn:
        conn.execute("DELETE FROM games")
    assert clock_game_is_active() is False
