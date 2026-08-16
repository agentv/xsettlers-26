"""
GameHouse handoff surface -- see xsettlers_mcp/gamehouse.py. Scoped to
Diaspora (config/game0.yaml) only for v1, registered with GameHouse as a
scenario-less game -- start_session's scenario_key is therefore always None
in real traffic today, but accepted so a real call from GameHouse's
push_start_session (which always sends the field) doesn't crash on an
unexpected keyword argument.
"""
import json
from db.connection import get_connection
from xsettlers_mcp.gamehouse import start_session
from xsettlers_mcp.tools.player_tools import get_player_state

def _clear_active_game():
    conn = get_connection()
    conn.execute("DELETE FROM games")
    conn.execute("DELETE FROM game_session")
    conn.commit(); conn.close()

def _person(pid):
    return {"player_id": pid, "kind": "person"}

def _npc(label, strategy="turtle", config=None):
    profile = {"strategy_ref": strategy}
    if config is not None:
        profile["config"] = config
    return {"player_id": label, "kind": "npc", "profile": profile}

# --- start_session: validation ---

def test_start_session_rejects_wrong_player_count():
    _clear_active_game()
    result = start_session("tok1", [_person(1)])  # game0 requires exactly 2
    assert "error" in result

def test_start_session_rejects_unknown_kind():
    _clear_active_game()
    result = start_session("tok1", [_person(1), {"player_id": 2, "kind": "robot"}])
    assert "error" in result

def test_start_session_rejects_npc_with_unregistered_strategy():
    _clear_active_game()
    result = start_session("tok1", [_person(1), _npc("npc-1", strategy="does_not_exist")])
    assert "error" in result

def test_start_session_rejects_empty_players_list():
    _clear_active_game()
    result = start_session("tok1", [])
    assert "error" in result

def test_start_session_accepts_scenario_key_without_crashing():
    """push_start_session always sends scenario_key, so a signature missing
    this kwarg would TypeError instead of returning cleanly."""
    _clear_active_game()
    result = start_session("tok1", [_person(1), _npc("npc-1")], scenario_key=None)
    assert result["ok"] is True

def test_start_session_tool_schema_permits_null_scenario_key():
    """The declared inputSchema must admit null. Declaring scenario_key as
    "type":"string" makes JSON Schema reject None -- so a real call from
    GameHouse (which always sends scenario_key, null for a scenario-less
    game) fails the MCP SDK's jsonschema.validate() before xsettlers' own
    Python code, which handles None fine, ever runs.
    Calling start_session() directly, or even through call_tool() as a raw
    Python function, doesn't exercise that validation layer at all -- only
    checking the schema declaration itself catches this class of bug.
    """
    import asyncio
    import jsonschema
    from xsettlers_mcp.server import list_tools
    tools = asyncio.run(list_tools())
    schema = next(t.inputSchema for t in tools if t.name == "start_session")
    jsonschema.validate(
        {"session_token": "tok1", "players": [], "scenario_key": None}, schema)

# --- start_session: successful handoff ---

def test_start_session_seats_a_person_and_an_npc():
    _clear_active_game()
    result = start_session("tok1", [_person(42), _npc("npc-1", strategy="fan_out",
                                                       config={"scout_distance": 2})])
    assert result["ok"] is True
    assert result["already_active"] is False
    assert result["scenario_name"] == "game0"
    assert len(result["players"]) == 2

    person_entry = next(p for p in result["players"] if p["kind"] == "person")
    npc_entry = next(p for p in result["players"] if p["kind"] == "npc")
    assert person_entry["player_id"] == 42
    assert "player_token" in person_entry
    assert "player_token" not in npc_entry  # never returned for NPCs

    conn = get_connection()
    players = {r["id"]: r for r in conn.execute("SELECT * FROM players").fetchall()}
    conn.close()
    person_row = players[person_entry["xsettlers_player_id"]]
    npc_row = players[npc_entry["xsettlers_player_id"]]
    assert person_row["is_npc"] == 0
    assert person_row["player_token"] == person_entry["player_token"]
    assert npc_row["is_npc"] == 1

    # Home sectors match game0.yaml's own two authored participants,
    # positionally (person is entry 0, npc is entry 1).
    conn = get_connection()
    orgs = conn.execute("""SELECT o.player_id, s.coord_x, s.coord_y, s.coord_z
        FROM organizations o JOIN sectors s ON s.id=o.sector_id
        WHERE o.player_id IN (?,?) LIMIT 2""",
        (person_row["id"], npc_row["id"])).fetchall()
    conn.close()
    coords = {(o["coord_x"], o["coord_y"], o["coord_z"]) for o in orgs}
    assert coords <= {(25, 25, 0), (25, 50, 0)}  # game0.yaml's two home_sector values

