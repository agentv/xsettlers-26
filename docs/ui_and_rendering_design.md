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

---

# Org Card — UI Spec

## Card Design

Each organization (ship or colony) is represented as a **card** — a self-contained rectangular unit sized proportionally to a playing card.

> **Adaptive sizing rule:** Cards are NOT fixed at a pixel dimension. Use `aspect-ratio: 5/7` with `min-width: 180px` and `max-width: 280px`. All internal spacing, font sizes, and bar heights use relative units (`rem`, `%`) so the card scales correctly across display sizes. The playing-card feel comes from the aspect ratio, not pixel counts.

**Card anatomy (top to bottom):**

1. **Header** — org name, type icon (ship vs colony), status indicator dot (green = idle/docked, amber = in transit)
2. **Location line** — sector coords `(x, y)` or `In Transit → (x, y)` with arrival turn
3. **Mission line** — current mission string
4. **Resource bars** — one bar per production task, colloquially `energy`, `factory`, `farm`. Each bar shows:
    1. A fill bar representing `aggregate_storage_current / aggregate_storage_capacity` for the pods on that task
    2. The numeric readout overlaid on the bar: e.g. `82 / 100`
5. **Footer** — player owner identifier (debug view only)

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
covered by `tests/test_sector_tools.py` and `tests/test_render.py`), scan range
enforcement and confidence stamping at end-of-turn resolution
(`tests/test_sector_tools.py`), and fog blink-out (`tests/test_turn.py`).

* `views/html_renderer.py` — implement `render_org_card(view: dict) -> str` returning hydrated card HTML; consume adaptive card spec above
* **Future:** variable `get_scan_range(org_id)` derived from the pods on the scan task — wire up when that is designed. Not a `sensor` pod type; pods have no type.
* **Future:** SVG map renderer — `views/svg_renderer.py`; same view model contract, different output format
* **Future:** whole-known-map view. `player_sectors` already *is* the global known-sectors store and `get_sector_map()` already reads it, and `render_map()` is written against a viewport (center + radius + known cells) rather than against "neighborhood" specifically — so the same renderer draws it. What is genuinely unbuilt is the width problem: `game0` puts home sectors 25 apart, so a full known-map bounding box exceeds what a markdown table shows readably on a phone. Needs downsampling or paging first.
* **Future:** Column config persistence — save/load named column layouts per player
