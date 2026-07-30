from views.render import render_status
from xsettlers_mcp.tools.organization_tools import (
    show_civilization_status, show_game_status, show_organization
)
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod

def test_render_status_civilization_status_is_hint_driven():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, mission="produce_energy", storage_current=10.0)
    status = show_civilization_status("U_P1")
    text = render_status(status)
    assert "short_name" in text            # header row uses the API's own column names
    assert "cargo_display" in text
    assert status["organizations"][0]["short_name"] in text
    assert status["organizations"][0]["cargo_display"] in text

def test_render_status_game_status_uses_standings_rows_key():
    seed_player()
    status = show_game_status("U_P1")
    text = render_status(status)
    assert "rank" in text
    assert "Player One" in text            # default seed_player display_name

def test_render_status_show_organization_includes_header_line():
    pid = seed_player(); sid = seed_sector(3, 3, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, mission="produce_energy", storage_current=10.0)
    status = show_organization("U_P1", oid)
    text = render_status(status)
    assert text.startswith(f"**{status['display']['header']}**")
    assert "Energy" in text                # task_display for produce_energy

def test_render_status_no_special_casing_by_tool_name():
    """The whole point: an arbitrary dict following the same display-hints
    shape renders correctly with no changes to render_status() itself."""
    data = {
        "widgets": [{"name": "a", "count": 3}, {"name": "b", "count": 5}],
        "display": {"rows_key": "widgets", "columns": ["name", "count"]},
    }
    text = render_status(data)
    assert "| a | 3 |" in text
    assert "| b | 5 |" in text

def test_render_status_propagates_error():
    assert render_status({"error": "Player not found"}) == "Error: Player not found"
