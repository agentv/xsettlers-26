def _markdown_table(columns: list, rows: list) -> list:
    """
    Header + separator + one line per row, every column padded to its widest
    entry. Returns lines, not a string.

    The padding is for whoever reads the markdown *source* -- a client that
    shows text raw, a log, a diff, a terminal. A client that renders the table
    ignores the whitespace entirely, so it costs nothing where it isn't
    needed, and an unpadded table is unreadable as source the moment one cell
    is longer than its header.

    Cells are padded, never truncated: a wide cell widens its column rather
    than losing characters. Padding is left-aligned here because most cells
    are labels; a column that wants its digits aligned pads its own cells to a
    fixed width before they arrive (see sector_tools.CELL_WIDTH), which this
    then leaves alone.
    """
    table = [list(columns)] + [list(row) for row in rows]
    widths = [max(len(row[i]) if i < len(row) else 0 for row in table)
              for i in range(len(columns))]

    def line(cells):
        return "| " + " | ".join(
            (cells[i] if i < len(cells) else "").ljust(widths[i])
            for i in range(len(columns))) + " |"

    return [line(columns),
            "|" + "|".join("-" * (w + 2) for w in widths) + "|"] + \
           [line(row) for row in table[1:]]


def _hinted_table(display: dict, rows: list, columns: list) -> list:
    """One table, built the way a `display` block asks for it: `columns` names
    the fields in order, `column_labels` overrides a header where the raw field
    name isn't what a human should read. Both renderers below draw their rows
    this way, so a table looks the same wherever it appears."""
    labels = display.get("column_labels") or {}
    return _markdown_table([labels.get(c, c) for c in columns],
                           [[str(r.get(c, "")) for c in columns] for r in rows])


def render_status(data: dict) -> str:
    """
    Generic renderer for any tool following the display-hints convention: a
    top-level `display` dict naming `rows_key` (which field holds the row
    list), `columns` (which fields to show, in order), an optional `header`,
    an optional `column_labels` overriding a column's header text when the
    raw field name isn't what a human should read, and an optional `footer`
    line or list of lines appended below the table. A column with no entry in
    `column_labels` headers as the field name itself.

    `display.kind == "map"` hands off to render_map(). Dispatch is on the
    *shape* of the data, never on which tool produced it -- there is no
    per-tool branching here, so any future tool returning either shape
    renders with no changes.
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

    lines.extend(_hinted_table(display, rows, columns))

    footer = display.get("footer")
    if footer:
        lines.append("")
        lines.extend(footer if isinstance(footer, list) else [footer])
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
        lines.append("")
        lines.extend(_hinted_table(display, rows, columns))
    return "\n".join(lines)
