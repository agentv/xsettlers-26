from xsettlers_mcp.auth import authenticate

def test_authenticate_known_player():
    result = authenticate("REPLACE_WITH_GENERATED_TOKEN_1")
    assert result["ok"] is True
    assert result["display_name"] == "Vincent"

def test_authenticate_unknown_player():
    result = authenticate("U_NOT_ON_ROSTER")
    assert result["ok"] is False
    assert "error" in result

# --- participation is a separate question from identity ---

def test_authenticate_accepts_a_participant_of_the_named_scenario():
    result = authenticate("REPLACE_WITH_GENERATED_TOKEN_1",
                          scenario_file="config/game_solo.yaml")
    assert result["ok"] is True
    assert result["display_name"] == "Vincent"

def test_authenticate_rejects_a_known_player_who_is_not_a_participant():
    """A token is an identity, not a blanket invitation to every game in the
    library. Player Two is in the directory but not seated in game_solo."""
    result = authenticate("REPLACE_WITH_GENERATED_TOKEN_2",
                          scenario_file="config/game_solo.yaml")
    assert result["ok"] is False
    assert "not a participant" in result["error"]

def test_authenticate_without_a_scenario_asks_only_who_are_you():
    """Same player, same token — accepted when no scenario is named, because
    identity and participation are deliberately separate questions."""
    assert authenticate("REPLACE_WITH_GENERATED_TOKEN_2")["ok"] is True
