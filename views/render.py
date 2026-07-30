def render_status(data: dict) -> str:
    """
    Generic renderer for any status tool in xsettlers_mcp/tools/organization_tools.py
    (show_game_status, show_civilization_status, show_organization) that
    follows the display-hints convention: a top-level `display` dict naming
    `rows_key` (which field holds the list of row-dicts to render), `columns`
    (which fields to show, in order), and an optional `header` line.

    Deliberately has no per-tool-name branching -- any future tool that
    returns the same `display` shape renders here with zero changes, which is
    the whole point of putting the hints in the data instead of the client.
    This is an interim, MVP-level renderer (markdown table, plain str(cell)
    formatting); it's expected to be superseded by the fuller card/renderer
    architecture sketched in docs/ui_and_rendering_design.md once that's
    actually built.
    """
    if "error" in data:
        return f"Error: {data['error']}"

    display = data.get("display") or {}
    rows = data.get(display.get("rows_key"), [])
    columns = display.get("columns") or []

    lines = []
    if display.get("header"):
        lines.append(f"**{display['header']}**")
        lines.append("")

    if not columns or not rows:
        lines.append("(no rows)")
        return "\n".join(lines)

    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "|".join(["---"] * len(columns)) + "|")
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    return "\n".join(lines)
