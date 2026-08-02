# XSettlers — Product Requirements

# Overview

XSettlers is a multiplayer, turn-based space strategy game played through any MCP-speaking client (Slack is the intended home, but nothing in the server is Slack-specific). Players manage organizations (ships and colonies) across a 3D sector map, competing to expand territory, produce resources, and outlast rivals.

---

# Players & Game Instance

* A game instance is defined by a single `game_config.yaml` file and a corresponding SQLite/SpatiaLite database.
* The player roster is **fixed at bootstrap time**. There is no mechanism for joining a game already in session.
* **The service is a library of games, not one game.** `config/game_config.yaml` holds a service-wide player *directory* (identity and credential, one entry per person); each scenario file declares its own `participants` — which directory players are seated in it, and where each starts. A player's token is an invitation to the specific games they are seated in, not to the whole library. Player count is therefore a property of the scenario: solo, two-player, and five-player games differ only in YAML.
* Each player is identified by a Slack user ID. Authentication is handled at the MCP layer.
* Roster size is per-scenario, bounded by the engine-wide `max_players` ceiling (currently 8). The shipped scenarios are two-player (`game0`, `game1`) and one-player (`game_solo`). Concurrent multi-game — several games live at once, rather than one per deployment — is still future scope.

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
* A pod has no intrinsic type — it is defined entirely by its **task**.
* Valid pod tasks: `idle`, `produce_energy`, `produce_food`, `produce_goods`, `scan`.
* **Pods have `task`; organizations have `mission`.** Two different concepts, two different words, deliberately (renamed 2026-07-31 — one word for both was actively misleading in status reports, where an idle *ship* sat above a table of busy *pods*).
* The colloquial names *energy*, *farm*, and *factory* map to the `produce_energy`, `produce_food`, and `produce_goods` tasks respectively — useful in UI and narrative, not a data field.
* Each pod has generic storage (`storage_capacity` plus `energy_stored`/`food_stored`/`goods_stored`) — a pod holds any mix of resources regardless of its own task, so retasking never hides or relabels existing cargo.
* Pods execute their task every turn regardless of whether their parent org is in transit or stationary, with two exceptions in transit: energy cannot be harvested (no sector to harvest from) and scans do not report.

## Pod Tasks — Full Roster

The following pod tasks are defined in the game model. Only a subset are active in any given game instance; the rest are deferred for future implementation.

| Pod Task | Colloquial Name | Status | Description |
|---|---|---|---|
| `idle` | — | **Active** | Pod does nothing. Default state. |
| `produce_energy` | energy | **Active** | Harvests energy from the organization's current sector — the only resource drawn from the map, and the sector depletes as it is taken. Consumes food. |
| `produce_food` | farm | **Active** | Manufactures food each turn from stored resources — **not** sector-sourced. Consumes energy and goods. |
| `produce_goods` | factory | **Active** | Manufactures goods each turn from stored resources — **not** sector-sourced. Consumes energy and food. The slowest to produce and the highest-scoring. |
| `scan` | scanner | **Active** | Scans one sector at end of turn, aimed by bearing via `set_pod_scan_bearing`. Consumes food. Suppressed (but still charged) while in transit. |
| `crew` | crew | Deferred | General-purpose pod. Flexible but less productive than specialized pods. |
| `cargo` | cargo | Deferred | Stores goods, energy, and food. Does not consume energy but consumes food for its crew. |
| `defend` | defense | Deferred | Absorbs damage from attackers. Requires combat system. |
| `attack` | attack | Deferred | Attacks other ships and colonies. Requires combat system. |

### Rates (per pod, per turn)

Retuned 2026-08-02. `engine/production.py` is authoritative; this table is a
convenience.

| Task | Produces | Costs |
|---|---|---|
| `produce_energy` | 6 energy | 1 food |
| `produce_food` | 5 food | 1 energy, 1 goods |
| `produce_goods` | 3 goods | 2 energy, 1 food |
| `scan` | — | 2 energy, 1 food |
| `idle` | — | — |

Plus **organization upkeep of 5 food + 1 energy per org per turn**, charged
once regardless of pod count or transit state, and drawn *before* pods run —
so upkeep gets first claim on the stock.

Inputs are drawn from the organization's pooled stock across all its pods, and
output is **prorated** to whatever fraction of the required input is actually
available: half the energy on hand yields half the output, rather than an
all-or-nothing gate. One consequence worth knowing: **a scanner needs energy**,
so an organization that runs dry goes blind as well as idle.

**POC active tasks:** `idle`, `produce_energy`, `produce_food`, `produce_goods`, `scan`. All other entries above are recognized in the data model but are not instantiated in any current game instance.

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
* **Fog decay**: unoccupied sectors lose a flat number of confidence points each turn (`CONFIDENCE_DECAY_PER_TURN`, default 20 — see [Data Model & Storage Design](data_model_and_storage_design.md) for why it is subtractive rather than proportional). At confidence = 0 the sector **blinks out**: it leaves the player's map entirely, with no degraded "last known" indicator. The underlying row is retained as history, but nothing player-facing shows it. At the default rate, a sector you stop confirming is gone on the fifth turn.
* **Rival detection**: if a rival org is present in the scanned sector at time of resolution, that information is surfaced with high priority in the player view.

---

# Scanning

