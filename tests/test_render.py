import re

from views.render import render_status, render_map
from xsettlers_mcp.tools.organization_reports import (
    show_civilization_status, show_game_status, show_organization
)
from xsettlers_mcp.tools.organization_tools import set_org_scan_bearing
from xsettlers_mcp.tools.navigation_tools import confirm_move
from xsettlers_mcp.tools.sector_tools import (show_sector_neighborhood,
                                             show_neighborhood_resources,
                                             CELL_WIDTH)
from tests.conftest import (
    seed_player, seed_sector, seed_ship, seed_pod, seed_player_sector
)

def _squash(text: str) -> str:
    """Collapse the alignment padding out of a rendered table.

    Most assertions below are about what a row says, not how it lines up;
    alignment has tests of its own (see the padding section at the end), and
    embedding column widths in every other assertion would make them fail
    whenever an unrelated cell grows a character."""
    return re.sub(r" +", " ", text)


def test_render_status_civilization_status_is_hint_driven():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    status = show_civilization_status("U_P1")
    text = render_status(status)
    assert "Unit" in text                  # column_labels overrides short_name -> "Unit"
    assert "Cargo" in text                 # ...and cargo_display -> "Cargo"
    assert status["organizations"][0]["short_name"] in text
    assert status["organizations"][0]["cargo_display"] in text

def test_render_status_fleet_status_opens_with_turn_and_countdown():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    text = render_status(show_civilization_status("U_P1"))
    assert text.splitlines()[0].startswith("**Turn 0 of ")
    assert "(--:--)" in text.splitlines()[0]   # no clock running under test

def test_render_status_fleet_status_headers_stack_the_resource_order():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    text = render_status(show_civilization_status("U_P1"))
    header_line = next(l for l in _squash(text).splitlines() if l.startswith("| Unit |"))
    assert header_line == ("| Unit | Location | Cargo | Storage<br>E/F/G | "
                           "Tasking<br>E/F/G/S/I | Production/Turn<br>E/F/G |")

def test_render_status_fleet_status_cells_are_bare_slashed_runs():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    text = render_status(show_civilization_status("U_P1"))
    row = next(l for l in text.splitlines() if l.startswith("| Test Ship |"))
    assert "10/0/0" in row                 # one energy pod holding 10
    assert "E:" not in row                 # no per-cell resource labels left

def test_render_status_tasking_counts_scan_and_idle_pods():
    """Every pod gets a slot -- a scanning or idle pod is still crew the
    player paid for, and a fixed-width cell that omits them under-counts."""
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy"); seed_pod(oid, task="produce_energy")
    seed_pod(oid, task="produce_food")
    seed_pod(oid, task="scan")
    seed_pod(oid, task="idle"); seed_pod(oid, task="idle")
    status = show_civilization_status("U_P1")
    assert status["organizations"][0]["tasking_summary"] == "2/1/0/1/2"
    row = next(l for l in _squash(render_status(status)).splitlines()
               if l.startswith("| Test Ship |"))
    assert "| 2/1/0/1/2 |" in row

def test_render_status_fleet_totals_render_below_the_table():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_capacity=100.0, storage_current=10.0)
    lines = render_status(show_civilization_status("U_P1")).splitlines()
    assert lines.index("**Fleet totals**") > lines.index(
        next(l for l in lines if l.startswith("| Test Ship |")))
    assert "- Storage (E/F/G): 10/0/0" in lines
    assert "- Capacity: 10 / 100 (10% full)" in lines

def test_render_status_fleet_status_shows_in_transit_not_the_sentinel():
    """A ship parked at the (-1,-1,-1) sentinel reads "in transit" -- the
    sentinel coordinates never reach the player."""
    pid = seed_player(); origin = seed_sector(0, 0, 0)
    oid = seed_ship(pid, origin)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    confirm_move("U_P1", oid, 3, 0, 0)
    status = show_civilization_status("U_P1")
    assert status["organizations"][0]["in_transit"] is True
    text = render_status(status)
    row = next(l for l in text.splitlines() if l.startswith("| Test Ship |"))
    assert "| in transit |" in row
    assert "-1" not in row

def test_render_status_game_status_uses_standings_rows_key():
    seed_player()
    status = show_game_status("U_P1")
    text = render_status(status)
    assert "Rank" in text
    assert "Player One" in text            # default seed_player display_name

