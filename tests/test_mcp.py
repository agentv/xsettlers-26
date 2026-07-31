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

# --- MCP dispatch serializes as JSON (see xsettlers_mcp/server.py) ---

def test_tool_responses_are_valid_json_not_python_repr():
    """These used to go over the wire as str(result) -- Python repr, with
    single quotes and True/None. An LLM client copes; anything that parses the
    payload cannot. Pinned because the failure is silent: repr looks fine in a
    log and only breaks at the client."""
    import json
    from xsettlers_mcp.server import _as_json
    text = _as_json(get_player_state("U_TEST_001"))
    parsed = json.loads(text)                       # the whole point
    assert parsed["player"]["email"] == "p1@test.com"
    assert "'" not in text.split('"email"')[0]      # no repr quoting

def test_json_serialization_survives_a_non_primitive_value():
    """default=str is a backstop: a response that fails to serialize would
    fail the whole call, which is worse than losing type fidelity."""
    import json, datetime
    from xsettlers_mcp.server import _as_json
    parsed = json.loads(_as_json({"when": datetime.date(2026, 7, 31)}))
    assert parsed["when"] == "2026-07-31"

def test_booleans_and_nulls_serialize_as_json_literals():
    import json
    from xsettlers_mcp.server import _as_json
    text = _as_json({"ok": True, "winner": None})
    assert '"ok": true' in text and '"winner": null' in text
    assert json.loads(text) == {"ok": True, "winner": None}
