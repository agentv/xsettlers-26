# XSettlers — Product Requirements

# Overview

XSettlers is a multiplayer, turn-based space strategy game played through any MCP-speaking client (Slack is the intended home, but nothing in the server is Slack-specific). Players manage organizations (ships and colonies) across a sector map, competing to expand territory, produce resources, and outlast rivals. The map is 3D-capable — sector coordinates and distance math are `(x, y, z)` throughout — but every shipped scenario places everything at `z = 0`; nothing currently plays in three dimensions, only supports it. `game_config.yaml`'s `dimensions: 2` field looks like it governs this but doesn't — it's parsed and never read anywhere in the codebase.

---

# Players & Game Instance

* A game instance is defined by a single scenario file (e.g. `config/game0.yaml`) and a corresponding SQLite/SpatiaLite database — one shared game per deployed instance.
* The player roster is **fixed at bootstrap time**. There is no mechanism for joining a game already in session through xsettlers' own tools (see GameHouse below for the one exception).
* Internally, xsettlers still supports several scenario files side by side (`config/game*.yaml`), each declaring its own `participants` against a service-wide player *directory* in `config/game_config.yaml` — player count is a property of the scenario, not the deployment, and a solo, two-player, or five-player game differs only in YAML. **What xsettlers is no longer responsible for is presenting itself as a browsable library to a person** — that role now belongs to a separate sibling service, **GameHouse** (`../gamehouse`), which owns Person-level identity and lobby matchmaking across potentially many hosted games, xsettlers being one of them. See `docs/TODO.md`'s "GameHouse handoff" section for the current integration. `list_scenarios`/`select_scenario` still work as xsettlers-internal tools and aren't being removed — they're just no longer the only, or the primary, way a game gets started.
* Each player is identified by an opaque `player_token`, not a Slack-specific ID — nothing in the auth path is platform-specific (renamed from `slack_user_id` 2026-07-22). A GameHouse-driven session (see above) generates its own `player_token`s per handoff, entirely separate from `config/game_config.yaml`'s static roster; both paths are live and neither has been retired.
* Roster size is per-scenario, bounded by the engine-wide `max_players` ceiling (currently 8). The shipped scenarios are two-player (`game0`, `game1`) and one-player (`game_solo`). Concurrent multi-game — several games live at once within one xsettlers deployment, rather than one per deployment — is still future scope; GameHouse orchestrates across separately-deployed games, it doesn't make any single xsettlers deployment itself multi-instance.

---

# The Map

* The game world is a grid of **sectors**, each with integer coordinates `(x, y, z)` — the schema and distance math are fully 3D, but every shipped scenario places its participants and their fleets at `z = 0` only (see Overview above).
* A sector has exactly one resource capacity: **energy**. Food and goods are manufactured from stock an organization already holds, never harvested from the map, so there is no per-sector pool of them (`food_capacity`/`goods_capacity` dropped 2026-08-02).
* **Sectors are lazily instantiated and their richness is rolled at discovery** (2026-08-02). No sector row exists until a scan, a ship arrival, or bootstrap placement reveals it via `db/sectors.py`'s `reveal_sector()`. On that first reveal — and only then — its energy is rolled as **400 + d6 × 100**, giving 500 / 600 / 700 / 800 / 900 / 1000 at flat 1-in-6 odds, mean 750. The floor is deliberate: every sector is worth working, so an unlucky roll costs a player upside rather than viability.
* **Home sectors are exempt and effectively bottomless.** Each player's starting sector is seeded flat at `home_sector_energy` (default `HOME_SECTOR_ENERGY` = 100,000, a per-scenario setting) instead of taking the discovery roll — roughly 600 turns of maximum plausible draw, so it does not deplete in any game that will be played. A player's own footing should never be what runs out from under them, and that is precisely what allows the frontier to be lean. **This is the home sector, not the sentinel sector** — see below.
* **The roll belongs to the sector, not to the finder.** Because `reveal_sector()` is a single get-or-create and the sole path for every reveal, whoever discovers a sector first fixes its value, and every later look by anyone — rivals included — reads that established figure, depletion and all. A rival arriving later cannot re-roll your find or refill what you have drawn down. Set `SECTOR_ROLL_SEED` to make discovery reproducible for experiments and tests.
* A **sentinel sector** (`id = -1`, coords `(-1,-1,-1)`) serves as the transit state for ships currently moving between sectors. It has 0 energy capacity, which is *how* transit suppresses energy harvesting — there is no special-case branch, so giving the sentinel any energy would silently delete transit stress from the game. Not to be confused with a **home sector**, which is a real playable sector at a scenario's starting coordinates. Players are informed when their ship is in the sentinel sector — it is not hidden.
* **Origin sector:** `(0,0,0)` is intended to be extraordinarily resource-rich. **Not implemented** — it currently takes the same d6 roll as anywhere else.
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
* Produce resources each turn based on pod tasks and sector capacities, at **1.5× a ship's rate** (`COLONY_PRODUCTION_MULTIPLIER`) — see [Rates](#rates-per-pod-per-turn). This is the whole mechanical payoff for colonizing.
* Created 3 turns after a ship is given org mission `colonize` (scheduled via a `colonize_complete` event at the moment the mission is set, resolved by the engine 3 turns later) — not on the same turn the mission is set, which the sentence below's "3-turn transition" already implies but this line previously didn't state outright.
* **Colonizing costs 30 energy** (`COLONIZATION_ENERGY_COST`), charged in full from the ship's pooled stock at the moment the mission is set — not spread over the 3-turn transition. Unlike every other cost in the economy this is an all-or-nothing gate rather than a prorated draw: a ship that cannot pay is refused and left untouched, since there is no such thing as half a conversion. The figure is provisional and expected to move with play data.

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
| `produce_goods` | 2 goods | 3 energy, 1 food |
| `scan` | — | 2 energy, 1 food |
| `idle` | — | — |

