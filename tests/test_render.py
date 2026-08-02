from views.render import render_status, render_map
from xsettlers_mcp.tools.organization_tools import (
    show_civilization_status, show_game_status, show_organization
)
from xsettlers_mcp.tools.sector_tools import show_sector_neighborhood
from tests.conftest import (
    seed_player, seed_sector, seed_ship, seed_pod, seed_player_sector
)

def test_render_status_civilization_status_is_hint_driven():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    status = show_civilization_status("U_P1")
    text = render_status(status)
    assert "Unit" in text                  # column_labels overrides short_name -> "Unit"
    assert "Cargo" in text                 # ...and cargo_display -> "Cargo"
    assert status["organizations"][0]["short_name"] in text
    assert status["organizations"][0]["cargo_display"] in text

def test_render_status_game_status_uses_standings_rows_key():
    seed_player()
    status = show_game_status("U_P1")
    text = render_status(status)
    assert "rank" in text
    assert "Player One" in text            # default seed_player display_name

def test_render_status_game_status_drops_decimals_and_utilization():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    status = show_game_status("U_P1")
    text = render_status(status)
    assert "utilization" not in text
    header_line = text.splitlines()[2]
    assert header_line == "| rank | Player | score | energy | food | goods |"
    row_line = next(l for l in text.splitlines() if l.startswith("| 1 |"))
    assert ".0" not in row_line              # whole numbers, no trailing decimal

def test_render_status_show_organization_includes_header_line():
    pid = seed_player(); sid = seed_sector(3, 3, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    status = show_organization("U_P1", oid)
    text = render_status(status)
    assert text.startswith(f"**{status['display']['header']}**")
    assert "Energy" in text                # task_display for produce_energy

def test_render_status_applies_column_labels_override():
    """show_organization's column_labels overrides task_display/capacity_display
    header text to "Task"/"Utilization" while row lookups still key off the
    raw field names."""
    pid = seed_player(); sid = seed_sector(3, 3, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    status = show_organization("U_P1", oid)
    text = render_status(status)
    header_line = text.splitlines()[2]
    assert "| Task |" in header_line
    assert "| Utilization |" in header_line
    assert "task_display" not in header_line
    assert "capacity_display" not in header_line

def test_render_status_columns_without_labels_header_as_field_name():
    """Unrelated tools that never set column_labels keep the old behavior --
    header text is just the field name."""
    data = {
        "widgets": [{"name": "a", "count": 3}],
        "display": {"rows_key": "widgets", "columns": ["name", "count"]},
    }
    text = render_status(data)
    assert "| name | count |" in text

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

# --- map rendering (display.kind == "map") ---

def _neighborhood():
    pid = seed_player(); sid = seed_sector(25, 25, 0); oid = seed_ship(pid, sid)
    seed_player_sector(pid, sid, 100)
    return show_sector_neighborhood("U_P1", org_id=oid)

def test_render_map_draws_a_grid_with_absolute_axis_labels():
    text = render_map(_neighborhood())
    lines = text.splitlines()
    header = next(i for i, l in enumerate(lines) if l.startswith("| y/x"))
    assert lines[header].startswith("| y/x | 20 | 21 |")   # x across, absolute coords
    assert lines[header + 1].startswith("|---|")
    assert "| **25** |" in text                            # y down the side
    assert "S1" in text

def test_render_map_includes_the_legend_so_cells_are_decodable():
    text = render_map(_neighborhood())
    assert "S3 = 3 of your ships" in text
    assert "(blank) = outside range" in text

def test_render_status_dispatches_to_map_on_display_kind():
    """Dispatch is on the shape of the data, not on which tool produced it."""
    data = _neighborhood()
    assert render_status(data) == render_map(data)

def test_render_map_no_special_casing_by_tool_name():
    """Same contract as render_status: an arbitrary dict following the map
    display shape renders with no changes to render_map() itself."""
    data = {"display": {"kind": "map", "header": "Somewhere",
                        "grid": {"corner": "y/x", "x_labels": ["0", "1"],
                                 "rows": [{"label": "0", "cells": ["A", ""]},
                                          {"label": "1", "cells": ["", "B"]}]}}}
    text = render_map(data)
    assert text.startswith("**Somewhere**")
    assert "| y/x | 0 | 1 |" in text
    assert "| **0** | A |  |" in text
    assert "| **1** |  | B |" in text

def test_render_map_notes_off_plane_sectors():
    pid = seed_player(); sid = seed_sector(25, 25, 0); oid = seed_ship(pid, sid)
    seed_player_sector(pid, sid, 100)
    upstairs = seed_sector(25, 25, 1)
    seed_player_sector(pid, upstairs, 100)
    text = render_map(show_sector_neighborhood("U_P1", org_id=oid))
    assert "off this z-plane" in text

def test_render_map_propagates_error():
    assert render_map({"error": "Player not found"}) == "Error: Player not found"
