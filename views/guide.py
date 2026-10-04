"""
Parses docs/player_guide.md into addressable chapters, one per top-level
(`##`) heading -- a `###` subheading stays folded into whichever chapter it
falls under, rather than becoming a chapter of its own.

Re-read and re-parsed from disk on every call rather than cached: the file is
a few KB, parsing it is microseconds, and there is nothing here worth trading
that simplicity for -- a doc edit is visible immediately to whatever imports
this module, with no invalidation to get wrong.
"""
import re
from pathlib import Path

GUIDE_PATH = Path(__file__).resolve().parent.parent / "docs" / "player_guide.md"


class Chapter:
    __slots__ = ("slug", "title", "content")

    def __init__(self, slug: str, title: str, content: str):
        self.slug, self.title, self.content = slug, title, content


def _slugify(heading: str) -> str:
    """
    GitHub's markdown anchor scheme: lowercase, strip anything that isn't a
    word character/space/hyphen, then turn every remaining space into its own
    hyphen (runs of spaces become runs of hyphens, not one).

    Matching this exactly is the point -- docs/player_guide.md already links
    its own sections by hand this way (e.g. `[Scanning & Discovery]
    (#scanning--discovery)`, the double hyphen coming from the removed `&`),
    so a slug returned here is the same one a reader following an in-doc link
    would land on.
    """
    s = re.sub(r"[^\w\s-]", "", heading.lower())
    return re.sub(r"\s", "-", s)


def _strip_trailing_rule(lines: list) -> list:
    """Drop a trailing blank-line/`---` run -- the horizontal rule between
    sections belongs to the document's layout, not to either chapter it
    separates."""
    lines = list(lines)
    while lines and lines[-1].strip() in ("", "---"):
        lines.pop()
    return lines


def load_guide():
    """
    Return (title, preface, chapters) -- the doc's own `#` title, the prose
    before the first `##` heading, and an ordered list of Chapter(slug,
    title, content), content being that section's own text INCLUDING its
    `## Heading` line, so it is complete, self-contained markdown on its own.
    """
    lines = GUIDE_PATH.read_text().splitlines()
    title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else ""

    chapters = []
    preface_lines = []
    current_title, current_lines = None, None

    def _flush():
        if current_title is not None:
            body = _strip_trailing_rule(current_lines)
            chapters.append(Chapter(_slugify(current_title), current_title,
                                    "\n".join(body).strip()))

    for line in lines[1:]:
        if line.startswith("## "):
            _flush()
            current_title = line[3:].strip()
            current_lines = [line]
        elif current_title is None:
            preface_lines.append(line)
        else:
            current_lines.append(line)
    _flush()

    preface = "\n".join(_strip_trailing_rule(preface_lines)).strip()
    return title, preface, chapters


def find_chapter(chapters: list, reference: str):
    """Look up a chapter by slug or exact title, case-insensitive either way
    -- a caller working from list_guide_chapters' `slug` column and one
    reading the doc's own heading text both just work."""
    reference = (reference or "").strip().lower()
    for chapter in chapters:
        if reference in (chapter.slug, chapter.title.lower()):
            return chapter
    return None
