# XSettlers — UI & Rendering Design

**Scope:** Defines how game state is translated into player-facing output. Contains visibility rules, view model schemas, scan action design, and rendering contracts for debug and player modes. No DB schema here — see [Data Model & Storage Design](data_model_and_storage_design.md). No tool implementations here — see the MCP Tools Scaffold source in `xsettlers_mcp/tools/`.

---

# Visibility Rules

These are the four authoritative rules governing what a player can see. All view models and renderers must respect them.

1. **Occupation** — An org physically present in a sector grants full, current detail on that sector. The player's `confidence` in that sector is pinned at 100 as long as any of their orgs occupies it.
2. **Scan** — Executed by a pod on `scan` mission at end of turn, granting full detail on the target sector at the moment of resolution. Confidence starts decaying from that point forward per the normal fog decay rate (`CONFIDENCE_DECAY_PER_TURN`).
3. **Fog decay — blink-out** — Any sector not currently occupied loses a flat `CONFIDENCE_DECAY_PER_TURN` points each turn (default 20). At confidence = 0 the sector **leaves the player's view entirely**. There is no degraded "last known" ghost indicator: a sector the player has stopped confirming becomes indistinguishable from one they never visited. The `player_sectors` row survives as history, but every player-facing read filters `confidence > 0`. At the default rate that is five turns from last sighting to gone. (This reverses the earlier ghost-memory design, decided 2026-07-30; see [Data Model & Storage Design](data_model_and_storage_design.md) for why the decay is subtractive rather than proportional.)
4. **Rival detection** — If a rival org is within the player's scan range at the time of a scan, that org's presence is surfaced with **high priority** in the player view — distinct from ordinary sector data but not a hard alert system.

> **Closed roster rule applies here too:** Player visibility is always computed relative to a fixed player set. There is no concept of an anonymous or guest observer in the player view.

---

# Scan Action Design

> **Superseded: scanning is no longer a player-callable action.** The
> `scan_sector` tool described below was never built as such. Scanning is a
> *pod mission*: a player sets a pod to `scan` with a target coordinate
> (`set_pod_mission` / `set_pod_scan_target` in
> `xsettlers_mcp/tools/organization_tools.py`), and the engine resolves it at
> end of turn (`engine/turn.py`, step 3). The range rule, the reveal, and the
> confidence stamp below all still describe what happens — only the trigger
> moved from an immediate player call to end-of-turn resolution. `get_scan_range()`
> is real and lives where this section says.

## `scan_sector(org_id, target_sector_id)` — *design-only, see note above*

**Location:** `xsettlers_mcp/tools/sector_tools.py`

**Scan range:** Fixed at 1 sector (Euclidean distance ≤ 1) for the POC. Range is derived from `get_scan_range(org_id)`, which currently returns the constant `1`.

> **Future hook — sensor pods:** Scan range will eventually be derived from the org's pod manifest. A future `sensor` pod type will contribute range increments, and `get_scan_range(org_id)` will query the pod table instead of returning a constant. Do NOT hard-code `1` at the call site — always call `get_scan_range()`.

**Flow:**

1. Validate that `target_sector_id` is within `get_scan_range(org_id)` of the org's current sector. Return error if not.
2. Write-ahead: log `sector.scanned` event BEFORE mutating state.
3. Upsert `player_sectors` row: `(player_id, target_sector_id, confidence=100)`.
4. Query for rival orgs in the scanned sector. For each found, write an `alert.rival_detected` event.
5. Return the full sector view model for the scanned sector, plus an `alerts` list (may be empty).

**Event types introduced:**

* `sector.scanned` — player delta; payload: `{ org_id, sector_id, scan_range_used }`
* `alert.rival_detected` — engine delta; payload: `{ scanning_org_id, rival_org_id, sector_id }`

## `get_scan_range(org_id) -> int`

**Location:** `xsettlers_mcp/tools/sector_tools.py`

```python
def get_scan_range(org_id: int) -> int:
    """
    Returns the scan range for an org.
    POC: always returns 1.
    Future: query pods for sensor type and sum range contributions.
    """
    return 1
```

---

# View Modes

Two modes. One view model builder per subject type. Renderers are separate and consume the view model dict.

## Debug / Designer View

