from xsettlers_mcp.auth import authenticate

def test_authenticate_known_player():
    result = authenticate("REPLACE_WITH_GENERATED_TOKEN_1")
    assert result["ok"] is True
    assert result["display_name"] == "Vincent"

def test_authenticate_unknown_player():
    result = authenticate("U_NOT_ON_ROSTER")
    assert result["ok"] is False
    assert "error" in result
