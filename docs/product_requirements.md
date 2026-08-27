# XSettlers — Product Requirements

# Overview

XSettlers is a multiplayer, turn-based space strategy game played through any MCP-speaking client (Slack is the intended home, but nothing in the server is Slack-specific). Players manage organizations (ships and colonies) across a sector map, competing to expand territory, produce resources, and outlast rivals.

**The game plays on a two-dimensional grid, and its coordinates are three-dimensional.** Sector coordinates and distance math are `(x, y, z)` throughout, and `z` is always `0`. That is a standing construct, not an oversight and not a gap waiting to be closed: the MVP will only ever be played on a plane, the third axis is carried anyway, and it stays carried until a later version redesigns around it. Do not remove `z` from a signature, a scenario, or a payload to tidy it away.

---

# Players & Game Instance

* A game instance is defined by a single scenario file (e.g. `config/game0.yaml`) and a corresponding SQLite database — one shared game per deployed instance.
* The player roster is **fixed at bootstrap time**. There is no mechanism for joining a game already in session through xsettlers' own tools (see GameHouse below for the one exception).
* Internally, xsettlers still supports several scenario files side by side (`config/game*.yaml`), each declaring its own `participants` against a service-wide player *directory* in `config/game_config.yaml` — player count is a property of the scenario, not the deployment, and a solo, two-player, or five-player game differs only in YAML. **Presenting itself as a browsable library to a person is not xsettlers' job** — that role belongs to a separate sibling service, **GameHouse** (`../gamehouse`), which owns Person-level identity and lobby matchmaking across potentially many hosted games, xsettlers being one of them. See `docs/TODO.md`'s "GameHouse handoff" section for the current integration. `list_scenarios`/`select_scenario` remain live as xsettlers-internal tools; they are simply not the only way a game gets started.
* Each player is identified by an opaque `player_token`, not a Slack-specific ID — nothing in the auth path is platform-specific. A GameHouse-driven session (see above) generates its own `player_token`s per handoff, entirely separate from `config/game_config.yaml`'s static roster; both paths are live.
* Roster size is per-scenario, bounded by the engine-wide `max_players` ceiling (currently 8). The shipped scenarios are two-player (`game0`, `game1`) and one-player (`game_solo`). Concurrent multi-game — several games live at once within one xsettlers deployment, rather than one per deployment — is still future scope; GameHouse orchestrates across separately-deployed games, it doesn't make any single xsettlers deployment itself multi-instance.

---

# The Map

* The game world is a grid of **sectors**, each with integer coordinates `(x, y, z)` — the schema and distance math are fully 3D, but every shipped scenario places its participants and their fleets at `z = 0` only (see Overview above).
* A sector has exactly one resource capacity: **energy**. Food and goods are manufactured from stock an organization already holds, never harvested from the map, so there is no per-sector pool of them.
* **Sectors are lazily instantiated and their richness is rolled at discovery.** No sector row exists until a scan, a ship arrival, or bootstrap placement reveals it via `db/sectors.py`'s `reveal_sector()`. On that first reveal — and only then — its energy is rolled as **400 + d6 × 100**, giving 500 / 600 / 700 / 800 / 900 / 1000 at flat 1-in-6 odds, mean 750. The floor is deliberate: every sector is worth working, so an unlucky roll costs a player upside rather than viability.
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
* Created 3 turns after a ship is given org mission `colonize` (scheduled via a `colonize_complete` event at the moment the mission is set, resolved by the engine 3 turns later) — not on the same turn the mission is set.
* **Colonizing costs 30 energy** (`COLONIZATION_ENERGY_COST`), charged in full from the ship's pooled stock at the moment the mission is set — not spread over the 3-turn transition. Unlike every other cost in the economy this is an all-or-nothing gate rather than a prorated draw: a ship that cannot pay is refused and left untouched, since there is no such thing as half a conversion. The figure is provisional and expected to move with play data.

---

# Pods

