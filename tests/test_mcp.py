from db.connection import connection, get_connection
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
    with connection() as conn:
        assert conn.execute("SELECT end_turn_declared FROM players WHERE player_token='U_TEST_001'"
                            ).fetchone()[0] == 1
    rescind_end_turn("U_TEST_001")
    with connection() as conn:
        assert conn.execute("SELECT end_turn_declared FROM players WHERE player_token='U_TEST_001'"
                            ).fetchone()[0] == 0

# --- MCP dispatch serializes as JSON (see xsettlers_mcp/server.py) ---

def test_tool_responses_are_valid_json_not_python_repr():
    """Tool results must be JSON, not str(result) -- Python repr, with single
    quotes and True/None. An LLM client copes with repr; anything that parses
    the payload cannot. Pinned because the failure is silent: repr looks fine
    in a log and only breaks at the client."""
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

# --- default-display steering (see xsettlers_mcp/server.py SERVER_INSTRUCTIONS/RENDER_DIRECTIVE) ---

def test_server_sends_instructions_at_initialize():
    # Sent once, before a client sees any tool response -- the strongest
    # available lever for steering how an LLM client displays returned
    # content, short of controlling the client itself.
    from xsettlers_mcp.server import app, SERVER_INSTRUCTIONS
    assert app.instructions == SERVER_INSTRUCTIONS
    assert "VERBATIM" in SERVER_INSTRUCTIONS

# --- response_format toggle (see xsettlers_mcp/server.py call_tool) ---

def test_as_markdown_renders_a_display_hinted_dict():
    from xsettlers_mcp.server import _as_markdown
    data = {"widgets": [{"name": "a"}], "display": {"rows_key": "widgets", "columns": ["name"]}}
    assert "| a    |" in _as_markdown(data)    # padded to the "name" header

def test_as_markdown_falls_back_for_non_dict_results():
    from xsettlers_mcp.server import _as_markdown
    assert _as_markdown(["not", "a", "dict"]) == "(no table view for this tool's response shape)"

def test_call_tool_default_markdown_view_returns_json_and_markdown():
    import asyncio, json
    from xsettlers_mcp.server import call_tool, RENDER_DIRECTIVE
    content = asyncio.run(call_tool("get_player_state", {"player_token": "U_TEST_001"}))
    assert len(content) == 3
    parsed = json.loads(content[0].text)
    assert parsed["player"]["email"] == "p1@test.com"
    # get_player_state has no `display` block (yet), so render_status's
    # no-rows fallback is the correct markdown output here, not a crash.
    assert content[1].text == "(no rows)"
    # Third block reinforces SERVER_INSTRUCTIONS on every call: render the
    # markdown verbatim, don't reconstruct a display from the JSON instead.
    assert content[2].text == RENDER_DIRECTIVE

def test_call_tool_data_only_returns_json_alone():
    import asyncio, json
    from xsettlers_mcp.server import call_tool
    content = asyncio.run(call_tool("get_player_state",
                                     {"player_token": "U_TEST_001", "response_format": "data_only"}))
    assert len(content) == 1
    assert json.loads(content[0].text)["player"]["email"] == "p1@test.com"

def test_call_tool_response_format_never_reaches_the_tool_function():
    import asyncio
    from xsettlers_mcp.server import call_tool
    # get_player_state(player_token) takes no response_format kwarg -- a TypeError
    # here means the pop in call_tool() failed to strip it before dispatch.
    content = asyncio.run(call_tool("get_player_state",
                                     {"player_token": "U_TEST_001", "response_format": "data_only"}))
    assert len(content) == 1

def test_call_tool_html_svg_falls_back_to_default_json_and_markdown():
    # html_svg is reserved for a future rendered-graphics response and isn't
    # built yet -- until it exists it's treated the same as markdown_view
    # (JSON + markdown table), not given special handling.
    import asyncio, json
    from xsettlers_mcp.server import call_tool
    content = asyncio.run(call_tool("get_player_state",
                                     {"player_token": "U_TEST_001", "response_format": "html_svg"}))
    assert len(content) == 3
    assert json.loads(content[0].text)["player"]["email"] == "p1@test.com"
    assert content[1].text == "(no rows)"