Plus **organization upkeep of 5 food + 3 energy per org per turn**, charged
once regardless of pod count or transit state, and drawn *before* pods run —
so upkeep gets first claim on the stock. That ordering matters at the margin:
once energy is thin enough that upkeep alone consumes it, production stops
outright rather than tapering, so a fleet that runs its sector dry fails
abruptly rather than gradually.

Inputs are drawn from the organization's pooled stock across all its pods, and
output is **prorated** to whatever fraction of the required input is actually
available: half the energy on hand yields half the output, rather than an
all-or-nothing gate. One consequence worth knowing: **a scanner needs energy**,
so an organization that runs dry goes blind as well as idle.

**Colonies multiply output by 1.5** (`COLONY_PRODUCTION_MULTIPLIER`, added
2026-08-02). Every pod aboard a colony produces 1.5× its base rate; ships
produce 1.0×. The multiplier applies to output only — costs and upkeep are
identical to a ship's — and it applies to the sector draw as well as to what
lands in storage, so a colony harvesting energy strips its sector 1.5× as
fast. Because costs are fixed, the effect on the *margin* is much larger than
1.5×: a 6-pod org netting +1 energy / +1 food / +2 goods a turn as a ship nets
+7 / +6 / +4 as a colony, roughly tripling its score rate.

**POC active tasks:** `idle`, `produce_energy`, `produce_food`, `produce_goods`, `scan`. All other entries above are recognized in the data model but are not instantiated in any current game instance.

