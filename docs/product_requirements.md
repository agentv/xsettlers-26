# XSettlers — Product Requirements

# Overview

XSettlers is a multiplayer, turn-based space strategy game played entirely through Slack via an MCP server. Players manage organizations (ships and colonies) across a 3D sector map, competing to expand territory, produce resources, and outlast rivals.

---

# Players & Game Instance

* A game instance is defined by a single `game_config.yaml` file and a corresponding SQLite/SpatiaLite database.
* The player roster is **fixed at bootstrap time**. There is no mechanism for joining a game already in session.
* Each player is identified by a Slack user ID. Authentication is handled at the MCP layer.
* For the POC, two players are supported. Multi-game and larger rosters are future scope.

---

# The Map

* The game world is a 3D grid of **sectors**, each with integer coordinates `(x, y, z)`.
* Sectors have resource capacities: **energy**, **food**, and **goods**.
* Sector layout and resource values are defined in `game_config.yaml` and seeded at bootstrap.
* A **sentinel sector** (`id = -1`, coords `(-1,-1,-1)`) serves as the transit state for ships currently moving between sectors. Players are informed when their ship is in the sentinel sector — it is not hidden.
* **Origin sector:** `(0,0,0)` is the origin and is extraordinarily resource-rich.
* **Energy barrier / playable boundary:** all sectors with any negative coordinate are inaccessible. The energy barrier is defined by the origin — only sectors where `x ≥ 0`, `y ≥ 0`, and `z ≥ 0` are playable. Ships and colonies cannot be placed in negative-coordinate space.

---

# Organizations

Players control **organizations** — the fundamental unit of agency. Two types:

## Ships

* Mobile. Can move between sectors.
* Carry **pods** that perform missions each turn.
* Have an **org mission** (`idle`, `move`, `colonize`, `defend`, `attack`).
* While in transit, parked at the sentinel sector. Pod behavior during transit:
    * **Produce pods** (`produce_energy`, `produce_food`, `produce_goods`) — operate normally.
    * **Scan pods** — suppressed for the duration of transit; cannot scan.
    * **All pods** — consume resources (energy, food) normally regardless of transit state.

## Colonies

* Stationary. Cannot move once established.
* Produce resources each turn based on pod missions and sector capacities.
* Created when a ship with org mission `colonize` completes its turn in a sector.

---

# Pods

* Each organization carries one or more **pods**.
* A pod has no intrinsic type — it is defined entirely by its **mission**.
* Valid pod missions: `idle`, `produce_energy`, `produce_food`, `produce_goods`, `scan`.
* The colloquial names *energy*, *farm*, and *factory* map to the `produce_energy`, `produce_food`, and `produce_goods` missions respectively — useful in UI and narrative, not a data field.
* Each pod has storage (`storage_current`, `storage_capacity`), `energy_consumption`, and `food_consumption`.
* Pods execute their mission every turn regardless of whether their parent org is in transit or stationary.

## Pod Missions — Full Roster

The following pod missions are defined in the game model. Only a subset are active in any given game instance; the rest are deferred for future implementation.

| Pod Mission | Colloquial Name | Status | Description |
|---|---|---|---|
| `idle` | — | **Active** | Pod does nothing. Default state. |
| `produce_energy` | energy | **Active** | Produces energy each turn. Powers other pods and contributes to movement capacity. |
| `produce_food` | farm | **Active** | Produces food each turn based on sector `food_capacity`. Consumes energy but not food. |
| `produce_goods` | factory | **Active** | Produces goods each turn based on sector `goods_capacity`. Consumes energy and food. Goods are required to build new pods or convert ships to colonies. |
| `scan` | scanner | **Active** | Scans a designated adjacent sector at end of turn. Requires a scan target to be set via `set_pod_scan_target`. Ships in transit cannot scan. |
| `crew` | crew | Deferred | General-purpose pod. Flexible but less productive than specialized pods. |
| `cargo` | cargo | Deferred | Stores goods, energy, and food. Does not consume energy but consumes food for its crew. |
| `defend` | defense | Deferred | Absorbs damage from attackers. Requires combat system. |
| `attack` | attack | Deferred | Attacks other ships and colonies. Requires combat system. |

**POC active missions:** `idle`, `produce_energy`, `produce_food`, `produce_goods`, `scan`. All other missions are recognized in the data model but are not instantiated in any current game instance.

