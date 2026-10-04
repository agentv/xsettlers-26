"""
Serves docs/player_guide.md over MCP so any client can read the rulebook on
its own -- an index (list_guide_chapters) and one chapter at a time
(show_guide_chapter), rather than a single wall of text a client has to
scroll or truncate.

Deliberately not @player_tool. The guide is the same for every player and
useful before anyone has a game to be a player of -- gated content would
mean a prospective player needs a token before they can even read the rules
that explain what a token gets them. See views/guide.py for the parser these
two tools are thin wrappers over.
"""
from xsettlers_mcp.tools.registry import mcp_tool
from views.guide import load_guide, find_chapter


@mcp_tool(
    "Index of the player guide's chapters, in reading order -- the table of "
    "contents. Call this first; pass any row's `slug` to show_guide_chapter "
    "to read that chapter in full. Free to call with no game selected and no "
    "player_token.")
def list_guide_chapters() -> dict:
    title, preface, chapters = load_guide()
    rows = [{"number": i + 1, "title": c.title, "slug": c.slug}
            for i, c in enumerate(chapters)]
    return {
        "title": title,
        "preface": preface,
        "chapters": rows,
        "display": {
            "header": title,
            "rows_key": "chapters",
            "columns": ["number", "title", "slug"],
            "column_labels": {"number": "#", "title": "Chapter", "slug": "slug"},
            "footer": "Pass a `slug` to show_guide_chapter to read that chapter.",
        },
    }


@mcp_tool(
    "One chapter of the player guide, in full -- its complete markdown text, "
    "not a summary. `chapter` is a slug from list_guide_chapters (or the "
    "chapter's exact heading text, case-insensitive). `previous`/`next` in "
    "the result name the adjacent chapters' slugs, for paging through "
    "sequentially. Free to call with no game selected and no player_token.")
def show_guide_chapter(chapter: str) -> dict:
    _title, _preface, chapters = load_guide()
    match = find_chapter(chapters, chapter)
    if match is None:
        return {"error": f"Unknown chapter '{chapter}'.",
                "valid_chapters": [c.slug for c in chapters]}
    index = chapters.index(match)
    return {
        "number": index + 1,
        "slug": match.slug,
        "title": match.title,
        "content": match.content,
        "previous": chapters[index - 1].slug if index > 0 else None,
        "next": chapters[index + 1].slug if index < len(chapters) - 1 else None,
        "display": {"kind": "text", "text_key": "content"},
    }
