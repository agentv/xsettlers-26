"""
There's no separate xsettlers_mcp/gateway.py module. select_scenario() is the one
explicit gate, and every gameplay tool already checks player existence
internally -- before a scenario is selected, players is empty, so those checks
reject on their own. These tests verify that combination actually gates access
end-to-end, which is what a dedicated gateway module's tests would otherwise
cover.

The per-tool check is the @player_tool decorator
(xsettlers_mcp/tools/session.py); the gate is per-tool, and these tests are
what say so.
"""
from db.connection import get_connection
from xsettlers_mcp.game_select import select_scenario
from xsettlers_mcp.tools.organization_reports import show_civilization_status

def _clear_active_game():
    conn = get_connection()
    conn.execute("DELETE FROM games")
    conn.commit(); conn.close()

def test_gameplay_blocked_before_any_scenario_selected():
    _clear_active_game()
    result = show_civilization_status("REPLACE_WITH_GENERATED_TOKEN_1")
    assert "error" in result

def test_gameplay_blocked_for_unrecognized_player_even_after_selection():
    _clear_active_game()
    select_scenario("REPLACE_WITH_GENERATED_TOKEN_1", "game0")
    result = show_civilization_status("U_TOTAL_STRANGER")
    assert "error" in result

def test_gameplay_works_for_roster_player_after_selection():
    _clear_active_game()
    select_scenario("REPLACE_WITH_GENERATED_TOKEN_1", "game0")
    result = show_civilization_status("REPLACE_WITH_GENERATED_TOKEN_1")
    assert "error" not in result
    assert result["turn"] == 0


# --- authentication happens before anything else ----------------------------

def _stranger_sees_only(result):
    """A stranger must learn nothing but that they aren't known."""
    return result == {"error": "Player not found"}


def test_argument_validation_never_runs_before_authentication():
    """
    A tool that validates its arguments BEFORE checking who is asking tells
    an unrecognized caller things they have no business learning: that the
    neighborhood radius caps at 10, that a name may not be empty, and -- most
    usefully to someone mapping the API -- the complete list of valid mission
    names.

    game_select.select_scenario already worked the other way round on purpose
    ("Identity is checked first so an unrecognized token learns nothing about
    which scenarios exist"); routing every tool through one decorator is what
    finally made the gameplay tools agree with it, since the body cannot run
    at all until the token resolves.
    """
    from xsettlers_mcp.tools.sector_tools import show_sector_neighborhood
    from xsettlers_mcp.tools.organization_tools import set_mission, rename_organization

    assert _stranger_sees_only(show_sector_neighborhood("U_STRANGER", org_id=1, radius=99))
    assert _stranger_sees_only(rename_organization("U_STRANGER", 1, "   "))
    invalid_mission = set_mission("U_STRANGER", 1, "dance")
    assert _stranger_sees_only(invalid_mission)
    assert "colonize" not in str(invalid_mission), "must not enumerate valid missions"


def test_every_player_facing_tool_rejects_an_unknown_token():
    """
    The guarantee is only worth as much as its coverage, so this asserts it
    over the whole surface rather than a sample: no tool may answer anything
    but "Player not found" to a token that isn't in the game.

    declare_end_turn is the reason this test is written as a sweep. It was the
    single tool that skipped the check, answering {"declared": True} to a
    stranger and then calling check_consensus_acceleration(), which can end
    the turn for every real player. A spot-check would have missed it.
    """
    from xsettlers_mcp.tools import (player_tools, sector_tools,
                                     navigation_tools, organization_tools,
                                     organization_reports)
    S = "U_TOTAL_STRANGER"
    calls = [
        lambda: player_tools.get_player_state(S),
        lambda: player_tools.declare_end_turn(S),
        lambda: player_tools.rescind_end_turn(S),
        lambda: player_tools.set_display_name(S, "Nova"),
        lambda: sector_tools.get_sector(S, 1),
        lambda: sector_tools.get_sector_map(S),
        lambda: sector_tools.show_sector_neighborhood(S, org_id=1),
        lambda: sector_tools.show_neighborhood_resources(S, org_id=1),
        lambda: navigation_tools.preview_move(S, 1, 1, 1, 0),
        lambda: navigation_tools.confirm_move(S, 1, 1, 1, 0),
        lambda: navigation_tools.cancel_move(S, 1),
        lambda: organization_tools.set_mission(S, 1, "idle"),
        lambda: organization_tools.set_pod_task(S, 1, "idle"),
        lambda: organization_tools.set_pod_scan_bearing(S, 1, bearing="N"),
        lambda: organization_tools.set_org_scan_bearing(S, 1, bearing="N"),
        lambda: organization_tools.rename_organization(S, 1, "Nope"),
        lambda: organization_tools.queue_command(S, 1, "at_turn", "set_pod_task",
                                                 {"pod_id": 1, "task": "idle"}, turn=1),
        lambda: organization_reports.show_organization(S, 1),
        lambda: organization_reports.show_civilization_status(S),
        lambda: organization_reports.show_game_status(S),
    ]
    _clear_active_game()
    select_scenario("REPLACE_WITH_GENERATED_TOKEN_1", "game0")   # a real game exists
    for call in calls:
        assert _stranger_sees_only(call()), call


def test_declare_end_turn_cannot_be_triggered_by_a_stranger():
    """The concrete harm behind the sweep above: a stranger must not be able
    to reach check_consensus_acceleration() and end everyone's turn."""
    from xsettlers_mcp.tools.player_tools import declare_end_turn
    _clear_active_game()
    select_scenario("REPLACE_WITH_GENERATED_TOKEN_1", "game0")
    conn = get_connection()
    before = conn.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()[0]
    conn.close()
    assert _stranger_sees_only(declare_end_turn("U_TOTAL_STRANGER"))
    conn = get_connection()
    after = conn.execute("SELECT current_turn FROM game_state WHERE id=1").fetchone()[0]
    declared = conn.execute("SELECT COUNT(*) FROM players WHERE end_turn_declared=1").fetchone()[0]
    conn.close()
    assert after == before, "a stranger must not advance the turn"
    assert declared == 0, "a stranger must not mark anyone as having declared"
