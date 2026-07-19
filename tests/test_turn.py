from db.connection import get_connection
from engine.turn import end_of_turn, check_consensus_acceleration
from tests.conftest import seed_player

def test_turn_resets_end_turn_declared():
    seed_player()
    conn = get_connection()
    conn.execute("UPDATE players SET end_turn_declared=1"); conn.commit(); conn.close()
    end_of_turn()
    conn = get_connection()
    assert conn.execute("SELECT end_turn_declared FROM players").fetchone()[0] == 0
    conn.close()

def test_consensus_fires_when_all_declared():
    seed_player()
    conn = get_connection()
    conn.execute("UPDATE players SET end_turn_declared=1"); conn.commit(); conn.close()
    assert check_consensus_acceleration() is True

def test_consensus_does_not_fire_when_undeclared():
    seed_player()
    assert check_consensus_acceleration() is False