**Platform note:** The original XSettlers architecture (xsettlers-game-papi) was designed as a Mule application with a System/Process/Experience API layer and an HTML5/D3 front end. That architecture is superseded. The current stack is **Python · SpatiaLite · MCP SDK · Slack** — see [MCP Server Layer Design](mcp_server_layer_design.md) for the current design.

---

# Movement

* Movement is a **two-step confirmation flow**: `preview_move` → `confirm_move`.
* `preview_move` is read-only: calculates travel time, no DB writes.
* `confirm_move` commits the move: parks the ship at the sentinel sector and queues an arrival.
* Travel time = `ceil(Euclidean distance / jump_range_per_turn)`, minimum 1 turn.
* `cancel_move` is available while a ship is in transit. Cancellation **rubber-bands** the ship to its `origin_sector_id` — no partial credit for distance traveled.

---

# Visibility & Fog of War

* **Occupation**: an org present in a sector pins confidence at 100. Full detail shown.
* **Scan**: executed by a pod with `scan` mission at end of turn. Grants full detail for the targeted sector. Confidence decays from that point.
* **Fog decay**: unoccupied sectors lose confidence each turn. At confidence = 0, the sector is not removed — instead a degraded "last known" indicator is shown, signaling that the player once had knowledge of this sector but it is now stale.
* **Rival detection**: if a rival org is present in the scanned sector at time of resolution, that information is surfaced with high priority in the player view.

---

# Scanning

* Scanning is performed by pods assigned the `scan` mission — it is not a discrete player action.
* A scan pod must have a **scan target** set via `set_pod_scan_target(pod_id, sector_id)` before end of turn for the scan to execute.
* Scan range is **fixed at 1 sector** (Euclidean distance ≤ 1) for the POC. The target sector must be adjacent.
* Range is always derived from `get_scan_range(org_id)`, which currently returns the constant `1`.
* If the designated target sector is **out of range** at end-of-turn resolution, the scan does not execute and the player receives an alert.
* Ships in transit cannot scan — scan pod missions are suppressed for the duration of transit.
* **Future:** scan range will expand (target: 12 sectors). The `get_scan_range()` hook is already in place.

---

# Turn Structure

* The game advances in **turns**. The current turn is stored in `game_state`.
* A player **declares end of turn** when they have no further moves. Once all players have declared, the turn resolves (consensus acceleration).
* Players may **rescind** their end-of-turn declaration before resolution.
* End-of-turn engine actions (in order):
    * Player declarations reset
    * Arrivals processed
    * Pod consumption then production (all pods: resources consumed first, then output produced; scan resolution for stationary orgs)
    * Mission dispatch (colonize, defend, attack)
    * Fog decayed
    * **Holdings calculated** — resource totals snapshotted across all orgs and pods
    * Turn counter incremented

**Holdings are always calculated after all processing is complete** — never before. This ensures the snapshot reflects the true end state of the turn, including all production, arrivals, and mission outcomes.

---

# Player Actions

This section is the canonical design-authority inventory of every action a player can take within a game session. For implementation signatures, see `mcp/tools/`.

---

## Mission & Pod Configuration

### Set Mission (`set_mission`)

Assigns a mission to one of the player's organizations. Valid missions are: `idle`, `move`, `colonize`, `defend`, `attack`. Colonies cannot be assigned the `move` mission. Setting a ship's mission to `colonize` causes it to convert to a colony at end of turn if it remains in the target sector.

### Set Pod Mission (`set_pod_mission`)

Assigns a mission to a pod belonging to the player's organization. Valid missions: `idle`, `produce_energy`, `produce_food`, `produce_goods`, `scan`. Pod missions take effect immediately and persist until changed. Pods continue running their mission regardless of whether their parent ship is in transit (except `scan`, which is suppressed during transit).

When `mission = scan`, a `target_sector_id` may optionally be supplied in the same call. If omitted, the pod enters scan mission with no target — the player must follow up with `set_pod_scan_target` before end of turn for the scan to execute.

### Set Pod Scan Target (`set_pod_scan_target`)

Assigns or changes the scan target sector for a pod already in `scan` mission. Can be called independently at any time before end of turn. The target sector must be within scan range (POC: Euclidean distance ≤ 1) at the time of end-of-turn resolution — not at the time of assignment. If the target is out of range at resolution, the scan does not execute and the player is alerted.

---

## Movement

### Preview Move (`preview_move`)

Read-only. Calculates the travel time and projected arrival turn for a proposed move without committing anything to state. No DB write, no event logged. Use this before confirming to let the player make an informed decision.