**Platform note:** The original XSettlers architecture (xsettlers-game-papi) was designed as a Mule application with a System/Process/Experience API layer and an HTML5/D3 front end. That architecture is superseded. The current stack is **Python · SpatiaLite · MCP SDK**, served over streamable HTTP to any MCP-speaking client — Slack was the original intended client but identity and the transport are both client-agnostic (see [Overview](#overview)), not Slack-specific. See [MCP Server Layer Design](mcp_server_layer_design.md) for the current design.

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
* **Rival detection**: if a rival org is present in the scanned sector at time of resolution, that information is surfaced with high priority in the player view. **Not implemented** — no `alert.rival_detected` (or equivalent) event exists anywhere in the codebase, verified by grep 2026-08-08. Presence at confidence 100 (occupation) does already surface rival orgs in `show_sector_neighborhood`; a *scan* specifically flagging a rival's presence with priority does not.

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
* End-of-turn engine actions (in order — corrected 2026-08-08, this list had fallen behind two features built since it was last accurate):
    * **NPC decisions** — every `is_npc=1` player's registered strategy (see `engine/npc.py`) acts, via the same tool functions a human player would call, before anything else this turn resolves
    * Player declarations reset
    * Arrivals processed
    * **Ship's log dispatch** — any `queue_command`-deferred action due this turn (`before_arrival`/`after_arrival`/`at_turn`) fires here, right after arrivals so a chained action sees an org's just-landed state, and before production so a re-departing org's production is correctly suppressed that turn
    * Pod consumption then production (all pods: resources consumed first, then output produced; scan resolution for stationary orgs)
    * Colonization resolution (matured `colonize_complete` events flip `org_type` ship → colony)
    * Mission dispatch (`defend`/`attack` — still stubs)
    * Fog decayed
    * **Holdings calculated** — resource totals snapshotted across all orgs and pods
    * Turn counter incremented; game-over/final-score check

**Holdings are always calculated after all processing is complete** — never before. This ensures the snapshot reflects the true end state of the turn, including all production, arrivals, and mission outcomes.

---

# Player Actions

This section is the canonical design-authority inventory of every action a player can take within a game session. For implementation signatures, see `xsettlers_mcp/tools/` (renamed from `mcp/tools/` 2026-07-22 — it collided with the third-party `mcp` SDK package).

**Known gap, not yet reconciled:** this inventory predates several tools that now exist — `list_scenarios`, `select_scenario`, `get_player_state`, `get_sector`, `get_sector_map`, `show_civilization_status`, and `queue_command` (the ship's log — see `docs/TODO.md`'s "Design (Data Model canvas)" section) have no entries below. Left as a flagged gap rather than silently patched in, since closing it properly means either documenting each here or deciding this section's scope should narrow to something TODO.md/dev_history.md already cover better.

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

Default radius 4, giving a 9×9 bounding square (max 10). Ships in transit (at the sentinel sector) have no location and cannot serve as an `org_id` center — callers must supply explicit coordinates in that case.

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
* **Player-action events** (deltas): `ship.move_confirmed`, `ship.move_cancelled`, `mission.set`, `pod.task_set`, `organization.scan_bearing_set`, `pod.scan_bearing_set`, `organization.renamed`
* **Engine events** (deltas): `ship.colonized`, `colonize_complete` (scheduled 3 turns ahead by `mission.set`, resolved by the engine), `alert.scan_out_of_range`
* **Snapshots**: `turn.snapshot` — full state written at end of each turn for replay and debug; `game.final_scores` — the persisted, idempotent end-of-game scoreboard.
* **Corrected 2026-08-08**: the previous version of this list (`ship.move_previewed`, `pod.mission_set`, `pod.scan_target_set`, `turn.declared`, `ship.arrived`, `pod.produced`, `pod.scanned`, `alert.rival_detected`, `fog.decayed`) had zero matches anywhere in the codebase, verified by grep — either renamed away (task/mission split, offset-based scan aiming) with this doc never updated, or never actually implemented in the first place. Rival detection specifically (`alert.rival_detected`) is a real gap: [Visibility & Fog of War](#visibility--fog-of-war) above still describes it as a feature, but no such event is ever written.

---

# Rendering & Views

**Rewritten 2026-08-08 — the previous version of this section described `views/cli_renderer.py`, `views/slack_renderer.py`, and `build_ship_view()`/`build_colony_view()`/`build_sector_view()`. None of that exists anywhere in the codebase (verified by grep); only `views/render.py` does.** Same category of staleness as `docs/mcp_server_layer_design.md`'s abandoned `gateway.py` sketch, per CLAUDE.md — a design that was written down and then superseded by what actually shipped, with this doc never catching up.

What's actually built: every gameplay tool that has something worth displaying returns a `display` dict alongside its raw data — `rows_key`, `columns`, optional `header`/`column_labels`/`footer`, or `kind: "map"` for a grid (see `show_sector_neighborhood`). `views/render.py`'s `render_status()` (tables) and `render_map()` (grids) turn that into markdown, dispatching purely on the *shape* of the `display` block, never on which tool produced it — no per-tool-name branching, so any future tool returning either shape renders with zero changes here.

The response itself is controlled by a `response_format` argument on every tool call (`xsettlers_mcp/server.py`'s `call_tool`, not a per-tool schema property): `markdown_view` (default) returns both the raw JSON and the rendered markdown; `data_only` returns JSON alone; `html_svg` is reserved for a future graphics response and currently falls back to `markdown_view`. Two additional mechanisms steer an LLM client toward actually displaying the rendered markdown rather than reconstructing its own from the JSON — an MCP `instructions` string sent once at session `initialize`, and a repeated directive block appended to every `markdown_view` response — added 2026-08-05 after the alternative (hoping a client renders it verbatim) proved unenforceable by convention alone. See `docs/TODO.md`'s "default display" note if that's still there, or `xsettlers_mcp/server.py`'s `SERVER_INSTRUCTIONS`/`RENDER_DIRECTIVE` directly.

There is no separate debug-vs-player view distinction at the rendering layer — every tool is already ownership-gated to the calling player at the data layer (ownership checks inline in each tool, not a rendering-time filter), so there's nothing left for a render step to additionally hide.

---

# Out of Scope (POC)

* Combat (`defend` / `attack` missions are stubs)
* Multi-game support within a single xsettlers deployment (concurrent games, not just switching scenarios) — see GameHouse note under [Players & Game Instance](#players--game-instance) for the orchestration layer that now exists *above* individual deployments instead
* Runtime player join to an xsettlers-internal game already in session (GameHouse's lobby is a join-before-start flow, not mid-session join)
* Variable scan range (sensor pods)
* SVG map renderer (`response_format="html_svg"` is reserved as a value but falls back to markdown; nothing renders it yet)
* Authentication hardening (the static player directory in `config/game_config.yaml` is still unhardened; GameHouse-issued sessions are a separate, additional path, not a replacement)
