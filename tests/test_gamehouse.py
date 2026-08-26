"""
GameHouse handoff surface -- see xsettlers_mcp/gamehouse.py. Covers both
directions: the scenario list xsettlers publishes at registration, and the
start_session push GameHouse makes once a lobby closes, carrying the
scenario_key the Person chose at join time. scenario_key=None remains valid
and resolves to Diaspora (config/game0.yaml), which is what every handoff
sent before scenario selection existed.
"""
import json
from db.connection import connection
from xsettlers_mcp.gamehouse import start_session
from xsettlers_mcp.tools.player_tools import get_player_state

def _clear_active_game():
    with connection() as conn:
        conn.execute("DELETE FROM games")
        conn.execute("DELETE FROM game_session")

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

    with connection() as conn:
        players = {r["id"]: r for r in conn.execute("SELECT * FROM players").fetchall()}
    person_row = players[person_entry["xsettlers_player_id"]]
    npc_row = players[npc_entry["xsettlers_player_id"]]
    assert person_row["is_npc"] == 0
    assert person_row["player_token"] == person_entry["player_token"]
    assert npc_row["is_npc"] == 1

    # Home sectors match game0.yaml's own two authored participants,
    # positionally (person is entry 0, npc is entry 1).
    with connection() as conn:
        orgs = conn.execute("""SELECT o.player_id, s.coord_x, s.coord_y, s.coord_z
            FROM organizations o JOIN sectors s ON s.id=o.sector_id
            WHERE o.player_id IN (?,?) LIMIT 2""",
            (person_row["id"], npc_row["id"])).fetchall()
    coords = {(o["coord_x"], o["coord_y"], o["coord_z"]) for o in orgs}
    assert coords <= {(25, 25, 0), (25, 50, 0)}  # game0.yaml's two home_sector values

def test_start_session_bootstraps_ships_and_pods():
    _clear_active_game()
    result = start_session("tok1", [_person(1), _npc("npc-1")])
    person_id = next(p for p in result["players"] if p["kind"] == "person")["xsettlers_player_id"]
    with connection() as conn:
        ships = conn.execute(
            "SELECT COUNT(*) AS n FROM organizations WHERE player_id=? AND org_type='ship'",
            (person_id,)).fetchone()
        pods = conn.execute("""SELECT COUNT(*) AS n FROM pods p
            JOIN organizations o ON o.id=p.org_id WHERE o.player_id=?""", (person_id,)).fetchone()
    assert ships["n"] == 8  # game0.yaml's ships_per_player
    assert pods["n"] == 8 * 6  # 6 pod templates per ship

def test_start_session_assigns_npc_profile_with_config():
    _clear_active_game()
    result = start_session("tok1", [_person(1), _npc("npc-1", strategy="fan_out",
                                                       config={"jump_range_per_turn": 2})])
    npc_id = next(p for p in result["players"] if p["kind"] == "npc")["xsettlers_player_id"]
    with connection() as conn:
        profile = conn.execute("SELECT strategy_name, config FROM npc_profiles WHERE player_id=?",
                               (npc_id,)).fetchone()
    import json
    assert profile["strategy_name"] == "fan_out"
    assert json.loads(profile["config"]) == {"jump_range_per_turn": 2}

def test_start_session_stores_the_session_token():
    _clear_active_game()
    start_session("tok-xyz", [_person(1), _npc("npc-1")])
    with connection() as conn:
        row = conn.execute("SELECT session_token FROM game_session WHERE id=1").fetchone()
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

    with connection() as conn:
        orgs = conn.execute("""SELECT mission, sector_id, scan_offset_x, scan_offset_y
                               FROM organizations WHERE player_id=? AND org_type='ship'""",
                            (npc_id,)).fetchall()
        memory = json.loads(conn.execute(
            "SELECT memory FROM npc_profiles WHERE player_id=?", (npc_id,)).fetchone()["memory"])

    # fan_out's first two steps ran; the third is a decide step still waiting
    # on its scans, which is where the program counter should be parked.
    assert memory["pc"] == 2
    assert "waiting" in memory
    assert all(o["mission"] == "move" and o["sector_id"] == -1 for o in orgs), \
        "every ship scattered"
    assert all(o["scan_offset_x"] is not None or o["scan_offset_y"] is not None
               for o in orgs), "every ship aimed its scanner"


# --- results hand-back ---
#
# The other direction of the wire: what xsettlers sends GameHouse when a game
# finishes. The score object separates an ENVELOPE every game guarantees
# (placement, score) from a payload GameHouse stores raw and never reads.

def _finish_game():
    """Play to the turn limit so game.final_scores is recorded."""
    from engine.turn import end_of_turn, is_game_over
    guard = 0
    while not is_game_over() and guard < 200:
        end_of_turn(); guard += 1

