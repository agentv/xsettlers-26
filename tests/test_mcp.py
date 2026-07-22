from db.connection import get_connection
from xsettlers_mcp.tools.player_tools import get_player_state, declare_end_turn, rescind_end_turn
from tests.conftest import seed_player

def setup_function():
    conn = get_connection()
    conn.execute("INSERT INTO players (email,display_name,player_token) VALUES (?,?,?)",
                 ("p1@test.com","Player One","U_TEST_001"))
    # A second, non-declaring player so declare_end_turn's consensus check
    # (check_consensus_acceleration) doesn't see "all players declared" and
    # immediately fire end_of_turn(), which would reset end_turn_declared
    # back to 0 before the test gets to assert on it.
    conn.execute("INSERT INTO players (email,display_name,player_token) VALUES (?,?,?)",
                 ("p2@test.com","Player Two","U_TEST_002"))
    conn.commit(); conn.close()

def test_get_player_state_returns_player():
    result = get_player_state("U_TEST_001")
    assert result["player"]["email"] == "p1@test.com"

def test_get_player_state_unknown_user():
    assert "error" in get_player_state("U_NOBODY")

def test_declare_and_rescind_end_turn():
    declare_end_turn("U_TEST_001")
    conn = get_connection()
    assert conn.execute("SELECT end_turn_declared FROM players WHERE player_token='U_TEST_001'"
                        ).fetchone()[0] == 1
    conn.close()
    rescind_end_turn("U_TEST_001")
    conn = get_connection()
    assert conn.execute("SELECT end_turn_declared FROM players WHERE player_token='U_TEST_001'"
                        ).fetchone()[0] == 0
    conn.close()
