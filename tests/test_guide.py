"""
The player guide, served over MCP: views/guide.py's parser and
xsettlers_mcp/tools/guide_tools.py's two tools together, since a reader never
touches one without the other.
"""
from views.guide import load_guide, find_chapter
from xsettlers_mcp.tools.guide_tools import list_guide_chapters, show_guide_chapter

EXPECTED_CHAPTERS = [
    ("The Setting", "the-setting"),
    ("The Basics", "the-basics"),
    ("Pods: What Actually Produces", "pods-what-actually-produces"),
    ("Movement", "movement"),
    ("Task Forces", "task-forces"),
    ("Colonizing", "colonizing"),
    ("Sharing Resources", "sharing-resources"),
    ("Scanning & Discovery", "scanning--discovery"),
    ("Standing Orders", "standing-orders"),
    ("Winning", "winning"),
    ("Quick Reference", "quick-reference"),
    ("Scenarios", "scenarios"),
]

# --- views/guide.py: parsing docs/player_guide.md ---

def test_load_guide_splits_on_every_top_level_heading_in_order():
    """One chapter per `##`, in the order they appear in the file -- a `###`
    subheading (e.g. "Names and call signs" under The Basics) must NOT become
    a chapter of its own."""
    title, preface, chapters = load_guide()
    assert title == "XSettlers — Player Guide"
    assert [(c.title, c.slug) for c in chapters] == EXPECTED_CHAPTERS

def test_preface_is_the_prose_before_the_first_heading():
    _title, preface, _chapters = load_guide()
    assert preface.startswith("This is the rulebook.")
    assert "## " not in preface   # nothing chapter-shaped leaked into it

def test_chapter_content_is_self_contained_markdown():
    """A chapter's content starts with its own heading line and carries no
    trailing horizontal rule -- it is a complete, standalone document."""
    _title, _preface, chapters = load_guide()
    task_forces = next(c for c in chapters if c.slug == "task-forces")
    assert task_forces.content.startswith("## Task Forces")
    assert not task_forces.content.rstrip().endswith("---")
    assert "named group of your own ships" in task_forces.content

def test_a_subheading_stays_folded_into_its_parent_chapter():
    _title, _preface, chapters = load_guide()
    basics = next(c for c in chapters if c.slug == "the-basics")
    assert "### Names and call signs" in basics.content
    assert not any(c.slug == "names-and-call-signs" for c in chapters)

def test_find_chapter_matches_by_slug_or_title_case_insensitively():
    _title, _preface, chapters = load_guide()
    assert find_chapter(chapters, "winning").title == "Winning"
    assert find_chapter(chapters, "WINNING").title == "Winning"
    assert find_chapter(chapters, "Scanning & Discovery").slug == "scanning--discovery"
    assert find_chapter(chapters, "no such chapter") is None

# --- list_guide_chapters ---

def test_list_guide_chapters_is_the_full_index_in_order():
    result = list_guide_chapters()
    assert [(row["title"], row["slug"]) for row in result["chapters"]] == EXPECTED_CHAPTERS
    assert [row["number"] for row in result["chapters"]] == list(range(1, len(EXPECTED_CHAPTERS) + 1))
    assert result["display"]["rows_key"] == "chapters"

# --- show_guide_chapter ---

def test_show_guide_chapter_returns_the_full_chapter_by_slug():
    result = show_guide_chapter("task-forces")
    assert result["title"] == "Task Forces"
    assert result["content"].startswith("## Task Forces")
    assert result["display"] == {"kind": "text", "text_key": "content"}

def test_show_guide_chapter_accepts_the_heading_text_too():
    assert show_guide_chapter("Winning")["slug"] == "winning"

def test_show_guide_chapter_unknown_reports_every_valid_slug():
    result = show_guide_chapter("does-not-exist")
    assert "error" in result
    assert result["valid_chapters"] == [slug for _title, slug in EXPECTED_CHAPTERS]

def test_show_guide_chapter_links_to_its_neighbors():
    first = show_guide_chapter("the-setting")
    assert first["previous"] is None
    assert first["next"] == "the-basics"

    middle = show_guide_chapter("movement")
    assert middle["previous"] == "pods-what-actually-produces"
    assert middle["next"] == "task-forces"

    last = show_guide_chapter("scenarios")
    assert last["next"] is None
    assert last["previous"] == "quick-reference"