def test_render_status_game_status_drops_decimals_and_utilization():
    pid = seed_player(); sid = seed_sector(); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    status = show_game_status("U_P1")
    text = render_status(status)
    assert "utilization" not in text
    header_line = _squash(text).splitlines()[2]
    assert header_line == "| Rank | Player | Score | Energy | Food | Goods |"
    row_line = next(l for l in _squash(text).splitlines() if l.startswith("| 1 |"))
    assert ".0" not in row_line              # whole numbers, no trailing decimal

def test_render_status_show_organization_includes_header_line():
    pid = seed_player(); sid = seed_sector(3, 3, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    status = show_organization("U_P1", oid)
    text = render_status(status)
    assert text.startswith(f"**{status['display']['header']}**")
    assert "Energy" in text                # task_display for produce_energy

def test_render_status_appends_the_scanner_footer_below_the_table():
    pid = seed_player(); sid = seed_sector(3, 3, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    set_org_scan_bearing("U_P1", oid, "NE")
    status = show_organization("U_P1", oid)
    text = render_status(status)
    lines = text.splitlines()
    assert lines[-1] == "Scans: Northeast"
    assert lines[-2] == ""                 # blank line separates footer from table

def test_render_status_omits_footer_section_when_there_is_none():
    pid = seed_player(); sid = seed_sector(3, 3, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    status = show_organization("U_P1", oid)
    text = render_status(status)
    assert "Scans:" not in text

def test_render_status_applies_column_labels_override():
    """show_organization's column_labels overrides task_display/capacity_display
    header text to "Task"/"Cargo" while row lookups still key off the raw
    field names. Every column is labelled, so the whole header is title case
    -- the same convention show_game_status and show_civilization_status
    follow."""
    pid = seed_player(); sid = seed_sector(3, 3, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    status = show_organization("U_P1", oid)
    text = render_status(status)
    header_line = _squash(text).splitlines()[2]
    assert header_line == "| Task | Count | Energy | Food | Goods | Cargo |"
    assert "task_display" not in header_line
    assert "capacity_display" not in header_line

def test_render_status_show_organization_drops_decimals():
    """No action in the game yields a fraction of a resource, so the table
    carries whole numbers -- the raw columns stay floats alongside."""
    pid = seed_player(); sid = seed_sector(3, 3, 0); oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_energy", storage_current=10.0)
    status = show_organization("U_P1", oid)
    assert status["tasks"][0]["energy"] == 10.0        # raw field untouched
    assert status["tasks"][0]["energy_display"] == "10"
    row = next(l for l in render_status(status).splitlines() if l.startswith("| Energy |"))
    assert ".0" not in row

def test_render_status_columns_without_labels_header_as_field_name():
    """Unrelated tools that never set column_labels keep the old behavior --
    header text is just the field name."""
    data = {
        "widgets": [{"name": "a", "count": 3}],
        "display": {"rows_key": "widgets", "columns": ["name", "count"]},
    }
    text = _squash(render_status(data))
    assert "| name | count |" in text

def test_render_status_no_special_casing_by_tool_name():
    """The whole point: an arbitrary dict following the same display-hints
    shape renders correctly with no changes to render_status() itself."""
    data = {
        "widgets": [{"name": "a", "count": 3}, {"name": "b", "count": 5}],
        "display": {"rows_key": "widgets", "columns": ["name", "count"]},
    }
    text = _squash(render_status(data))
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
    text = _squash(render_map(_neighborhood()))
    lines = text.splitlines()
    header = next(i for i, l in enumerate(lines) if l.startswith("| y/x"))
    assert lines[header].startswith("| y/x | 21 | 22 |")   # x across, absolute coords
    assert set(lines[header + 1]) == {"|", "-"}      # separator, whatever the widths
    assert "| **25** |" in text                            # y down the side
    assert "S1" in text

def test_render_map_header_carries_turn_and_countdown():
    """The map opens with the same turn line the status reports do, so two
    reports read side by side cannot disagree about the clock."""
    text = render_map(_neighborhood())
    assert text.splitlines()[0].startswith("**Neighborhood of ")
    assert " — Turn 0 of " in text.splitlines()[0]
    assert "(--:--)**" in text.splitlines()[0]   # no clock running under test

def test_render_map_highlights_table_headers_are_title_case():
    """The highlights table follows the same header convention as the three
    status reports -- every column labelled, no raw field names."""
    text = _squash(render_map(_neighborhood()))
    header_line = next(l for l in text.splitlines() if l.startswith("| Coords |"))
    assert header_line == "| Coords | Ships | Colonies | Rivals/Confidence |"
    assert "coords_display" not in text
    assert "own_ships" not in text

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
    text = _squash(render_map(data))
    assert text.startswith("**Somewhere**")
    assert "| y/x | 0 | 1 |" in text
    assert "| **0** | A | |" in text
    assert "| **1** | | B |" in text

def test_render_map_notes_off_plane_sectors():
    pid = seed_player(); sid = seed_sector(25, 25, 0); oid = seed_ship(pid, sid)
    seed_player_sector(pid, sid, 100)
    upstairs = seed_sector(25, 25, 1)
    seed_player_sector(pid, upstairs, 100)
    text = render_map(show_sector_neighborhood("U_P1", org_id=oid))
    assert "off this z-plane" in text

def test_render_map_propagates_error():
    assert render_map({"error": "Player not found"}) == "Error: Player not found"

# --- the resource map renders through the same map path ---

def _resources():
    pid = seed_player(); sid = seed_sector(25, 25, 0, energy=800.0)
    oid = seed_ship(pid, sid)
    seed_player_sector(pid, sid, 100)
    return show_neighborhood_resources("U_P1", org_id=oid)

def test_render_map_draws_the_resource_grid_with_no_new_code():
    """A second map tool renders through render_map() unchanged -- the whole
    point of putting the hints in the data rather than the client."""
    text = render_map(_resources())
    assert text.splitlines()[0].startswith("**Resources near ")
    assert " — Turn 0 of " in text.splitlines()[0]
    assert "| y/x | 21 | 22 |" in _squash(text)
    assert "0.80@" in text                      # the sector you're centered on, in thousands
    assert "Energy x 1k: 2.20 = 2,200" in text      # the unit, with its example

def test_render_map_draws_the_richest_table_under_the_resource_grid():
    text = _squash(render_map(_resources()))
    header_line = next(l for l in text.splitlines() if l.startswith("| Coords |"))
    assert header_line == "| Coords | Energy (000s) | Confidence |"
    assert "| (25,25,0) | 0.80 | 100 |" in text

def test_render_status_dispatches_the_resource_map_to_render_map():
    data = _resources()
    assert render_status(data) == render_map(data)


# --- alignment: the markdown SOURCE has to read as a table too ---

def test_tables_pad_every_column_to_its_widest_cell():
    """A client that renders the table ignores the padding; a log, a diff or a
    terminal showing the source does not, and an unpadded table is unreadable
    there the moment a cell outgrows its header."""
    data = {"widgets": [{"name": "a", "count": 3}, {"name": "elephant", "count": 5}],
            "display": {"rows_key": "widgets", "columns": ["name", "count"]}}
    lines = render_status(data).splitlines()
    assert lines[0] == "| name     | count |"
    assert lines[1] == "|----------|-------|"
    assert lines[2] == "| a        | 3     |"
    assert lines[3] == "| elephant | 5     |"
    assert len({len(l) for l in lines}) == 1        # every line the same width

def test_a_cell_wider_than_its_column_widens_the_column_rather_than_truncating():
    data = {"widgets": [{"n": "x" * 30}],
            "display": {"rows_key": "widgets", "columns": ["n"]}}
    assert "x" * 30 in render_status(data)

def test_resource_grid_cells_are_all_one_width_so_the_decimals_line_up():
    """Every cell, blanks and unknowns included -- a column of figures only
    reads as a column when the decimal points sit in the same place."""
    pid = seed_player(); sid = seed_sector(25, 25, 0, energy=2200.0)
    oid = seed_ship(pid, sid)
    seed_player_sector(pid, sid, 100)
    seen = seed_sector(27, 25, 0, energy=900.0)
    seed_player_sector(pid, seen, 60)
    grid = show_neighborhood_resources("U_P1", org_id=oid)["display"]["grid"]
    assert {len(c) for row in grid["rows"] for c in row["cells"]} == {CELL_WIDTH}
    lines = render_map(show_neighborhood_resources("U_P1", org_id=oid)).splitlines()
    body = [l for l in lines if l.startswith("| **")]
    assert len({len(l) for l in body}) == 1
    assert "2.20@" in body[4] and " 0.90" in body[4]
