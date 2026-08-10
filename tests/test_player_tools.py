from xsettlers_mcp.tools.player_tools import set_display_name
from tests.conftest import seed_player

def test_set_display_name_changes_the_name():
    seed_player(player_token="U_P1", display_name="Player 1")
    result = set_display_name("U_P1", "Voyager")
    assert result["ok"] is True
    assert result["previous_display_name"] == "Player 1"
    assert result["display_name"] == "Voyager"

def test_set_display_name_rejects_a_game_wide_duplicate():
    seed_player(email="p1@test.com", player_token="U_P1", display_name="Player 1")
    seed_player(email="p2@test.com", player_token="U_P2", display_name="Player 2")
    result = set_display_name("U_P2", "player 1")  # case-insensitive
    assert "error" in result

def test_set_display_name_rejects_empty_and_overlong_names():
    seed_player(player_token="U_P1")
    assert "error" in set_display_name("U_P1", "   ")
    assert "error" in set_display_name("U_P1", "x" * 25)
    assert set_display_name("U_P1", "  Trimmed  ")["display_name"] == "Trimmed"

def test_set_display_name_unknown_player():
    assert "error" in set_display_name("U_NOBODY", "Anything")