* Unfiltered. Full DB fidelity.
* Shows all orgs in a sector regardless of ownership or fog state.
* Shows raw confidence scores, all resource levels, transit ETAs, pod internals.
* Used during development and troubleshooting.
* Natural renderer: CLI (`views/cli_renderer.py`).

## Player View

* Filtered by `player_id` and confidence.
* Sectors with confidence = 0 are invisible.
* Own orgs: full detail.
* Rival orgs: presence indicated if in a currently visible sector; no internal detail.
* Resource levels in unoccupied sectors reflect the last scan, not current state.
* Natural renderer: Slack MCP response (`views/slack_renderer.py`).

---

# View Model Schemas

Each `build_*_view()` function returns a plain dict. The renderer decides how to format it.

## Ship View — `build_ship_view(org_id, player_id=None)`

```python
{
  "org_id": int,
  "name": str,
  "org_type": "ship",
  "status": "docked" | "in_transit",       # docked = sector_id != -1
  "sector_id": int | None,                  # None if in transit
  "sector_coords": [x, y, z] | None,        # None if in transit
  "dest_sector_id": int | None,             # set if in_transit
  "arrival_turn": int | None,               # set if in_transit
  "mission": str,
  "pods": [
    {
      "pod_id": int,
      "pod_type": str,
      "task": str,
      "storage_current": float,
      "storage_capacity": float,
      "energy_consumption": float,
      "food_consumption": float
    }
  ],
  # Debug-only fields (omitted in player view):
  "_debug": {
    "player_id": int,
    "is_mobile": int,
    "mission_params": str | None
  }
}
```

> **Implemented starting point, ahead of the view-model layer below:** the
> `build_ship_view` architecture on this page is still design, not code — no
> `build_*_view()` function exists. `views/` itself does now exist, but holds
> only `render.py` (`render_status()` + `render_map()`), which renders tool
> responses directly off their `display` hints rather than off a view model.
> In the meantime, `show_organization`
> (`xsettlers_mcp/tools/organization_tools.py`) already returns a locked,
> ready-to-render cargo table via its `display` block: one row per task
> (not per pod) with columns `Task, Count, Energy, Food, Goods, Capacity`,
> Capacity shown as `current/total` (e.g. `"200/200"`), plus a header line
> (`"<name> — at (x,y,z), <mission>"`). This is a deliberate MVP baseline,
> not a final design — expect it to be superseded once the card/renderer
> architecture below is actually built.

## Colony View — `build_colony_view(org_id, player_id=None)`

```python
{
  "org_id": int,
  "name": str,
  "org_type": "colony",
  "sector_id": int,
  "sector_coords": [x, y, z],
  "mission": str,
  "pods": [
    {
      "pod_id": int,
      "pod_type": str,
      "task": str,
      "storage_current": float,
      "storage_capacity": float,
      "production_per_turn": float,           # from engine/production.py
      "energy_consumption": float,
      "food_consumption": float
    }
  ],
  # Debug-only:
  "_debug": {
    "player_id": int,
    "mission_params": str | None
  }
}
```

## Sector Focal Point View — `build_sector_view(sector_id, player_id=None)`

```python
{
  "sector_id": int,
  "coords": [x, y, z],
  "confidence": int,                          # 0–100; None in debug mode (unfiltered)
  "is_occupied_by_player": bool,               # True if player has an org here
  "energy_capacity": float,
  "food_capacity": float,
  "goods_capacity": float,
  "own_orgs": [                                 # orgs belonging to this player
    { "org_id": int, "name": str, "org_type": str, "mission": str }
  ],
  "rival_orgs": [                               # only shown if confidence > 0 and rival present
    { "org_id": int, "presence": True }         # player view: presence only, no detail
    # debug view: full org dict
  ],
  "neighbors": [                                # sectors within scan range 1
    { "sector_id": int, "coords": [x, y, z], "confidence": int | None }
  ],
  "alerts": [                                    # active unacknowledged alerts for this sector
    { "event_type": str, "payload": dict, "turn": int }
  ],
  # Debug-only:
  "_debug": {
    "all_orgs": [ ... ]                          # unfiltered org list
  }
}
```

---

# Neighborhood Map — built

Unlike the card/renderer architecture below, this one exists:
`show_sector_neighborhood()` (`xsettlers_mcp/tools/sector_tools.py`) builds it
and `render_map()` (`views/render.py`) draws it. Built 2026-07-30.

