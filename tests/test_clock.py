from datetime import datetime, timezone
from db.connection import get_connection
from engine.clock import _stamp_next_tick_at, TICK_SECONDS, is_frozen, freeze, unfreeze

def test_stamp_next_tick_at_writes_approx_tick_seconds_ahead():
    _stamp_next_tick_at()
    conn = get_connection()
    row = conn.execute("SELECT next_tick_at FROM game_state WHERE id=1").fetchone()
    conn.close()
    next_dt = datetime.strptime(row["next_tick_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    delta = (next_dt - datetime.now(timezone.utc)).total_seconds()
    assert TICK_SECONDS - 2 <= delta <= TICK_SECONDS

def test_stamp_next_tick_at_honors_seconds_remaining():
    _stamp_next_tick_at(seconds_remaining=10)
    conn = get_connection()
    row = conn.execute("SELECT next_tick_at FROM game_state WHERE id=1").fetchone()
    conn.close()
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