* Scanning is performed at end of turn by an organization's own sensors and by any pods on the `scan` task — it is not a discrete player action.
* A scanner must be **aimed** before end of turn for the scan to execute — via `set_org_scan_bearing` for an organization's own sensors, or `set_pod_scan_bearing` for a pod on the `scan` task. An unaimed scanner still pays its food cost and reveals nothing.
* Scan range is **fixed at 2 sectors** (Euclidean distance ≤ 2), derived from `get_scan_range(org_id)` — always call it, never hard-code the number. Raised from 1 on 2026-07-31: under a Euclidean metric, range 1 reaches only the four orthogonal neighbours, because a diagonal is √2 ≈ 1.41. Being refused a scan of the sector diagonally adjacent reads as broken. Range 2 reaches **12 sectors** — 4 orthogonal, 4 diagonal, 4 two-out orthogonal — the smallest radius at which scanning behaves the way a player expects.
* **A scan is aimed by a bearing relative to the scanner, not by absolute coordinates** (2026-07-31). Sensors are mounted on the thing that carries them: they look a fixed direction and distance from wherever it currently is, and a ship flying away from a sector does not keep seeing it. Two consequences: a scan pattern survives a move with no re-aiming, and range becomes a permanent property of the aim rather than something that can silently stop being true — so an out-of-range aim is **rejected when set** instead of failing at resolution.
* **Bearings**: `N NE E SE S SW W NW` (distance 1 or √2) and `N2 E2 S2 W2` (distance 2) — the 12 names map exactly onto the 12 sectors reachable at range 2. Explicit `offset_x/y/z` is always available for anything the table doesn't name (including off-plane targets). **North is −y**, matching the neighborhood map's rendering; arbitrary but fixed.
* **Scanning is scanning.** An organization's own sensors and a scan pod's follow identical rules — same bearings, same cost, same range, same suppression in transit. The only difference is what carries the equipment.
* **A scan reveals the targeted sector only — no halo, no surrounding ring.** Decided 2026-07-31, after considering and rejecting a radius-5 halo. If scanning is to be a meaningful activity with a real cost, it must not also be cheap area coverage: one pod-turn plus its food buys one sector of knowledge, and the player chooses which. Range says how far you can *reach*; it does not widen what you *get*.
* If the designated target sector is **out of range** at end-of-turn resolution, the scan does not execute and the player receives an alert. The food cost is still paid (see `docs/TODO.md`).
* Ships in transit cannot scan — the reveal is suppressed for the duration of transit.
* **Future:** range becomes variable per org once sensor pods exist. The `get_scan_range()` hook is already in place.

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

### Set Pod Task (`set_pod_task`)

Assigns a task to a pod belonging to the player's organization. Valid tasks: `idle`, `produce_energy`, `produce_food`, `produce_goods`, `scan`. Tasks take effect immediately and persist until changed. Pods continue running their task regardless of whether their parent ship is in transit (except `scan`, whose reveal is suppressed during transit, and `produce_energy`, which has no sector to draw from).

For `task = scan`, an aim may optionally be supplied in the same call as either a compass `bearing` or an explicit `offset_x/y/z`. If omitted, the pod takes the scan task with no aim — `set_pod_scan_bearing` must follow before end of turn, or the pod pays its food cost and reveals nothing.

### Set Pod Scan Bearing (`set_pod_scan_bearing`)

Aims a pod already on the `scan` task. See [Scanning](#scanning) for the bearing vocabulary. An out-of-range aim is **rejected when set** rather than at resolution: an aim is an offset, so its range is fixed and cannot drift.

### Rename Organization (`rename_organization`)

Gives one of the player's own ships or colonies a name of their choosing (max 24 characters). Names must be unique among that player's own organizations, case-insensitively — a name is how a player issues an order, so an ambiguous one is not a name. Uniqueness is per player, not global. Defaults are `S1`…`Sn` for ships and `C1` for a colony.

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

### Set Organization Scan Bearing (`set_org_scan_bearing`)

Aims an organization's **own** sensors. Every ship and colony can scan one sector per turn on its own account — a ship's bridge, a colony's headquarters — without dedicating a pod to it. Identical in every rule to a scan pod: same food cost, same range, same transit suppression. An organization that also carries scan pods gets both, and each pays its own way.

### Set Pod Scan Bearing (`set_pod_scan_bearing`)

Aims a pod already on the `scan` task. Same vocabulary and same rules as the organization's own sensors — scanning is scanning, whoever carries the equipment.

Both take either a compass `bearing` or an explicit `offset_x/y/z`, and passing neither clears the aim (and stops paying for it).

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

Renders the neighborhood around a center point as a ready-to-draw map. The center may be specified as either:

* an `org_id` — the neighborhood is centered on that organization's current sector (the normal way to call it: "show me what's around this ship")
* a coordinate triple `(x, y, z)` — centered on an arbitrary point in space

Default radius 5, giving an 11×11 bounding square (max 10). Ships in transit (at the sentinel sector) have no location and cannot serve as an `org_id` center — callers must supply explicit coordinates in that case.

Unlike `get_sector_map`, which returns a bare list of what the player knows, this returns the **complete lattice** — every coordinate in range, including ones never visited. An unvisited cell has no `sectors` row at all under the lazy-reveal model, so the grid is synthesized from center and radius and known sectors are overlaid onto it. This is deliberate: a cell you have never seen is the most actionable thing on the map, since it is where a scan pod should go.

It is a **pure view** — it reveals nothing, costs nothing, and changes no confidence. `reveal_sector()` remains the only writer.

Cell markers are at most 3 characters (see [UI & Rendering Design](ui_and_rendering_design.md) for the full vocabulary and the rendering contract). Confidence deliberately does not appear on the grid: under blink-out a sector is either still on the map or gone from it, so the cell is binary and the numeric figure belongs in the accompanying detail rows.

**Rival presence is reported only for sectors at confidence 100** — ones the player currently occupies. Rival positions are read live from `organizations` (there is no sighting history in the schema), so surfacing them on a decayed cell would hand the player current intelligence about a sector they last looked at many turns ago. Occupation already grants full current detail, so this restriction is leak-free.

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