def test_start_session_bootstraps_ships_and_pods():
    _clear_active_game()
    result = start_session("tok1", [_person(1), _npc("npc-1")])
    person_id = next(p for p in result["players"] if p["kind"] == "person")["xsettlers_player_id"]
    conn = get_connection()
    ships = conn.execute(
        "SELECT COUNT(*) AS n FROM organizations WHERE player_id=? AND org_type='ship'",
        (person_id,)).fetchone()
    pods = conn.execute("""SELECT COUNT(*) AS n FROM pods p
        JOIN organizations o ON o.id=p.org_id WHERE o.player_id=?""", (person_id,)).fetchone()
    conn.close()
    assert ships["n"] == 8  # game0.yaml's ships_per_player
    assert pods["n"] == 8 * 6  # 6 pod templates per ship

def test_start_session_assigns_npc_profile_with_config():
    _clear_active_game()
    result = start_session("tok1", [_person(1), _npc("npc-1", strategy="fan_out",
                                                       config={"jump_range_per_turn": 2})])
    npc_id = next(p for p in result["players"] if p["kind"] == "npc")["xsettlers_player_id"]
    conn = get_connection()
    profile = conn.execute("SELECT strategy_name, config FROM npc_profiles WHERE player_id=?",
                           (npc_id,)).fetchone()
    conn.close()
    import json
    assert profile["strategy_name"] == "fan_out"
    assert json.loads(profile["config"]) == {"jump_range_per_turn": 2}

def test_start_session_stores_the_session_token():
    _clear_active_game()
    start_session("tok-xyz", [_person(1), _npc("npc-1")])
    conn = get_connection()
    row = conn.execute("SELECT session_token FROM game_session WHERE id=1").fetchone()
    conn.close()
    assert row["session_token"] == "tok-xyz"

def test_returned_player_token_actually_works_against_gameplay_tools():
    """The concrete proof this is wired into the existing, untouched
    player_token auth path -- not just DB rows that happen to look right."""
    _clear_active_game()
    result = start_session("tok1", [_person(1), _npc("npc-1")])
    person_token = next(p for p in result["players"] if p["kind"] == "person")["player_token"]
    state = get_player_state(person_token)
    assert "error" not in state
    assert len(state["organizations"]) == 8

# --- start_session: idempotency and conflict ---

def test_start_session_is_idempotent_for_the_same_token():
    _clear_active_game()
    start_session("tok1", [_person(1), _npc("npc-1")])
    result = start_session("tok1", [_person(1), _npc("npc-1")])
    assert result == {"ok": True, "already_active": True, "scenario_name": "game0"}

def test_start_session_rejects_a_different_token_while_active():
    _clear_active_game()
    start_session("tok1", [_person(1), _npc("npc-1")])
    result = start_session("tok2", [_person(99), _npc("npc-2")])
    assert "error" in result


def test_a_referenced_npc_seated_here_actually_plays():
    """The handoff's end of the strategy_ref contract: a roster naming a
    strategy must seat an NPC that plays it under a real end_of_turn() loop,
    with nothing calling the strategy by hand."""
    from engine.turn import end_of_turn
    _clear_active_game()
    result = start_session("tok1", [_person(42), _npc("npc-1", strategy="fan_out")])
    assert result["ok"] is True
    npc_id = next(p for p in result["players"] if p["kind"] == "npc")["xsettlers_player_id"]

    end_of_turn()  # the only thing driving the NPC

    conn = get_connection()
    orgs = conn.execute("""SELECT mission, sector_id, scan_offset_x, scan_offset_y
                           FROM organizations WHERE player_id=? AND org_type='ship'""",
                        (npc_id,)).fetchall()
    memory = json.loads(conn.execute(
        "SELECT memory FROM npc_profiles WHERE player_id=?", (npc_id,)).fetchone()["memory"])
    conn.close()

    # fan_out's first two steps ran; the third is a decide step still waiting
    # on its scans, which is where the program counter should be parked.
    assert memory["pc"] == 2
    assert "waiting" in memory
    assert all(o["mission"] == "move" and o["sector_id"] == -1 for o in orgs), \
        "every ship scattered"
    assert all(o["scan_offset_x"] is not None or o["scan_offset_y"] is not None
               for o in orgs), "every ship aimed its scanner"