def test_start_session_stamps_the_gamehouse_person_id_on_person_seats_only():
    """`gamehouse_person_id IS NOT NULL` is what the hand-back filters on, so
    it has to mean exactly 'GameHouse has a Person for this seat'."""
    _clear_active_game()
    start_session("tok1", [_person(42), _npc("npc-1")])
    with connection() as conn:
        rows = {r["email"]: r["gamehouse_person_id"] for r in conn.execute(
            "SELECT email, gamehouse_person_id FROM players").fetchall()}
    assert rows["gamehouse-42@handoff"] == 42
    assert rows["gamehouse-npc-1@handoff"] is None, "an NPC has no Person to credit"

def test_build_results_carries_the_envelope_keyed_by_gamehouse_person_id():
    from xsettlers_mcp.gamehouse import build_results, PLACEMENT_FIELD, SCORE_FIELD
    _clear_active_game()
    start_session("tok1", [_person(42), _npc("npc-1")])
    _finish_game()

    results = build_results()
    assert len(results) == 1, "only the person-backed player is reported"
    entry = results[0]
    assert entry["player_id"] == 42, "keyed by GameHouse's own person.id"
    assert isinstance(entry["score"][PLACEMENT_FIELD], int)
    assert entry["score"][PLACEMENT_FIELD] >= 1
    assert isinstance(entry["score"][SCORE_FIELD], (int, float))
    # Payload rides along; GameHouse stores it and never reads it.
    assert "energy" in entry["score"] and "display_name" in entry["score"]

def test_build_results_matches_the_recorded_scoreboard_not_a_recomputation():
    """What GameHouse is told has to be what happened at the whistle."""
    from engine.turn import get_final_scores
    from xsettlers_mcp.gamehouse import build_results, PLACEMENT_FIELD, SCORE_FIELD
    _clear_active_game()
    start_session("tok1", [_person(42), _npc("npc-1")])
    _finish_game()

    recorded = {s["player_id"]: s for s in get_final_scores()["standings"]}
    with connection() as conn:
        xs_id = conn.execute("SELECT id FROM players WHERE gamehouse_person_id=42").fetchone()["id"]
    entry = build_results()[0]
    assert entry["score"][SCORE_FIELD] == recorded[xs_id]["score"]
    assert entry["score"][PLACEMENT_FIELD] == recorded[xs_id]["rank"]

def test_build_results_is_empty_before_the_game_ends():
    from xsettlers_mcp.gamehouse import build_results
    _clear_active_game()
    start_session("tok1", [_person(42), _npc("npc-1")])
    assert build_results() == []

def test_report_results_skips_when_there_is_no_gamehouse_session():
    """The guard that keeps this mechanism out of ../xsettlers-designer: a
    game bootstrapped without start_session has nobody to report to. A data
    condition, not a mode flag -- designer sets nothing and imports nothing."""
    import asyncio
    from xsettlers_mcp.gamehouse import report_results
    _clear_active_game()
    outcome = asyncio.run(report_results())
    assert outcome["ok"] is False
    assert "no GameHouse session" in outcome["skipped"]

def test_report_results_skips_while_the_game_is_still_running():
    import asyncio
    from xsettlers_mcp.gamehouse import report_results
    _clear_active_game()
    start_session("tok1", [_person(42), _npc("npc-1")])
    outcome = asyncio.run(report_results())
    assert outcome["ok"] is False and "not over" in outcome["skipped"]

def test_an_all_npc_game_records_the_event_without_calling_gamehouse():
    """Nothing to report is a resolved state, not a permanent retry: without
    the event, the reporter would poll this game forever."""
    import asyncio
    from xsettlers_mcp.gamehouse import report_results, RESULTS_REPORTED_EVENT
    _clear_active_game()
    start_session("tok1", [_npc("npc-1"), _npc("npc-2")])
    _finish_game()

    outcome = asyncio.run(report_results())
    assert outcome == {"ok": True, "reported": 0}
    with connection() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE event_type=?",
                         (RESULTS_REPORTED_EVENT,)).fetchone()["n"]
    assert n == 1

def test_results_are_reported_only_once():
    import asyncio
    from xsettlers_mcp.gamehouse import report_results
    _clear_active_game()
    start_session("tok1", [_npc("npc-1"), _npc("npc-2")])
    _finish_game()
    assert asyncio.run(report_results())["ok"] is True
    again = asyncio.run(report_results())
    assert again["ok"] is False and "already reported" in again["skipped"]

# --- archiving on settle ---
#
# Once a finished game is fully settled -- reported to GameHouse if it needed
# to be, nothing pending -- the live database is archived so a running server
# can accept the next game without a restart. archive_active_database() itself
# is tested in tests/test_db_archive.py; what belongs here is the gate that
# decides WHEN it's safe to call it.

def test_game_settled_is_false_while_the_game_is_running():
    from xsettlers_mcp.gamehouse import _game_settled
    _clear_active_game()
    assert _game_settled() is False

