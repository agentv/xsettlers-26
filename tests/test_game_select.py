from db.connection import get_connection
from xsettlers_mcp.game_select import list_scenarios, get_active_game, select_scenario

def _clear_active_game():
    conn = get_connection()
    conn.execute("DELETE FROM games")
    conn.commit(); conn.close()

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
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    conn.close()
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
