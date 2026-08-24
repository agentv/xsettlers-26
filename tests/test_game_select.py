from db.connection import connection
from xsettlers_mcp.game_select import list_scenarios, get_active_game, select_scenario

def _clear_active_game():
    with connection() as conn:
        conn.execute("DELETE FROM games")

def test_list_scenarios_finds_game0():
    scenarios = list_scenarios()
    names = {s["scenario_name"] for s in scenarios}
    assert "game0" in names
    game0 = next(s for s in scenarios if s["scenario_name"] == "game0")
    assert game0["name"] == "Diaspora"
    assert game0["description"]

def test_list_scenarios_finds_game1():
    scenarios = list_scenarios()
    names = {s["scenario_name"] for s in scenarios}
    assert "game1" in names
    game1 = next(s for s in scenarios if s["scenario_name"] == "game1")
    assert game1["name"] == "Outbreak"
    assert game1["description"]

def test_list_scenarios_finds_the_solo_scenario():
    solo = next(s for s in list_scenarios() if s["scenario_name"] == "game_solo")
    assert solo["player_count"] == 1

def test_list_scenarios_returns_only_games_the_player_is_seated_in():
    """A token is an invitation to specific games, not to the whole library.
    Player Two is in the directory but not a participant in game_solo."""
    mine = {s["scenario_name"] for s in list_scenarios("REPLACE_WITH_GENERATED_TOKEN_2")}
    assert mine == {"game0", "game1"}
    everyones = {s["scenario_name"] for s in list_scenarios("REPLACE_WITH_GENERATED_TOKEN_1")}
    assert everyones == {"game0", "game1", "game_solo"}

def test_list_scenarios_tells_an_unknown_token_nothing():
    assert list_scenarios("U_NOT_ON_ROSTER") == []

def test_select_scenario_rejects_a_known_player_not_seated_in_that_scenario():
    _clear_active_game()
    result = select_scenario("REPLACE_WITH_GENERATED_TOKEN_2", "game_solo")
    assert result["ok"] is False
    assert "not a participant" in result["error"]
    assert get_active_game() is None      # nothing was bootstrapped

def test_select_scenario_bootstraps_a_solo_game_with_one_player():
    """Player count is a property of the scenario, not the service -- no code
    path branches on how many participants there are."""
    _clear_active_game()
    result = select_scenario("REPLACE_WITH_GENERATED_TOKEN_1", "game_solo")
    assert result["ok"] is True
    with connection() as conn:
        players = conn.execute("SELECT display_name FROM players").fetchall()
    assert [p["display_name"] for p in players] == ["Vincent"]

def test_get_active_game_none_before_selection():
    _clear_active_game()
    assert get_active_game() is None

def test_select_scenario_unknown_player():
    _clear_active_game()
    result = select_scenario("U_NOT_ON_ROSTER", "game0")
    assert result["ok"] is False

def test_select_scenario_unknown_scenario_name():
    _clear_active_game()
    result = select_scenario("REPLACE_WITH_GENERATED_TOKEN_1", "does_not_exist")
    assert "error" in result

def test_select_scenario_bootstraps_and_activates():
    _clear_active_game()
    result = select_scenario("REPLACE_WITH_GENERATED_TOKEN_1", "game0")
    assert result["ok"] is True
    assert result["already_active"] is False
    active = get_active_game()
    assert active["scenario_name"] == "game0"
    # Roster players now exist in the DB, seeded by bootstrap_game()
    with connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    assert count == 2  # Vincent + Player Two, per game_config.yaml

def test_select_scenario_idempotent_same_scenario():
    _clear_active_game()
    select_scenario("REPLACE_WITH_GENERATED_TOKEN_1", "game0")
    result = select_scenario("REPLACE_WITH_GENERATED_TOKEN_1", "game0")
    assert result["ok"] is True
    assert result["already_active"] is True

def test_select_scenario_rejects_switching_once_active():
    _clear_active_game()
    select_scenario("REPLACE_WITH_GENERATED_TOKEN_1", "game0")
    result = select_scenario("REPLACE_WITH_GENERATED_TOKEN_1", "game1")
    assert "error" in result
    active = get_active_game()
    assert active["scenario_name"] == "game0"  # unchanged