### Confirm Move (`confirm_move`)

Commits a previewed move. Parks the ship at the sentinel sector (`sector_id = -1`), sets its mission to `move`, inserts an `arrival_queue` row (recording `origin_sector_id` for rubber-band support), and logs a `ship.move_confirmed` write-ahead event. The ship remains at the sentinel until the engine processes its arrival.

### Cancel Move (`cancel_move`)

Available while a ship is in transit. Rubber-bands the ship back to its `origin_sector_id` — no partial credit for distance traveled. Clears the `arrival_queue` entry, resets mission to `idle`, and logs a `ship.move_cancelled` write-ahead event.

---

## Scanning

### Set Pod Mission to Scan (`set_pod_mission` with `mission=scan`)

Assigns the `scan` mission to a pod. Optionally includes a `target_sector_id` in the same call. If the target is not provided here, `set_pod_scan_target` must be called separately before end of turn.

### Set Pod Scan Target (`set_pod_scan_target`)

Designates the sector to be scanned by a pod in `scan` mission. Can be called any time before end of turn. Scan executes at end of turn if the target is within range; otherwise the player is alerted and no scan occurs.

---

## Turn Control

### Declare End of Turn (`declare_end_turn`)

Signals that the player has no further moves this tick. Once all players have declared, the turn resolves via consensus acceleration.

### Rescind End of Turn (`rescind_end_turn`)

Takes back a previously declared end-of-turn, provided the turn has not yet resolved. Resets the player's `end_turn_declared` flag to false.

---

## View Actions

### Show Organization (`show_organization`)

Returns the complete properties of one of the player's own organizations: `org_type`, `mission`, `mission_params`, `is_mobile`, sector location, and all pods with their `mission` and `mission_params`. Ownership-gated — only the calling player's organizations are accessible via this action.

### Show Sector Neighborhood (`show_sector_neighborhood`)

Returns all sectors within Euclidean distance ≤ 2 of a center point, filtered by the player's fog-of-war (confidence > 0). The center may be specified as either:

* an `org_id` — the neighborhood is centered on that organization's current sector
* a coordinate triple `(x, y, z)` — the neighborhood is centered on an arbitrary point in space

Radius is fixed at 2 for the POC (parameterized in the signature for future flexibility). Ships in transit (at the sentinel sector) cannot serve as a valid `org_id` center — callers must supply explicit coordinates in that case.

### Show Game Status (`show_game_status`)

Returns a player-scoped summary of the current game state. Includes:

* **Turn context** — current turn number and turn limit (e.g. Turn 4 of 20)
* **Organizations** — all of the player's ships and colonies, each with: name, `org_type`, `mission`, and current sector location. Ships currently in transit are marked as *in transit* with their destination sector and expected arrival turn.
* **Accumulated assets** — aggregate resource totals across the entire player portfolio, broken down by energy, food, and goods, plus an overall total. Not broken down per organization.

Ownership-gated — only the calling player's data is returned. Lives in `organization_tools.py`.

# Events (Write-Ahead Log)

* Every state mutation is preceded by an event log entry. No exceptions.
* **Player-action events** (deltas): `ship.move_previewed`, `ship.move_confirmed`, `ship.move_cancelled`, `mission.set`, `pod.mission_set`, `pod.scan_target_set`, `turn.declared`
* **Engine events** (deltas): `ship.arrived`, `ship.colonized`, `pod.produced`, `pod.scanned`, `alert.rival_detected`, `alert.scan_out_of_range`, `fog.decayed`
* **Snapshots**: `turn.snapshot` — full state written at end of each turn for replay and debug.

---

# Rendering & Views

Two view modes, one view model layer:

* **Debug/Designer view** — unfiltered, full DB fidelity. Used during development and troubleshooting. CLI renderer (`views/cli_renderer.py`).
* **Player view** — filtered by `player_id` and confidence. Fog-aware. Rival orgs show presence only, no internals. Slack MCP renderer (`views/slack_renderer.py`).

Three view model types: `build_ship_view()`, `build_colony_view()`, `build_sector_view()`. Each returns an interface-agnostic dict. Renderers consume it independently.

---

# Out of Scope (POC)

* Combat (`defend` / `attack` missions are stubs)
* Resource consumption / deduction
* Multi-game support
* Runtime player join
* Variable scan range (sensor pods)
* SVG map renderer
* Authentication hardening
