# XSettlers — UI & Rendering Design

---

# Visibility Rules

These are the four authoritative rules governing what a player can see. All view models and renderers must respect them.

1. **Occupation** — An org physically present in a sector grants full, current detail on that sector. The player's `confidence` in that sector is pinned at 100 as long as any of their orgs occupies it.
2. **Scan** — Executed at end of turn by an organization's own sensors (every org can scan one sector per turn without a pod) and by any pods on the `scan` task, granting full detail on the target sector at the moment of resolution. Confidence starts decaying from that point forward per the normal fog decay rate (`CONFIDENCE_DECAY_PER_TURN`).
3. **Fog decay — blink-out** — Any sector not currently occupied loses a flat `CONFIDENCE_DECAY_PER_TURN` points each turn (default 20). At confidence = 0 the sector **leaves the player's view entirely**. There is no degraded "last known" ghost indicator: a sector the player has stopped confirming becomes indistinguishable from one they never visited. The `player_sectors` row survives as history, but every player-facing read filters `confidence > 0`. At the default rate that is five turns from last sighting to gone. (See [Data Model & Storage Design](data_model_and_storage_design.md) for why the decay is subtractive rather than proportional.)
4. **Rival detection** — If a rival org is within the player's scan range at the time of a scan, that org's presence is surfaced with **high priority** in the player view — distinct from ordinary sector data but not a hard alert system.

> **Closed roster rule applies here too:** Player visibility is always computed relative to a fixed player set. There is no concept of an anonymous or guest observer in the player view.

---

# Scan Action Design

> **Scanning is not a player-callable action.** A player aims a scanner
> (`set_pod_task` / `set_pod_scan_bearing` / `set_org_scan_bearing`) and the
> engine resolves it at end of turn (`engine/turn.py`, step 3). The range rule,
> the reveal and the confidence stamp below describe what happens; only the
> trigger is end-of-turn resolution rather than an immediate call.

## Scan resolution

**Scan range:** Fixed at 2 sectors (Euclidean distance ≤ 2), derived from `get_scan_range(org_id)`. At range 1 a Euclidean radius reaches only the 4 orthogonal neighbours (a diagonal is √2 ≈ 1.41 > 1), which reads as broken; range 2 reaches 12 sectors.

**A scan reveals only its target sector.** No halo, no ring — range governs reach, not breadth. A radius-5 halo was considered and rejected as making scanning too cheap for the value it returns.

> **Future hook — scan range from the pods aboard:** Scan range will eventually be derived from an org's pods rather than a constant, and `get_scan_range(org_id)` will query the pod table. Note this is about pods *on the scan task*, not a `sensor` pod type — pods have no type (see [Product Requirements](product_requirements.md)), so range would come from how many crews are aimed at the problem. Do NOT hard-code the number at the call site — always call `get_scan_range()`.

# Neighborhood Map — built

Unlike the card/renderer architecture below, this one exists:
`show_sector_neighborhood()` (`xsettlers_mcp/tools/sector_tools.py`) builds it
and `render_map()` (`views/render.py`) draws it.

## Why the server pre-renders the grid


So the tool ships a finished grid inside `display`, alongside the structured
cells — the same move `show_organization` makes with its locked cargo-table
spec, in a different shape. A client that wants a different-looking map can
still re-render from `display.grid` without re-deriving what a cell *means*.

**The tool decides meaning; the renderer decides layout.** Cell strings are
built in `sector_tools.py`; `render_map()` only arranges them.

## Grid shape

* **Markdown table, not monospace ASCII art.** Every MCP client renders tables
  with correct alignment; ASCII grids break the moment a cell holds a
  double-width glyph (all emoji are), and the target client is a phone.
* **Absolute coordinates on both axes**, not offsets from the center — a
  coordinate read off the map goes straight into `preview_move` or
  `set_pod_scan_bearing` with no arithmetic.
* **Bounding square, with out-of-range cells blank.** The blank corners trace
  the disc shape of the radius, teaching the player their own range.
