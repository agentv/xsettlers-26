"""
There's no separate mcp/gateway.py module. select_scenario() is the one
explicit gate, and every gameplay tool already checks player existence
internally (SELECT id FROM players WHERE slack_user_id=?) -- before a
scenario is selected, players is empty, so those checks reject on their own.
These tests verify that combination actually gates access end-to-end, which
is what a dedicated gateway module's tests would otherwise cover.
"""
from db.connection import get_connection
from mcp.game_select import select_scenario
from mcp.tools.organization_tools import show_game_status

def _clear_active_game():
    conn = get_connection()
    conn.execute("DELETE FROM games")
    conn.commit(); conn.close()

def test_gameplay_blocked_before_any_scenario_selected():
    _clear_active_game()
    result = show_game_status("U0BF2CE53GA")
    assert "error" in result

def test_gameplay_blocked_for_unrecognized_player_even_after_selection():
    _clear_active_game()
    select_scenario("U0BF2CE53GA", "game0")
    result = show_game_status("U_TOTAL_STRANGER")
    assert "error" in result

def test_gameplay_works_for_roster_player_after_selection():
    _clear_active_game()
    select_scenario("U0BF2CE53GA", "game0")
    result = show_game_status("U0BF2CE53GA")
    assert "error" not in result
    assert result["turn"] == 0
