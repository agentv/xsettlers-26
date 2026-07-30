# XSettlers — UI & Rendering Design

**Scope:** Defines how game state is translated into player-facing output. Contains visibility rules, view model schemas, scan action design, and rendering contracts for debug and player modes. No DB schema here — see [Data Model & Storage Design](data_model_and_storage_design.md). No tool implementations here — see the MCP Tools Scaffold source in `mcp/tools/`.

---

# Visibility Rules

These are the four authoritative rules governing what a player can see. All view models and renderers must respect them.

1. **Occupation** — An org physically present in a sector grants full, current detail on that sector. The player's `confidence` in that sector is pinned at 100 as long as any of their orgs occupies it.
2. **Scan** — A deliberate player action (`scan_sector`) grants full detail on the target sector at the moment of the scan. Confidence starts decaying from that point forward per the normal fog decay rate (`CONFIDENCE_DECAY`).
3. **Fog decay** — Any sector not currently occupied loses confidence each turn. At confidence = 0, the sector is **not removed** from the player's view — instead a degraded "last known" ghost indicator is shown, signaling that the player once had knowledge of this sector but the information is now fully stale.
4. **Rival detection** — If a rival org is within the player's scan range at the time of a scan, that org's presence is surfaced with **high priority** in the player view — distinct from ordinary sector data but not a hard alert system.

> **Closed roster rule applies here too:** Player visibility is always computed relative to a fixed player set. There is no concept of an anonymous or guest observer in the player view.

---

# Scan Action Design

## `scan_sector(org_id, target_sector_id)`

**Location:** `mcp/tools/sector_tools.py`

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

**Location:** `mcp/tools/sector_tools.py`

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
> `build_ship_view`/renderer architecture on this page is still design, not
> code — `views/` doesn't exist yet. In the meantime, `show_organization`
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

* `views/cli_renderer.py` — implement all three render functions (ship, colony, sector)
* `views/slack_renderer.py` — implement all three render functions
* `views/html_renderer.py` — implement `render_org_card(view: dict) -> str` returning hydrated card HTML; consume adaptive card spec above
* `mcp/tools/sector_tools.py` — implement `scan_sector()` and `get_scan_range()` stubs
* `mcp/server.py` — add `scan_sector` to `list_tools()` and `call_tool()` dispatch
* `tests/test_scan.py` — cover: range validation, confidence upsert, rival detection surfacing, no-write on out-of-range attempt
* `tests/test_renderers.py` — cover: debug view shows `_debug` fields; player view omits them; ghost indicator appears at confidence = 0; resource bar fill math correct
* **Future:** `sensor` pod type + variable `get_scan_range(org_id)` — wire up when sensor pods are designed
* **Future:** SVG map renderer — `views/svg_renderer.py`; same view model contract, different output format
* **Future:** Column config persistence — save/load named column layouts per player
* **TDD rule**: no new function without a corresponding test entry