## Why the server pre-renders the grid

`xsettlers_mcp/server.py` returns `str(fn(**arguments))` — the raw dict goes
over the wire and the *client* decides how to draw it. That is fine for tables:
an LLM renders a list of row-dicts consistently enough. It is not fine for a
map. Ask three clients to draw a grid from a sector list and you get three
different grids, and on a phone the failure mode is a wall of coordinates.

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
  `set_pod_scan_target` with no arithmetic.
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

# Rendering Contract

The view model layer is **renderer-agnostic**. Each renderer receives a view model dict and formats it independently.

## CLI Debug Renderer — `views/cli_renderer.py`

```python
def render_ship(view: dict) -> str: ...
def render_colony(view: dict) -> str: ...
def render_sector(view: dict) -> str: ...
```

* Plain text tables, monospace alignment.
* Shows `_debug` fields.
* No fog filtering — confidence shown as raw number.
* Suitable for `python -m views.cli_renderer --sector 3` style invocation.

## Slack Player Renderer — `views/slack_renderer.py`

```python
def render_ship(view: dict) -> str: ...
def render_colony(view: dict) -> str: ...
def render_sector(view: dict) -> str: ...
```

* Emoji-annotated, human-readable Slack message text.
* Omits `_debug` fields entirely.
* Fog-aware: if confidence < 100, appends a staleness indicator (e.g. `:fog: Last seen: turn N`).
* Alerts rendered as `:rotating_light: ALERT` blocks at the top of the sector view.
* Designed to be returned directly from MCP tool responses.

---

# Org Card — UI Spec

## Card Design

Each organization (ship or colony) is represented as a **card** — a self-contained rectangular unit sized proportionally to a playing card.

> **Adaptive sizing rule:** Cards are NOT fixed at a pixel dimension. Use `aspect-ratio: 5/7` with `min-width: 180px` and `max-width: 280px`. All internal spacing, font sizes, and bar heights use relative units (`rem`, `%`) so the card scales correctly across display sizes. The playing-card feel comes from the aspect ratio, not pixel counts.

**Card anatomy (top to bottom):**

1. **Header** — org name, type icon (ship vs colony), status indicator dot (green = idle/docked, amber = in transit)
2. **Location line** — sector coords `(x, y)` or `In Transit → (x, y)` with arrival turn
3. **Mission line** — current mission string
4. **Resource bars** — one bar per pod type: `energy`, `factory`, `farm`. Each bar shows:
    1. A fill bar representing `aggregate_storage_current / aggregate_storage_capacity` for that pod type
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

* `views/cli_renderer.py` — implement all three render functions (ship, colony, sector)
* `views/slack_renderer.py` — implement all three render functions
* `views/html_renderer.py` — implement `render_org_card(view: dict) -> str` returning hydrated card HTML; consume adaptive card spec above
* **Rival detection is not built.** `engine/turn.py`'s scan resolution still reads `# TODO: emit pod.scanned event; detect rivals` — no `alert.rival_detected` event is ever emitted, and there is no sighting history in the schema. Because rival positions can therefore only be read live from `organizations`, the neighborhood map restricts rival reporting to sectors at confidence 100 (see Cell vocabulary above). Real sighting storage would let rivals surface on decayed cells honestly, stamped with the turn they were last seen.
* `tests/test_renderers.py` — cover the card/view-model renderers once they exist: debug view shows `_debug` fields; player view omits them; resource bar fill math correct
* **Future:** `sensor` pod type + variable `get_scan_range(org_id)` — wire up when sensor pods are designed
* **Future:** SVG map renderer — `views/svg_renderer.py`; same view model contract, different output format
* **Future:** whole-known-map view. `player_sectors` already *is* the global known-sectors store and `get_sector_map()` already reads it, and `render_map()` is written against a viewport (center + radius + known cells) rather than against "neighborhood" specifically — so the same renderer draws it. What is genuinely unbuilt is the width problem: `game0` puts home sectors 25 apart, so a full known-map bounding box exceeds what a markdown table shows readably on a phone. Needs downsampling or paging first.
* **Future:** Column config persistence — save/load named column layouts per player
* **TDD rule**: no new function without a corresponding test entry