* **One z-plane — the center's.** The model is 3D and distance is 3D
  everywhere else, but no scenario has yet placed anything off `z=0`, so a
  plane is the whole picture today. Known sectors in range but off-plane are
  still returned in `sectors` and counted in `off_plane_count` rather than
  silently flattened in, so the day `z` matters the view says so.

## Cell vocabulary

Every marker is at most 3 characters, so the table stays narrow.

| Cell | Meaning |
|---|---|
| `S3` | 3 of the player's ships |
| `C` | the player's colony (`C2` for two) |
| `S3C` | ships and a colony together |
| `S3!` | rival present alongside the player's orgs |
| `R` | rival present, none of the player's orgs |
| `*` | seen, and nothing of anyone's there |
| `·` | in range, never seen — i.e. the scan-me list |
| *(blank)* | outside the scan radius |

Two deliberate choices:

* **Confidence is not on the grid.** It is a reporting number, not something to
  steer by: under blink-out a sector is either still on the map or gone from
  it, so the cell is binary. Exact confidence and resources ride in the detail
  rows (`display.rows_key` → `highlights`).
* **A rival always wins the third character**, truncating the own-org marker to
  make room (`S6C` + rival → `S6!`). A contested sector is the most important
  cell on the board, and what the truncation costs — knowing a colony is also
  there — is static information the player already has, whereas the rival is
  news. Exact composition is in `highlights` either way.

## Renderer dispatch

`render_status()` hands off to `render_map()` on `display.kind == "map"`. The
dispatch is on the *shape* of the data, never on which tool produced it — same
contract as the table path, so any future tool returning either shape renders
with no renderer changes.

## Graphic form — built

`views/neighborhood.py` draws the same payload as SVG, registered in
`server.py`'s `SVG_RENDERERS`. It does not replace the table: markdown is what
a text client gets, and `response_format='html_svg'` is a request, not a
promise.

**Why the map looks the way it does is not documented here.** That is a design
question and it lives in `../xsettlers-designer/docs/neighborhood_map_graphic.md`
— the palette, the encodings, what a sigil means, why a rival cools. What
follows is only the wiring.

**One payload, three channels.** `show_sector_neighborhood(channel=...)` takes
`occupancy`, `energy` or `scan` and echoes it into `display.channel`; the
markdown table ignores it and always answers who-is-where. The sector rows
already carry own counts, rival counts, energy capacity and scan aims
together, so three views of one neighborhood is one call.

**The channel is a tool argument, not a renderer argument**, and that is
forced: `SVG_RENDERERS` maps a tool name to a callable taking the result dict
and nothing else. A renderer that needed a second parameter could not be
registered, so the channel travels in the payload like everything else.

**`display.scales` carries the engine's own numbers** — sector energy floor and
ceiling, confidence maximum and decay rate, and how many times a sighting is
drawn before it blinks out. `views/` imports nothing outside the standard
library, which is what lets a rasterizer or a Block Kit consumer use a layout
without opening a database, so it cannot reach `db/sectors.py` for these. The
renderer keeps module constants as fallbacks for a payload captured before the
block existed; they are not a second source of truth, and a test asserts that
moving a scale moves the map.

**Three payload fields exist for this renderer** and are worth not removing:

| field | why |
|---|---|
| `rival_ships` / `rival_colonies`, `sighted_ships` / `sighted_colonies` | a colony is a permanent hold and a ship is passing through; a bare count cannot say which is standing there |
| `scan_aims[].origin_x/y/z` | otherwise every reader needs a copy of `SCAN_BEARINGS`, correct only while `SCAN_RANGE == 2` |
| `display.channel`, `display.scales` | see above |

**One glyph pair for the whole codebase.** `svg_renderer.icon_marks(org_type,
x, y, color, size)` draws the ship chevron and the colony block for both the
card and a map node. A shape duplicated per caller drifts, and these two carry
meaning rather than decoration.

**Assert on the mark list, never on an SVG string** — the same rule the card's
tests follow, for the same reason.

---

# Org Card — UI Spec

## Card Design

Each organization (ship or colony) is represented as a **card** — a self-contained rectangular unit sized proportionally to a playing card.

