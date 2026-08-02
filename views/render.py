def _markdown_table(columns: list, rows: list) -> list:
    """Header + separator + one line per row. Returns lines, not a string."""
    lines = ["| " + " | ".join(columns) + " |",
             "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return lines


def render_status(data: dict) -> str:
    """
    Generic renderer for any tool that follows the display-hints convention:
    a top-level `display` dict naming `rows_key` (which field holds the list of
    row-dicts to render), `columns` (which fields to show, in order), an
    optional `header` line, and an optional `column_labels` dict overriding a
    column's header text (keyed by field name) when the raw field name isn't
    what a human should read -- e.g. `task_display` -> "Task". Columns with no
    entry in `column_labels` header as the field name itself, same as always.
    Covers the status tools in
    xsettlers_mcp/tools/organization_tools.py (show_game_status,
    show_civilization_status, show_organization).

    `display.kind == "map"` hands off to render_map() -- a grid is not a table,
    but the dispatch is still on the *shape* of the data, not on which tool
    produced it. Deliberately has no per-tool-name branching: any future tool
    returning either shape renders here with zero changes, which is the whole
    point of putting the hints in the data instead of the client.

    This is an interim, MVP-level renderer (markdown, plain str(cell)
    formatting); it's expected to be superseded by the fuller card/renderer
    architecture sketched in docs/ui_and_rendering_design.md once that's
    actually built.
    """
    if "error" in data:
        return f"Error: {data['error']}"

    display = data.get("display") or {}
    if display.get("kind") == "map":
        return render_map(data)
    rows = data.get(display.get("rows_key"), [])
    columns = display.get("columns") or []
    labels = display.get("column_labels") or {}

    lines = []
    if display.get("header"):
        lines.append(f"**{display['header']}**")
        lines.append("")

    if not columns or not rows:
        lines.append("(no rows)")
        return "\n".join(lines)

    headers = [labels.get(c, c) for c in columns]
    lines.extend(_markdown_table(headers, [[str(r.get(c, "")) for c in columns] for r in rows]))
    return "\n".join(lines)


def render_map(data: dict) -> str:
    """
    Render a `display.kind == "map"` payload (see
    xsettlers_mcp/tools/sector_tools.show_sector_neighborhood) as a markdown
    table: x coordinates across the header, y down the left, one pre-rendered
    marker per cell.

    A markdown table rather than monospace ASCII art on purpose -- every MCP
    client renders tables with correct alignment, whereas ASCII grids break the
    moment a cell holds a double-width glyph, and the target client is a phone.

    Axis labels are absolute coordinates, not offsets from the center, so a
    coordinate read off the map can be handed straight to preview_move or
    set_pod_scan_target without arithmetic.

    The cell strings are built by the tool, not here: this function decides
    layout, the tool decides meaning. A client that wants a different-looking
    map can re-render from `display.grid` without re-deriving what a cell says.
    """
    if "error" in data:
        return f"Error: {data['error']}"

    display = data.get("display") or {}
    grid = display.get("grid") or {}
    x_labels = grid.get("x_labels") or []

    lines = []
    if display.get("header"):
        lines.append(f"**{display['header']}**")
        lines.append("")

    if not x_labels or not grid.get("rows"):
        lines.append("(empty map)")
        return "\n".join(lines)

    lines.extend(_markdown_table(
        [grid.get("corner", "y/x")] + x_labels,
        [[f"**{row['label']}**"] + row["cells"] for row in grid["rows"]]))

    if display.get("legend"):
        lines.append("")
        lines.extend(display["legend"])

    off_plane = data.get("off_plane_count") or 0
    if off_plane:
        lines.append("")
        lines.append(f"({off_plane} known sector(s) in range lie off this z-plane "
                     f"and are not drawn -- see `sectors`.)")

    rows = data.get(display.get("rows_key"), [])
    columns = display.get("columns") or []
    if rows and columns:
        labels = display.get("column_labels") or {}
        headers = [labels.get(c, c) for c in columns]
        lines.append("")
        lines.extend(_markdown_table(headers, [[str(r.get(c, "")) for c in columns] for r in rows]))
    return "\n".join(lines)