def test_game_settled_is_true_immediately_for_a_non_gamehouse_game():
    """A plain (select_scenario-style) game has no session_token, so there is
    nothing to wait for -- settled the instant it ends."""
    from xsettlers_mcp.gamehouse import _game_settled
    from tests.conftest import seed_player
    # fresh_db already seeds an active (non-GameHouse) game -- clearing it
    # would make end_of_turn() no-op forever, per CLAUDE.md.
    seed_player()
    _finish_game()
    assert _game_settled() is True

def test_game_settled_waits_for_the_gamehouse_handback():
    import asyncio
    from xsettlers_mcp.gamehouse import _game_settled, report_results
    _clear_active_game()
    start_session("tok1", [_npc("npc-1"), _npc("npc-2")])
    _finish_game()
    assert _game_settled() is False, "results haven't been reported yet"
    asyncio.run(report_results())
    assert _game_settled() is True

def test_reporter_tick_archives_only_once_settled():
    import asyncio, os
    from xsettlers_mcp.gamehouse import _reporter_tick
    _clear_active_game()
    start_session("tok1", [_npc("npc-1"), _npc("npc-2")])
    db_path = os.environ["DB_PATH"]

    asyncio.run(_reporter_tick())  # game still running: no report, no archive
    assert os.path.exists(db_path)
    assert not any(f.startswith(os.path.basename(db_path) + ".finished-")
                   for f in os.listdir(os.path.dirname(db_path) or "."))

    _finish_game()
    asyncio.run(_reporter_tick())  # reports to GameHouse (all-NPC, so a no-op
                                    # hand-back) and, now settled, archives
    assert any(f.startswith(os.path.basename(db_path) + ".finished-")
               for f in os.listdir(os.path.dirname(db_path) or "."))
    assert os.path.exists(db_path), "a fresh DB must be ready at the live path"

def test_scoreboard_schema_declares_the_envelope_and_its_direction():
    from xsettlers_mcp.gamehouse import scoreboard_schema, PLACEMENT_FIELD, SCORE_FIELD
    schema = scoreboard_schema()
    assert set(schema["required"]) == {PLACEMENT_FIELD, SCORE_FIELD}
    # placement is direction-free (1 is best either way); score is not, which
    # is the whole reason direction is declared.
    assert schema["direction"] == "higher_is_better"

# --- scenario selection: GameHouse picks, xsettlers bootstraps ---

def test_registrable_scenarios_omits_a_differently_sized_lobby():
    """Registration carries one lobby shape for the whole game, so a scenario
    sized differently cannot be offered without being mis-lobbied. game_solo
    is 1 player on a 0s wait window against Diaspora's 2 and 120s."""
    from xsettlers_mcp.gamehouse import registrable_scenarios
    from config.loader import load_starting_configuration
    keys, skipped = registrable_scenarios(load_starting_configuration("config/game0.yaml").lobby)
    assert "game0" in keys and "game1" in keys
    assert skipped == ["game_solo"]
    assert "game_solo" not in keys

def test_resolve_scenario_maps_a_key_to_its_file():
    from xsettlers_mcp.gamehouse import resolve_scenario
    assert resolve_scenario("game1") == ("config/game1.yaml", "game1")
    assert resolve_scenario(None) == ("config/game0.yaml", "game0")
    assert resolve_scenario("no-such-scenario") is None

def test_start_session_bootstraps_the_scenario_gamehouse_chose():
    _clear_active_game()
    result = start_session("tok-g1", [_person(1), _npc("npc-1")], scenario_key="game1")
    assert result["ok"] is True
    with connection() as conn:
        row = conn.execute("SELECT scenario_name, scenario_file FROM games WHERE id=1").fetchone()
    assert row["scenario_name"] == "game1"
    assert row["scenario_file"] == "config/game1.yaml"

def test_start_session_seats_players_at_the_chosen_scenarios_home_sectors():
    """Home sectors come from the resolved scenario's own participants, not
    game0's -- the seating has to follow the map actually being played."""
    from config.loader import load_starting_configuration
    _clear_active_game()
    start_session("tok-g1", [_person(1), _npc("npc-1")], scenario_key="game1")
    expected = [p.home_sector for p in load_starting_configuration("config/game1.yaml").participants]
    with connection() as conn:
        rows = conn.execute("""SELECT s.coord_x, s.coord_y, s.coord_z FROM players p
            JOIN organizations o ON o.player_id = p.id
            JOIN sectors s ON s.id = o.sector_id
            GROUP BY p.id ORDER BY p.id""").fetchall()
    seated = [(r["coord_x"], r["coord_y"], r["coord_z"]) for r in rows]
    assert seated == [tuple(h) for h in expected]

def test_start_session_rejects_an_unknown_scenario_key():
    _clear_active_game()
    result = start_session("tok-x", [_person(1), _npc("npc-1")], scenario_key="not-a-scenario")
    assert "error" in result and "not-a-scenario" in result["error"]
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM games").fetchone()["n"] == 0