**Built** — `views/svg_renderer.py`, drawn from the dict `show_organization()` already returns. The card is 260px wide with a content-derived height; the `aspect-ratio: 5/7` playing-card proportion sketched earlier is not what shipped.

**Card anatomy (top to bottom):**

1. **Header** — org name, type icon (chevron = ship, roofed block = colony), status dot (green = docked, amber = in transit)
2. **Location line** — sector coords `(x, y, z)` or `in transit`
3. **Mission line** — current mission string
4. **Tasking bars** — one per task in `_TASK_ORDER`: idle, energy, food, goods, scan. Each shows pods on that task out of the org's *total* pod count, e.g. `2/6 pods`.
5. **Storage line** — one composite bar: energy, food and goods as coloured segments of total capacity, the empty tail showing headroom, a right-aligned `used/capacity` readout, and a legend carrying the three figures plus `free N`
6. **Scanner line** — bearings of any scanning pods, or `unaimed`

Three decisions worth not re-opening:

- **Every tasking bar draws whether or not a pod is on it.** Absent task groups read `0/N` rather than vanishing, so the card is a fixed shape a captain can compare against itself turn to turn, and only fills move. Task groups come from a `GROUP BY`, so the alternative made the card grow and shrink as pods retask.
- **Idle leads, and turns `#ef4444` the moment a pod is on it.** The row is always present; the alarm colour is what appears, not the row.
- **Storage is one bar, not one per resource.** An org already spends its pods as a single purse — `engine/org_resources.py` sums, drains and fills across all of them, so per-pod distribution cannot change an outcome. Three bars against a shared denominator could never read full (each resource is a fraction of the *whole* hold) and never showed headroom at all.

## Column Layout

Columns are **user-defined view streams** — not tied to any specific data entity. A column is simply:

```python
{
  "label": str,           # user-defined name shown as column header
  "filter_fn": callable,  # takes view model list, returns matching org cards
  "sort_fn": callable     # optional; default: insertion order
}
```

Example column configurations:

* `"Sector (4,4)"` — filter: orgs where `sector_coords == [4, 4, 0]`
* `"All Farm pods < 50%"` — filter: orgs with any farm pod below 50% capacity
* `"In Transit"` — filter: orgs where `status == "in_transit"`
* `"Rival Contacts"` — filter: sectors with high-priority rival detections in last 3 turns

**Layout model:**

* Columns pan **horizontally** across the viewport
* Cards within a column stream **vertically**, scrolling independently
* Multiple columns may be open simultaneously
* Column configs are defined in a simple list; the renderer loops over them

> **Simplicity rule:** A column is just a label + a filter function + an optional sort. No schema, no config object, no refresh cadence for MVP. The renderer evaluates each column's filter against the current view model list and streams matching cards into it.

# Known TODOs

Done, listed here only so they aren't re-opened: the neighborhood map and its
renderer (`show_sector_neighborhood` + `views/render.py`'s `render_map()`,
covered by `tests/test_sector_tools.py` and `tests/test_render.py`), its
graphic form (`views/neighborhood.py`, `tests/test_render.py`), scan range
enforcement and confidence stamping at end-of-turn resolution
(`tests/test_sector_tools.py`), and fog blink-out (`tests/test_turn.py`).

* `views/html_renderer.py` — implement `render_org_card(view: dict) -> str` returning hydrated card HTML; consume adaptive card spec above
* **Future:** variable `get_scan_range(org_id)` derived from the pods on the scan task — wire up when that is designed. Not a `sensor` pod type; pods have no type.
* **Future:** SVG *map* renderer. `views/svg_renderer.py` exists but draws org cards only; a map would reuse its `emit_svg` and add a layout
* **Future:** whole-known-map view. `player_sectors` already *is* the global known-sectors store and `get_sector_map()` already reads it, and `render_map()` is written against a viewport (center + radius + known cells) rather than against "neighborhood" specifically — so the same renderer draws it. What is genuinely unbuilt is the width problem: `game0` puts home sectors 25 apart, so a full known-map bounding box exceeds what a markdown table shows readably on a phone. Needs downsampling or paging first.
* **Future:** Column config persistence — save/load named column layouts per player