* Each organization carries one or more **pods**.
* A pod has no intrinsic type — it is defined entirely by its **task**.
* Valid pod tasks: `idle`, `produce_energy`, `produce_food`, `produce_goods`, `scan`.
* **Pods have `task`; organizations have `mission`.** Two different concepts, two different words, deliberately: one word for both is actively misleading in status reports, where an idle *ship* sits above a table of busy *pods*.
* The colloquial names *energy*, *farm*, and *factory* map to the `produce_energy`, `produce_food`, and `produce_goods` tasks respectively — useful in UI and narrative, not a data field.
* Each pod has generic storage (`storage_capacity` plus `energy_stored`/`food_stored`/`goods_stored`) — a pod holds any mix of resources regardless of its own task, so retasking never hides or relabels existing cargo.
* Pods execute their task every turn regardless of whether their parent org is in transit or stationary, with two exceptions in transit: energy cannot be harvested (no sector to harvest from) and scans do not report.

## Pod Tasks — Full Roster

**A pod has no type.** Every pod is the same object — generic storage, no specialization — and the only thing that varies between two of them is the task their crew has been given. That is the point of pods being modular: a player deploys each one however they wish and retasks it whenever they wish, and because storage is generic (see above) retasking never strands or relabels what the pod is already holding. There is no `pod_type` field, no manifest to reconfigure, and no pod that can only ever do one job.

So the roster below is a list of **jobs a pod can be given**, not a catalogue of pods to acquire. Only a subset are active in any given game instance; the rest are deferred.

| Pod Task | Colloquial Name | Status | Description |
|---|---|---|---|
| `idle` | — | **Active** | Pod does nothing. Default state. |
| `produce_energy` | energy | **Active** | Harvests energy from the organization's current sector — the only resource drawn from the map, and the sector depletes as it is taken. Consumes food. |
| `produce_food` | farm | **Active** | Manufactures food each turn from stored resources — **not** sector-sourced. Consumes energy and goods. |
| `produce_goods` | factory | **Active** | Manufactures goods each turn from stored resources — **not** sector-sourced. Consumes energy and food. The slowest to produce and the highest-scoring. |
| `scan` | scanner | **Active** | Scans one sector at end of turn, aimed by bearing via `set_pod_scan_bearing`. Consumes food. Suppressed (but still charged) while in transit. |
| `defend` | defense | Deferred | Crew works the shields, absorbing damage aimed at their org. Requires combat system. |
| `attack` | attack | Deferred | Crew works the guns against other ships and colonies. Requires combat system. |

Two jobs that used to be listed here are gone rather than deferred, because the current model does their work already: a **cargo** pod is redundant when every pod stores any mix of resources up to its own capacity, and a **crew** pod — "general-purpose, flexible, less productive than a specialist" — describes what every pod now is. Neither is a thing to build; both were consequences of pods having types.

### Rates (per pod, per turn)

`engine/production.py` is authoritative; this table is a
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

**Colonies multiply output by 1.5** (`COLONY_PRODUCTION_MULTIPLIER`). Every
pod aboard a colony produces 1.5× its base rate; ships
produce 1.0×. The multiplier applies to output only — costs and upkeep are
identical to a ship's — and it applies to the sector draw as well as to what
lands in storage, so a colony harvesting energy strips its sector 1.5× as
fast. Because costs are fixed, the effect on the *margin* is much larger than
1.5×: a 6-pod org netting +1 energy / +1 food / +2 goods a turn as a ship nets
+7 / +6 / +4 as a colony, roughly tripling its score rate.

**POC active tasks:** `idle`, `produce_energy`, `produce_food`, `produce_goods`, `scan`. All other entries above are recognized in the data model but are not instantiated in any current game instance.

