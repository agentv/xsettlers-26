"""
Value formatting for the display hints reports carry.

Two jobs, deliberately split: this module turns one value into the string a
player reads ("80/80/100", "P1-01", "03:47"); views/render.py lays those
strings out as a table or a grid. A report builds its `display` block from
here and hands it to the renderer."""
from datetime import datetime, timezone

# Offloads simple, repetitive formatting onto the server rather than every
# client (LLM or not) reinventing it. Raw fields are always present alongside
# these in a report, so a client that wants its own presentation is free to
# ignore all of it.
RESOURCE_ABBREV = {"energy": "E", "food": "F", "goods": "G"}

# A sector holds energy in the thousands, and four raw digits per cell is
# wider than a map grid can carry legibly. Reported in thousands to two
# decimals instead: every figure is the same four characters whatever it is
# worth, which is what lets a column of them line up and be compared down the
# page. Any report using it has to say so in a legend -- see
# sector_tools.RESOURCE_LEGEND -- since "2.20" is meaningless without its unit.
ENERGY_UNIT = 1000.0

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

# The slots a tasking triple-and-then-some occupies, in order. Longer than
# RESOURCE_ABBREV because a pod's task is not only production: a scanning or
# idle pod is still a pod the player paid for, and leaving it out of the
# tasking column makes a fixed-width cell look complete while under-counting
# the crew. Every task a pod can hold needs a slot here.
TASK_ABBREV = {"produce_energy": "E", "produce_food": "F", "produce_goods": "G",
               "scan": "S", "idle": "I"}

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


def in_thousands(value: float) -> str:
    """2200.0 -> "2.20" -- a figure in thousands, always two decimals."""
    return f"{value / ENERGY_UNIT:.2f}"


def short_name(name: str) -> str:
    """"Ship-P1-01" -> "P1-01", "Colony-P1" -> "P1" -- a ready-to-display
    label so clients don't need their own name-shortening rule."""
    for prefix in _NAME_PREFIXES_TO_STRIP:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def display_label(name: str, call_sign: str = None) -> str:
    """
    What a report calls a unit: its call sign if the player has given it one,
    otherwise its given name.

    A call sign is set precisely so a player sees their own word for a unit
    rather than "S3", so it wins here -- but it is additive, never a rename,
    and every report keeps `name` alongside in its data block so the given
    name stays available (see organizations.call_sign).
    """
    call_sign = (call_sign or "").strip()
    return call_sign or short_name(name)


def slashed(values: dict, slots: dict = RESOURCE_ABBREV) -> str:
    """
    {"energy": 20.0, "food": 20.0} -> "20/20/0" -- one slot per entry in
    `slots`, in that order, 0 for anything the dict doesn't mention. What each
    number counts is carried by the column header (see stacked_header), not
    repeated on every cell.

    `slots` is what separates a resource-keyed dict from a task-keyed one --
    RESOURCE_ABBREV for holdings and production, TASK_ABBREV for pod tasking.
    A key with no slot is dropped, so a slot set must cover everything its
    dicts can hold, or the cell silently under-counts.
    """
    return "/".join(f"{values.get(name, 0):g}" for name in slots)


def tasking_summary(tasking: dict) -> str:
    """{"produce_energy": 2, "produce_food": 2, "scan": 1} -> "2/2/0/1/0"."""
    return slashed(tasking, TASK_ABBREV)


def resource_summary(values: dict) -> str:
    """{"energy": 20.0, "food": 20.0} -> "20/20/0"."""
    return slashed(values)


def stacked_header(label: str, slots: dict = RESOURCE_ABBREV) -> str:
    """
    A two-line header for a column whose cells are a `slashed` run:
    the column's name, then the order its numbers follow. Pass the same
    `slots` the cells were built with.

    `<br>` rather than a literal newline -- a markdown table cell is
    single-line by definition, and every client that renders these tables
    renders them as HTML.
    """
    return f"{label}<br>{'/'.join(slots.values())}"


def turn_header(current_turn: int, turn_limit: int, next_tick_at: str | None) -> str:
    """"Turn 7 of 20 (03:47)" -- the line a report puts above its table so a
    player always knows where they are in the game and how long they have to
    act. Shared by every turn-context report, so they cannot drift apart."""
    return f"Turn {current_turn} of {turn_limit} ({tick_countdown(next_tick_at)})"


def winners_label(winners: list) -> str:
    """
    "Winner: Ada" for one, "Winners: Ada, Grace" for a tie, "Winner: none"
    for an empty game. Nothing breaks a tie in scoring, so every player on
    the top rank is named and the word agrees with how many there are.
    """
    if not winners:
        return "Winner: none"
    label = "Winner" if len(winners) == 1 else "Winners"
    return f"{label}: {', '.join(winners)}"


def totals_footer(title: str, assets: dict) -> list:
    """
    The fleet-wide aggregate, rendered below the table rather than in it."""
    percent = assets.get("percent_full", 0.0)
    return [f"**{title}**",
            f"- Storage ({'/'.join(RESOURCE_ABBREV.values())}): {slashed(assets)}",
            f"- Capacity: {assets.get('total', 0):g} / {assets.get('capacity', 0):g} "
            f"({percent:g}% full)"]


def scanner_footer(scanners: list) -> str | None:
    """
    A ready-to-render line for an org's active scanners ("Scans: North, South,
    Southeast"), or None when it has none in use."""
    if not scanners:
        return None
    aimed = [BEARING_FULL_NAME.get(s["bearing"], s["bearing"])
             for s in scanners if s["aimed"]]
    unaimed = sum(1 for s in scanners if not s["aimed"])
    footer = f"Scans: {', '.join(aimed)}" if aimed else "Scans: none aimed"
    if unaimed:
        footer += f" (+{unaimed} unaimed)"
    return footer
