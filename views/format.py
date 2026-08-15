"""
Value formatting for the display hints reports carry.

Two jobs, deliberately split: this module turns one value into the string a
player reads ("E:20, F:20", "P1-01", "03:47"); views/render.py lays those
strings out as a table or a grid. A report builds its `display` block from
here and hands it to the renderer.

Pure functions over plain values -- no database, no engine imports, nothing to
know about which tool is calling. That is what lets a report add a formatted
field without either the renderer or the engine learning about it.
"""
from datetime import datetime, timezone

# Offloads simple, repetitive formatting onto the server rather than every
# client (LLM or not) reinventing it. Raw fields are always present alongside
# these in a report, so a client that wants its own presentation is free to
# ignore all of it.
RESOURCE_ABBREV = {"energy": "E", "food": "F", "goods": "G"}

# Legacy bootstrap name prefixes ("Ship-P1-01", "Colony-P1"), stripped for
# display. Current defaults are already short (S1..Sn, C1).
_NAME_PREFIXES_TO_STRIP = ("Ship-", "Colony-")

# Locked MVP cargo-table format for a single org's status (see
# show_organization): columns are Task, Count, Energy, Food, Goods, Capacity --
# Capacity as a "current/total" string rather than a bare number. Richer
# presentations are sketched in docs/ui_and_rendering_design.md; this is the
# one clients can render today without inventing their own column order.
TASK_DISPLAY = {"produce_energy": "Energy", "produce_food": "Food",
                "produce_goods": "Goods", "idle": "Idle", "scan": "Scan"}

# Spelled-out compass names for the scanners footer -- distinct from the terse
# codes (engine/bearings.py's "N"/"NE"/"N2") used everywhere else, since this
# is a summary line meant to read in plain English rather than a table cell.
BEARING_FULL_NAME = {
    "N": "North", "NE": "Northeast", "E": "East", "SE": "Southeast",
    "S": "South", "SW": "Southwest", "W": "West", "NW": "Northwest",
    "N2": "North (2)", "E2": "East (2)", "S2": "South (2)", "W2": "West (2)",
}


def tick_countdown(next_tick_at: str | None) -> str:
    """
    "MM:SS" until the next clock tick, or "--:--" when there's nothing ticking
    -- next_tick_at is None before any scenario is selected or whenever the
    clock process isn't the one refreshing it (see engine/turn.py's
    get_next_tick_at). Unlike scripts/status.py's _clock_status this needs no
    liveness probe: it runs in the same process the clock does, so None here
    already means "not currently running".
    """
    if not next_tick_at:
        return "--:--"
    next_dt = datetime.strptime(next_tick_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    remaining = max(0, round((next_dt - datetime.now(timezone.utc)).total_seconds()))
    minutes, seconds = divmod(remaining, 60)
    return f"{minutes:02d}:{seconds:02d}"


def short_name(name: str) -> str:
    """"Ship-P1-01" -> "P1-01", "Colony-P1" -> "P1" -- a ready-to-display
    label so clients don't need their own name-shortening rule."""
    for prefix in _NAME_PREFIXES_TO_STRIP:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def abbreviated(values: dict, key=lambda k: k) -> str:
    """{"energy": 20.0, "food": 20.0} -> "E:20, F:20". `key` maps a dict key
    onto a resource name, which is all that separates a resource-keyed dict
    from a task-keyed one. Entries naming something that isn't a resource
    (an idle or scan pod count) are left out rather than shown unabbreviated."""
    return ", ".join(f"{RESOURCE_ABBREV[key(k)]}:{v:g}"
                     for k, v in values.items() if key(k) in RESOURCE_ABBREV)


def tasking_summary(tasking: dict) -> str:
    """{"produce_energy": 2, "produce_food": 2} -> "E:2, F:2"."""
    return abbreviated(tasking, key=lambda task: task.replace("produce_", ""))


def resource_summary(values: dict) -> str:
    """{"energy": 20.0, "food": 20.0} -> "E:20, F:20"."""
    return abbreviated(values)


def scanner_footer(scanners: list) -> str | None:
    """
    A ready-to-render line for an org's active scanners ("Scans: North, South,
    Southeast"), or None when it has none in use.

    An unaimed scan pod still costs its food and reveals nothing, so it is
    counted and flagged rather than silently dropped.
    """
    if not scanners:
        return None
    aimed = [BEARING_FULL_NAME.get(s["bearing"], s["bearing"])
             for s in scanners if s["aimed"]]
    unaimed = sum(1 for s in scanners if not s["aimed"])
    footer = f"Scans: {', '.join(aimed)}" if aimed else "Scans: none aimed"
    if unaimed:
        footer += f" (+{unaimed} unaimed)"
    return footer