**Platform note:** The stack is **Python · SQLite · MCP SDK**, served over streamable HTTP to any MCP-speaking client — identity and transport are both client-agnostic (see [Overview](#overview)), not Slack-specific.

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
* **Rival detection**: if a rival org is present in the scanned sector at time of resolution, that information is surfaced with high priority in the player view. **Not implemented** — no `alert.rival_detected` (or equivalent) event exists anywhere in the codebase. Presence at confidence 100 (occupation) does already surface rival orgs in `show_sector_neighborhood`; a *scan* specifically flagging a rival's presence with priority does not.

---

# Scanning

* Scanning is performed at end of turn by an organization's own sensors and by any pods on the `scan` task — it is not a discrete player action.
* A scanner must be **aimed** before end of turn for the scan to execute — via `set_org_scan_bearing` for an organization's own sensors, or `set_pod_scan_bearing` for a pod on the `scan` task. An unaimed scanner still pays its food cost and reveals nothing.
* Scan range is **fixed at 2 sectors** (Euclidean distance ≤ 2), derived from `get_scan_range(org_id)` — always call it, never hard-code the number. Under a Euclidean metric, range 1 would reach only the four orthogonal neighbours, because a diagonal is √2 ≈ 1.41, and being refused a scan of the diagonally adjacent sector reads as broken. Range 2 reaches **12 sectors** — 4 orthogonal, 4 diagonal, 4 two-out orthogonal — the smallest radius at which scanning behaves the way a player expects.
* **A scan is aimed by a bearing relative to the scanner, not by absolute coordinates.** Sensors are mounted on the thing that carries them: they look a fixed direction and distance from wherever it currently is, and a ship flying away from a sector does not keep seeing it. Two consequences: a scan pattern survives a move with no re-aiming, and range becomes a permanent property of the aim rather than something that can silently stop being true — so an out-of-range aim is **rejected when set** instead of failing at resolution.
* **Bearings**: `N NE E SE S SW W NW` (distance 1 or √2) and `N2 E2 S2 W2` (distance 2) — the 12 names map exactly onto the 12 sectors reachable at range 2. Explicit `offset_x/y/z` is always available for anything the table doesn't name (including off-plane targets). **North is −y**, matching the neighborhood map's rendering; arbitrary but fixed.
* An organization's own sensors and a scan pod's follow identical rules — same bearings, same cost, same range, same suppression in transit.
* **A scan reveals the targeted sector only — no halo, no surrounding ring.** A radius-5 halo was considered and rejected. If scanning is to be a meaningful activity with a real cost, it must not also be cheap area coverage: one pod-turn plus its food buys one sector of knowledge, and the player chooses which. Range says how far you can *reach*; it does not widen what you *get*.
* If the designated target sector is **out of range** at end-of-turn resolution, the scan does not execute and the player receives an alert. The food cost is still paid — costs are drawn before a reveal is attempted, deliberately (see `docs/dev_history.md`).
* Ships in transit cannot scan — the reveal is suppressed for the duration of transit.
* **Future:** range becomes variable per org once sensor pods exist. The `get_scan_range()` hook is already in place.

---

# Turn Structure

* The game advances in **turns**. The current turn is stored in `game_state`.
* A player **declares end of turn** when they have no further moves. Once all players have declared, the turn resolves (consensus acceleration).
* Players may **rescind** their end-of-turn declaration before resolution.
* End-of-turn engine actions, in order:
    * **NPC decisions** — every `is_npc=1` player's registered strategy (see `npc/strategies.py`) acts, via the same tool functions a human player would call, before anything else this turn resolves
    * Player declarations reset
    * Arrivals processed
    * **Ship's log dispatch** — any `queue_command`-deferred action due this turn (`upon_arrival`/`at_turn`) fires here, right after arrivals so a chained action sees an org's just-landed state, and before production so a re-departing org's production is correctly suppressed that turn
    * Pod consumption then production (all pods: resources consumed first, then output produced; scan resolution for stationary orgs)
    * Colonization resolution (matured `colonize_complete` events flip `org_type` ship → colony)
    * Mission dispatch (`defend`/`attack` — still stubs, and unreachable while `set_mission` refuses both; the step is kept as the seam combat lands in)
    * Fog decayed
    * **Holdings calculated** — resource totals snapshotted across all orgs and pods
    * Turn counter incremented; game-over/final-score check

**Holdings are always calculated after all processing is complete** — never before. This ensures the snapshot reflects the true end state of the turn, including all production, arrivals, and mission outcomes.

---

# Player Actions

This section is the canonical design-authority inventory of every action a player can take within a game session. For implementation signatures, see `xsettlers_mcp/tools/` (never `mcp/tools/` — that name collides with the third-party `mcp` SDK package).

**Known gap, not yet reconciled:** this inventory predates a good deal of what now exists. Fourteen of the registry's 29 tools have entries below; the fifteen without are the six task-force tools (`create_task_force`, `add_to_task_force`, `remove_from_task_force`, `disband_task_force`, `list_task_forces`, `order_task_force` — designed in `docs/dev_history.md`), scenario selection and session setup (`list_scenarios`, `select_scenario`, `start_session`, `set_display_name`), the read tools `get_player_state`, `get_sector`, `get_sector_map` and `show_civilization_status`, and `queue_command` (the ship's log, whose four trigger primitives are documented at `db/schema.py`'s `org_command_queue` definition). Left as a flagged gap rather than silently patched in, since closing it properly means either documenting each here or narrowing this section's scope to something `docs/dev_history.md` already covers better.

---

## Mission & Pod Configuration

### Set Mission (`set_mission`)

Assigns a mission to one of the player's organizations. Valid missions are: `idle`, `move`, `colonize`, `defend`, `attack` — but `defend` and `attack` are **refused** with "Weapons are inoperable", since combat is designed and not built. They stay in the vocabulary rather than being dropped from it: dropped, the rejection would enumerate the survivors and read as "this game has no combat", which is false. What neither answer may do is accept the order silently, which is what happened before the gate — the mission was written, the fleet report printed it, and the engine's stubs never resolved it. Colonies cannot be assigned the `move` mission. Setting a ship's mission to `colonize` causes it to convert to a colony at end of turn if it remains in the target sector.

### Set Pod Task (`set_pod_task`)

Assigns a task to a pod belonging to the player's organization. Valid tasks: `idle`, `produce_energy`, `produce_food`, `produce_goods`, `scan`. Tasks take effect immediately and persist until changed. Pods continue running their task regardless of whether their parent ship is in transit (except `scan`, whose reveal is suppressed during transit, and `produce_energy`, which has no sector to draw from).

For `task = scan`, an aim may optionally be supplied in the same call as either a compass `bearing` or an explicit `offset_x/y/z`. If omitted, the pod takes the scan task with no aim — `set_pod_scan_bearing` must follow before end of turn, or the pod pays its food cost and reveals nothing.

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

Aims an organization's **own** sensors. Every ship and colony can scan one sector per turn on its own account — a ship's bridge, a colony's headquarters — without dedicating a pod to it. Identical in every rule to a scan pod: same food cost, same range, and the same behavior in transit — the cost is paid, the reveal is suppressed. An organization that also carries scan pods gets both, and each pays its own way.

### Set Pod Scan Bearing (`set_pod_scan_bearing`)

Aims a pod already on the `scan` task. Same vocabulary and same rules as the organization's own sensors.

Both take either a compass `bearing` or an explicit `offset_x/y/z`, and passing neither clears the aim (and stops paying for it). An out-of-range aim is **rejected when set** rather than at resolution: an aim is an offset, so its range is fixed and cannot drift.

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

### Show Neighborhood Resources (`show_neighborhood_resources`)

The companion to `show_sector_neighborhood` over exactly the same viewport — same center rules (an `org_id` or an explicit coordinate triple, never a ship in transit), same default radius 4 and max 10, same fog of war, same single drawn z-plane with off-plane sectors counted rather than dropped. Both draw their lattice through shared helpers in `sector_tools.py`, so the two reports cannot come to disagree about which sectors count as "nearby".

What differs is the question a cell answers. There: who is standing in the sector. Here: what the sector is worth. A known cell reads as its energy capacity **in thousands to two decimals** (`2.20` is 2,200 energy — a legend line carries the unit as `Energy (×1k)`, since the figure alone is meaningless), `·` means in range and never seen, blank means out of range, and the sector the view is centered on takes a trailing `@` — a grid of bare numbers has no other anchor for finding yourself.

Cells are a fixed five characters wide, blanks and unknowns padded to match, so the decimal points line up down a column. That matters for the markdown *source*, which is what a client showing text raw, a log or a diff displays; a rendered table would align regardless. The detail rows below use the same unit as the grid and say so in the column header — two figures for one quantity in one report is how a player misreads it.

**Energy is the whole map** because energy is the only resource a sector yields; food and goods are manufactured from stock already held, never harvested. When a sector grows a second yield, the cell gains a slot rather than the suite gaining a report.

A figure is only as current as the sector it came from: energy capacity never changes on its own, but the player's knowledge of it ages, and a sector that blinks out at confidence 0 takes its reading off the map with it — hence `confidence` beside the figure in the detail rows. Those rows are a shortlist, not a repeat of the grid: the 10 richest sectors in view, ranked, ties broken on coordinates so the same board always ranks the same way.

Also a **pure view** — reveals nothing, costs nothing, changes no confidence.

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
* **Rival detection is a real gap**: [Visibility & Fog of War](#visibility--fog-of-war) above describes it as a feature, but no `alert.rival_detected` event is ever written. The event list above is the complete set the code actually emits — check it against `grep` before relying on any event name.

---

# Rendering & Views

**Rendering lives entirely in `views/render.py`.** There is no `views/cli_renderer.py`, no `views/slack_renderer.py`, and no `build_ship_view()`/`build_colony_view()`/`build_sector_view()` — if you find those named in an older design sketch, they were never built.

What's actually built: every gameplay tool that has something worth displaying returns a `display` dict alongside its raw data — `rows_key`, `columns`, optional `header`/`column_labels`/`footer`, or `kind: "map"` for a grid (see `show_sector_neighborhood`). `views/render.py`'s `render_status()` (tables) and `render_map()` (grids) turn that into markdown, dispatching purely on the *shape* of the `display` block, never on which tool produced it — no per-tool-name branching, so any future tool returning either shape renders with zero changes here.

The response itself is controlled by a `response_format` argument on every tool call (`xsettlers_mcp/server.py`'s `call_tool`, not a per-tool schema property): `markdown_view` (default) returns both the raw JSON and the rendered markdown; `data_only` returns JSON alone; `html_svg` returns the JSON plus a server-rendered SVG document for the tools listed in `server.py`'s `SVG_RENDERERS` (today `show_organization` alone), and falls back to `markdown_view` for the rest. **The server draws; no client ever executes JavaScript** — that is what keeps the graphics client-agnostic. Two additional mechanisms steer an LLM client toward actually displaying the rendered markdown rather than reconstructing its own from the JSON — an MCP `instructions` string sent once at session `initialize`, and a repeated directive block appended to every `markdown_view` response. Convention alone does not hold — a client left to its own devices rebuilds its own table from the JSON. See `xsettlers_mcp/server.py`'s `SERVER_INSTRUCTIONS`/`RENDER_DIRECTIVE`.

There is no separate debug-vs-player view distinction at the rendering layer — every tool is already ownership-gated to the calling player at the data layer (ownership checks inline in each tool, not a rendering-time filter), so there's nothing left for a render step to additionally hide.

---

# Out of Scope (POC)

* Combat (`defend` / `attack` missions are stubs, and `set_mission` refuses both rather than accepting an order nothing will resolve)
* Multi-game support within a single xsettlers deployment (concurrent games, not just switching scenarios) — see GameHouse note under [Players & Game Instance](#players--game-instance) for the orchestration layer that now exists *above* individual deployments instead
* Runtime player join to an xsettlers-internal game already in session (GameHouse's lobby is a join-before-start flow, not mid-session join)
* Variable scan range (sensor pods)
* SVG map renderer (`response_format="html_svg"` is reserved as a value but falls back to markdown; nothing renders it yet)
* Authentication hardening (the static player directory in `config/game_config.yaml` is still unhardened; GameHouse-issued sessions are a separate, additional path, not a replacement)
