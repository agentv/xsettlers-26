# XSettlers — Known TODOs

Only things that still need to be done. Completed work and resolved
decisions have been moved to `docs/dev_history.md` (split out 2026-07-30) —
check there for the "why" behind anything that looks like it should already
exist.

## DB & Engine

* [ ] `engine/turn.py` — `_handle_defend` and `_handle_attack` are stubs; implement combat system later.
* [ ] `engine/turn.py` — **rival detection is still unbuilt** (`# TODO: emit pod.scanned event; detect rivals` in scan resolution). No `alert.rival_detected` event is ever emitted and the schema has no sighting-history table, so rival positions can only be read live from `organizations`. `show_sector_neighborhood` works around this by reporting rivals *only* in sectors at confidence 100 (ones the player occupies) — anywhere else would leak current intel onto a decayed cell. Building sighting storage would let rivals appear on stale cells honestly, stamped with the turn last seen. See `docs/ui_and_rendering_design.md`'s Cell vocabulary.
* [ ] `engine/turn.py` — scan pods still pay their food cost while in transit; only the *reveal* is currently suppressed (the `org["sector_id"] != -1` check gates just the reveal/range-check branch, not the recipe-drain above it). Should suppress the whole scan mission, cost included, while in transit — not just the reveal.

## MCP Tools

* [ ] **Move-tasking response template — canonical.** Locked in play-testing on 2026-07-30: this is the report a player wants back after ordering a ship to move, and it should be what `confirm_move` (and `set_mission(mission='move')`, which delegates to it) actually returns. Today they return a bare dict of raw fields and the client improvises the rest. Four parts, in order:

  1. **What was ordered, previewed then committed** — `turns_needed` and `arrival_turn` per ship, then the confirmation. Preview before commit is the intended flow (see the `set_mission` tool description); showing both makes the cost of the order visible before its effect.
  2. **The whole fleet, not just the ships that moved** — `id`, `name`, `where`, `mission`, one row per org. In-transit rows must name the destination (`in transit → (10,12,0)`), because `show_civilization_status`'s `status` string is deliberately terse and a player who just issued a move needs to see it reflected. Showing unmoved orgs is the point: the question after tasking two ships is "what do I still have available," not "did those two ships accept the order."
  3. **What it cost in aggregate** — how many orgs are now off the board, what fraction of total holdings went with them, what remains at home.
  4. **One forward-looking consequence** — the thing that will matter when they arrive but is not visible in the table. Play-test example: both destinations were 2 sectors out against a scan range of 1, so the two arrival footprints would not overlap each other or home — two isolated islands of vision. This part is judgment, not a computed field; the template asks for the most relevant single consequence, not a checklist.

  Implementation shape: parts 1–3 are mechanical and belong in a `display` block on the move response, following the same hints convention as `show_organization` and `render_map` (see `docs/ui_and_rendering_design.md`) so any client renders them identically. Part 4 is not mechanizable and stays with whatever agent is narrating.

* [ ] `show_civilization_status` returns `current_turn: None` — `turn_limit` resolves (20) but the turn number does not, so the fleet report cannot answer "what turn is it" without a separate `get_current_turn()` call. Found while play-testing 2026-07-30. The fleet report is exactly where a player looks for turn context, and its own docstring promises it.

* [ ] **Is energy production meant to be suppressed in transit?** Observed in play-testing 2026-07-30: a ship with `mission='move'` reports `E:0, F:20, G:10` — pod tasking unchanged (still 2 energy pods), but energy output stops while food and goods continue. Since food and goods each consume energy to run (`engine/production.py`'s recipes) and energy is the one input that needs none, a long voyage burns down its carried energy with no way to replenish it, and can arrive unable to restart its own economy. That is either a nice hidden cost of exploration or an accident of how transit suppression was written. Not yet traced through `engine/turn.py` — decide which it is before treating it as a rule.

* [ ] **A general "edit organization" entry point may eventually be warranted.** `rename_organization` was built 2026-07-31, and `mission`/`task` are settable, so a player can now edit three things about an org — each via its own bespoke tool. If more editable characteristics arrive, revisit whether they each get a tool or whether one general editor is the better shape. Not urgent; noted so the third bespoke tool isn't followed by a fourth without anyone asking.
* [ ] `organization_tools.py` — `set_pod_task(task='scan')`: if the parent ship is currently in transit, allow the assignment but return a warning that the scan will be suppressed (though still charged) until arrival.
* [ ] Consider a DB-level `CHECK` constraint enforcing `org_type != 'colony' OR is_mobile = 0` on `organizations` — currently only enforced by application code (bootstrap + colonize resolution), not the schema itself.

## Distance metric — Euclidean for the MVP, revisit later

* [ ] **Distance is Euclidean everywhere, and that is a deliberate MVP choice, not an oversight** (logged 2026-07-31 at the player's request). Movement (`navigation_tools.py`: `ceil(euclidean / jump_range_per_turn)`), scan range (`_scan_target_status`), and the neighborhood viewport (`show_sector_neighborhood`) all use straight-line distance on integer coordinates. Consequences observed in play-testing:
  - A diagonal move costs the same as three orthogonal steps (√8 ≈ 2.83 → `ceil` 3), so diagonals are consistently poor value for the displacement gained.
  - Scan radius produces a plus-shape at range 1 and a 12-cell rosette at range 2, never a square. Range 3 would reach 28 cells in-plane; the growth is quadratic in-plane and cubic once `z` is ever used, which is what killed the old `get_scan_targets` pick-list (see `docs/dev_history.md`, 2026-07-29).

  The alternative worth weighing is **Chebyshev** distance (`max(|dx|,|dy|,|dz|)`), under which a diagonal costs the same as an orthogonal step and radius *r* is exactly the (2r+1)³ box — much closer to how players read a grid. Long-term the intent is something more sophisticated than either; the likely shape is that movement and sensing stop sharing one metric, since "how far can I travel" and "how far can I see" are not obviously the same geometry. **Staying Euclidean for the MVP** — noted so the eventual change is a decision rather than a surprise.

## Config

* [ ] **`config/game_config.yaml`'s `game:` block is mostly dead, shadowed by env vars.** Only `max_players` and `score_weights` are consumed. `tick_seconds` (vs `GAME_TICK_SECONDS`), `turn_limit` (vs `TURN_LIMIT`), and `confidence_decay_per_turn` (vs `CONFIDENCE_DECAY_PER_TURN`) are each parsed into `GameSettings` by `config/loader.py` and then never read. This is a live trap — editing a value in the YAML looks like it should work and silently does nothing. Fix is to pick a precedence rule (proposed: YAML supplies the default, env overrides it) and apply it to all of them at once, rather than wiring up one field and leaving the rest inconsistent. Raised 2026-07-30 while changing the fog decay model.
  * **Narrowed 2026-08-11** (complexity audit): `dimensions` and `feature_flags` are **gone** — deleted from `GameSettings` and from the YAML. They belonged to a different category than the three above: no env var shadowed them and no code read them anywhere, so there was no precedence rule to decide and nothing to reconcile. (`dimensions: 2` had been sitting in the YAML contradicting the loader's own default of `3`, with nothing to notice.) The three env-shadowed fields are deliberately **kept** pending the precedence decision — they are reserved, not dead. Re-add feature flags alongside the code that reads them.

## Infrastructure

* [ ] Add a CI workflow (`.github/workflows/`) — deferred for now, planned before/around the first push upstream.
* [ ] **Known gap: `config/game_config.yaml`'s roster is committed to git but `player_token` is now a real credential.** Committed values are placeholders (`REPLACE_WITH_GENERATED_TOKEN_*`), not real secrets — don't paste real generated tokens into this file as-is. No mechanism yet lets `xsettlers_mcp/auth.py` read real per-player secrets from somewhere outside git (analogous to how `MCP_SHARED_SECRET` is handled via `fly secrets` + a gitignored local `.env`) while keeping the "roster is one YAML file" design. Deferred until real (non-`@example.com`) players are actually onboarded.
* [ ] **`/mcp` is open, and `player_token` is the only access control.** The `MCP_SHARED_SECRET` perimeter was removed 2026-07-31 to make the server reachable by MCP client connectors, which accept a URL and optionally OAuth but have no field for a static `Authorization` header — with the gate in place, a connector could only ever receive a 401. The consequence is that anyone who knows the URL can call any tool, and `player_token` only proves "caller knows this player's credential." Compounding it: the roster's tokens are still `REPLACE_WITH_GENERATED_TOKEN_*` placeholders in a **public** repository that also documents this server's URL, and there is no rate limiting. Accepted knowingly (the game holds nothing of value and is unadvertised) — but this is now the single largest gap, and it stops being acceptable the moment either of those facts changes. The real fix is OAuth on the endpoint plus per-player tokens held outside git; those two are the same piece of work as the roster-in-git item above.

## Dev/Admin Tooling (explicitly NOT part of the player-facing MCP interface)

* [ ] **Clock pause/resume for experimentation** — need a way to freeze `engine/clock.py`'s background tick so DB state holds still while poking at it (today's workaround — kill the server, call `end_of_turn()` manually in a loop, restart — works but is a manual dance, not a real mechanism). Explicitly scoped as **developer/admin-only, never a `server.py` tool registration, never reachable over `/mcp`** — this must not become something a player token can invoke. Not designed yet — open questions: an env var the clock checks each tick vs. a signal/file-flag vs. a small out-of-band control (e.g. a second, unauthenticated-but-localhost-only endpoint, or just a CLI flag/script) that never touches `xsettlers_mcp/server.py`'s tool dispatch at all. Whatever the shape, it needs to be impossible to trigger through the same channel a player's `player_token` reaches.

## Design direction — the economic arc

Direction set by the player 2026-07-30 during the first solo play-test, with
supporting measurements from that session. The three pieces below are meant to
land in order, each making the next one meaningful. Marked **decided** where
the player committed to it and **direction** where it is intent, not yet a
settled rule.

### 1. Transit stress — **decided**, not built

**Intent:** moving should cost. A ship in motion burns energy and forgoes
production, and the player accepts that in anticipation of finding better
ground. Without a price, moving is free, so it is not a decision — and the
whole intended skill loop (read your surroundings, judge where the resources
are, commit to planting roots) only becomes a skill when the move is a gamble
that can be got wrong.

**What actually happens today is the inverse, and by a wide margin.** Measured
at turn 1 of the play-test, per org, per turn:

| | energy | food | goods | score delta |
|---|---|---|---|---|
| at home | +1 | −5 | +4 | **+3** |
| in transit | −7 | −1 | +8 | **+15** |

Transit is worth five times as much score as sitting still. The mechanism:
a ship in transit produces no energy but keeps consuming it (goods cost 2
energy, food costs 1, and org upkeep costs 1 regardless of transit state).
So the stress *exists* — the energy drain is real — but it is rewarded,
because energy scores 0 and draining it frees storage headroom that goods,
which score 2, immediately fill. Transit is currently a machine for
converting worthless energy into scoring goods.

**The deeper cause is that every org starts at 100% storage.** At full
capacity, production is waste and only consumption creates room, so *anything*
that consumes without replacing is a score engine. Fixing transit stress
without looking at the full-at-bootstrap start would likely just relocate the
exploit.

**Not designed yet:** whether the cost is a flat per-turn energy drain, total
forfeit of production while moving, a cost scaling with distance, or a
combination. Note that suppressing *more* production is not obviously the fix
— suppressing energy production is what causes the current inversion.

### 2. Sector resource variation and scarcity — **direction**, explicitly later

The player intends to reduce available sector resources so that staying put
stops paying and relocation becomes urgent. Two things worth separating,
because only one of them exists:

* **Depletion already works.** Only energy is sector-sourced (`engine/production.py`'s
  `RESOURCE_CAPACITY_COLUMN`; food and goods are manufactured from stored
  resources, not harvested). Production draws energy from the sector and
  decrements it, prorating output when the sector runs thin. Measured: the
  home sector went 1000 → 900 in one turn with 5 orgs on it, so a full fleet
  of 9 strips a sector in roughly five turns. Urgency-to-move is therefore
  already in the engine, unbuilt only in the sense that nothing makes the
  player feel it yet.
* ~~**Variation does not exist.**~~ Built 2026-08-02: `roll_sector_energy()`
  rolls **400 + d6 × 100** at discovery — 500 to 1000, flat odds, mean 750 —
  while home sectors are seeded bottomless (`HOME_SECTOR_ENERGY`) so the lean
  frontier costs a player their expansion, not their footing.
  Open follow-up: **variation does not yet change outcomes.** Measured across
  five map seeds on Outbreak, the spread moved the final margin by ~8 points
  in ~2750, and the passive player's score was byte-identical on every map.
  The reason is that at 20 turns almost nothing runs dry, so how much a sector
  holds never binds — only *whether* you are on one does. Richness will start
  to matter when depletion does: longer games, more colonies (which draw at
  1.5×), or a leaner band. Do not conclude the roll is mistuned until
  depletion actually bites; the thing to change first is probably the horizon,
  not the dice.

Resolved 2026-08-02: `sectors.food_capacity`/`goods_capacity` are **dropped**,
not made real. Food and goods are manufactured from held stock, so a per-sector
pool of them had nothing to mean — and being displayed to players, they
actively misrepresented what a sector was worth. Sector variation therefore has
exactly one axis to vary: `energy_capacity`.

Contention is already settled and needs no new mechanism: `reveal_sector()` is
the single get-or-create entry point for scans, ship arrivals, and bootstrap
placement alike, so whoever reveals a sector first establishes its value and
every later look by anyone — rival included — reads the established (and
possibly already depleted) figure. Randomizing at discovery is therefore safe
as-is; no player can re-roll another's find.

#### Open questions, raised 2026-08-02 — **not decided**

Three things the player flagged while setting the current levels. Recorded so
the reasoning survives the gap; none of them is a decision yet, and the
current numbers are explicitly "good enough until we look again."

* **Is `HOME_SECTOR_ENERGY = 100,000` higher than it needs to be?** Suspected
  yes. It was sized as ~600 turns of maximum plausible draw, which is far more
  headroom than "does not deplete" actually requires. Nothing depends on the
  magnitude — the only property that matters is that home outlasts any real
  game — so this can come down a long way without changing behaviour. Purely
  a question of what reads honestly to someone opening the scenario file.

* **Should a sector ever deplete to *zero*?** Currently it can, and does:
  measured over 60 turns, five frontier sectors reached exactly 0 and became
  permanently dead ground (there is no regeneration). Whether total exhaustion
  is meant to be part of the game at all is unsettled. Alternatives not
  explored: a floor below which a sector cannot be drawn, slow regeneration,
  or diminishing returns that asymptote rather than terminate. Note the
  interaction with the abrupt-failure mode already documented under Rates —
  upkeep is drawn before production, so a fleet on dead ground stops
  completely rather than tapering.

* **Richness is a reserve, not a rate — and that may be why it doesn't
  matter.** This is the most substantive of the three. `energy_capacity` is
  purely a depletion budget: a 1000-energy sector and a 500-energy one are
  *identical to work* until the poorer one runs out. Nothing about a rich
  sector makes a pod more productive while it lasts. That is very likely why
  two separate experiments failed to show variation affecting outcomes —
  richness can only express itself through the horizon, and no game so far has
  run long enough for the horizon to bind. The idea raised: let richness
  drive **production rate** as well as duration, so a good find pays
  immediately rather than eventually. Undesigned — whether that is a
  multiplier on pod output, a cap on how fast a sector can be drawn, or
  something else, and how it interacts with `COLONY_PRODUCTION_MULTIPLIER`
  (which is already a rate multiplier and would compound with it).

Also unresolved from the colony work, and relevant here because it is the
other place a rate meets a ceiling: **storage capacity does not scale with the
colony multiplier.** At 60 turns both home colonies finished at 99.5% and 100%
of capacity, so their 1.5× output became pure waste and the passive player
overtook the colonizer on turn 47. Decide this before retuning the multiplier
— the problem is not that 1.5× is too strong or too weak, it is that the extra
output has nowhere to go.

### 3. Combat — **direction**, phase three

The long-game discovery the player is aiming at: growing resources and
*capturing* resources are alternative strategies, and in a long game capture
may be competitive with cultivation. `engine/turn.py`'s `_handle_defend` and
`_handle_attack` are no-op stubs today (tracked separately under DB & Engine).
This is what makes a third answer to "where do my resources come from"
available alongside produce-here and move-elsewhere.

### Rotating scanners — **direction**, post-MVP equipment advance

Raised 2026-07-31 alongside relative scan aiming, and deliberately deferred.
Today an org (or a scan pod) holds **one** offset, set by hand, which persists
until changed — a fixed bearing that travels with the hull. The idea is to let
a scanner hold a *sequence* of bearings and cycle one per turn, sweeping its
own surroundings unattended: `N, E, S, W` on an org would cover four sectors of
rolling coverage per hull, and nine orgs sweeping four cells each is 36 sectors
a turn without a single manual order.

Deferred because it changes the balance rather than the mechanics: scanning is
meant to cost something and be chosen, and automation makes it cheap by making
it free of attention. The intended home is as an **equipment/technology
advance** once scenarios can grant differentiated kit — a scanner that sweeps
is a better scanner, and should have to be earned or fitted rather than being
how scanning works by default. Sequence it with sensor pods and ship classes
(see the Design section), not before.

The groundwork is already right: aim is stored as an offset, so a rotation is a
list of offsets and the resolution step already knows how to turn one into a
sector.

### Further tensions raised 2026-07-30 — **direction**

These are not sequenced against the arc above; they are the levers the player
expects to shape the game with.

* **Scenario setup is the tension lever.** The intent is that a scenario's
  characteristics — not engine constants — are where difficulty and pressure
  get dialled in, so variants can be tuned without code changes. Worth knowing
  what is exposed today: `starting_fill` (scenario-wide, overridable per pod
  template), `pods_per_ship[].storage_capacity`, `ships_per_player`,
  `home_colony`, and each participant's `home_sector`.

  `starting_fill` was **built 2026-07-30** in response to this direction —
  previously `db/bootstrap.py` hardcoded every pod to start 100% full, which
  was both the "everyone is too rich" problem and the cause of the
  full-capacity waste distortion described under Transit stress. All three
  shipped scenarios state `starting_fill: 1.0` explicitly to preserve their
  original balance; the field defaults to 1.0 only for compatibility, and is
  not a recommended starting point. The per-template override exists for
  asymmetric starts (full on food, thin on goods) as a tension lever.

  Still open: nobody has actually tuned a scenario with it. The player has
  deferred the richness question itself until the final-round scoring regimen
  is designed, so the dial exists but the setting is undecided. Note the floor
  — production consumes resources to run, so a scenario starting at or near
  0.0 deadlocks its own economy with nothing to spend.

* **Per-player loadouts within a scenario** (postulated 2026-07-30). Today
  `pods_per_ship`, `ships_per_player` and `starting_fill` are scenario-wide:
  every participant is issued an identical fleet. The postulate is that a
  scenario should be able to give each player their *own* loadout —
  asymmetric starts as a first-class scenario feature rather than a fairness
  violation. The natural home is the participant entry, which already carries
  per-player data (`home_sector`, `is_npc`), with the scenario-wide values
  becoming defaults a participant may override — the same cascade
  `starting_fill` already uses between scenario and pod template. Enables
  handicapping, asymmetric-faction scenarios, and a tutorial where the human
  starts stronger than the NPCs. Not designed: whether a participant
  overrides the whole loadout or patches individual fields.

* ~~**Colonies should out-produce ships.**~~ Done 2026-08-02:
  `COLONY_PRODUCTION_MULTIPLIER = 1.5`, output-only, plus a 30-energy
  conversion charge. Open follow-up: **both numbers are provisional.** 1.5×
  gross roughly triples a 6-pod org's net score rate because costs are fixed,
  which may prove too strong once several colonies compound; and 30 energy was
  picked to establish that conversion has a price at all, not sized against
  what a colony is worth. Retune against play data, not analysis.

* **Resource transfer between organizations** — no such tool exists today; an
  org's stock is reachable only by its own pods. The play this enables:
  ship a cargo of resources to a fledgling colony that has landed somewhere
  rich but has nothing to work with, letting it bootstrap into abundance.
  That makes a well-chosen colony site worth *investing in* rather than merely
  worth occupying, and gives ships a logistics role distinct from exploration.
  Undesigned: whether transfer requires co-location in a sector (almost
  certainly), whether it costs anything, and whether it is one tool or a
  pair (give/take).

## Design (Data Model canvas)

* [x] ~~Ship's log — free-form notes plus queued future-turn commands.~~
  **Built 2026-08-05** (commit `c0c3a89`). Queued commands only — free-form
  notes still not built, deferred as a trivial low-risk add-on whenever
  there's an actual use for them. Four fixed trigger primitives, not an
  arbitrary N-turn delay: `during_transit` (event-triggered, fires the
  instant an org departs — dispatched from `engine/movement.py`'s
  `apply_confirm_move`, restricted to `action='set_pod_task'` since pod
  tasking is the one thing not locked by departure), `before_arrival` (fires
  the same `end_of_turn()` pass that lands the org), `after_arrival` (fires
  exactly one pass later), and `at_turn` (an explicit absolute turn number,
  independent of any move — for orders that don't fit the arrival-relative
  model at all). New table `org_command_queue` (`db/schema.py`); dispatch
  logic in `engine/ship_log.py`, called from `engine/turn.py`'s `end_of_turn()`
  as step 2.5, right after arrival resolution and before production so a
  chained action sees the org's just-landed state. New MCP tool
  `queue_command` (`xsettlers_mcp/tools/organization_tools.py`). Action
  whitelist: `move` and `set_pod_task`, each dispatched via a `cur`-based
  core mutation helper (`engine/movement.py`, `engine/pod_tasking.py`) split
  out from the player-facing tool specifically so dispatch can run inside
  `engine/turn.py`'s own open transaction without a second self-connecting
  call colliding with it ("database is locked" — `db/connection.py` sets no
  `busy_timeout`). `engine/npc.py`'s `_fan_out_consolidate` — the strategy
  that originally motivated this — migrated off its hand-rolled
  `memory["second_leg_turn"]` poll onto `after_arrival`, deleting that
  bespoke pattern entirely; its `hold_turns` config (previously 2) is gone,
  since `after_arrival` is fixed at exactly one turn. See `tests/test_ship_log.py`.
* [ ] **Ship classes** — not built, not documented anywhere yet. Today every `org_type='ship'` row is a single undifferentiated archetype — `org_type` is a flat ship/colony binary with no stat variation within "ship," and the closest adjacent concept, `pod_type` (`crew`/`cargo`/`defense`/`attack`/`ship`/`sensor`), is about individual pod roles, not ship-level archetypes — deferred, not instantiated. The idea as raised:
  - **Ranger class** — mobile, fast, longer range/faster movement, traded off against thinner "skin" (durability/cargo, exact tradeoff not yet specified)
  - **Transit ability is a class-differentiating stat** (raised 2026-07-30). Today `jump_range_per_turn` is a per-call argument defaulting to 1, identical for every hull — so "fast ship" has nowhere to live. Once transit stress exists (see "Design direction" above), how far a class moves per turn and what that movement costs it become the natural axis distinguishing hulls: a Ranger buys range and pays in cargo, a Colony hull is the reverse. Sequence this after transit stress, since range only means something once movement has a price.
  - **Legacy class** — general-purpose mix of resources, eventually weapons; the default/baseline class (closest to what every ship already is today, undifferentiated)
  - **Colony class** — heavily loaded with resources, purpose-built for a one-way trip to found a colony — distinct from the existing `org_type='colony'` flip (what a ship *becomes* after colonizing); this would be about a ship's *loadout/build* before that transition even starts

  Not designed: what fields this needs (a `ship_class` column on `organizations`? a stat-modifier table keyed by class?), how it interacts with the existing `pods_per_ship`/pod-loadout templates in `config/game*.yaml`, or how it relates to the deferred `pod_type` roster above (a ship class could plausibly be *defined* by its pod-type mix rather than being a separate field). Backlog item only — no decision made yet.
* [ ] Review sector schema for ownership field creep.
* [ ] Evaluate denormalized active player ID vector on Sector (future).
* [ ] Evaluate Neo4j Community Edition (future).

## Models refactor

* [ ] CRUD logic currently lives in tool files (`xsettlers_mcp/tools/*.py`) — pull it out into a model layer once the POC is stable.
  * **2026-08-11** (complexity audit): the `models/` directory itself has been **deleted**. It held six 0-byte files and had no importer anywhere, so it advertised a layer that did not exist — a reader looking for model classes found an empty package rather than an honest absence. The refactor above is unaffected: recreate the package when there is something to put in it. Note that the natural first step is now item #1 of the audit (a shared auth/connection helper), which is where the per-tool CRUD boilerplate actually concentrates.

## TDD rule (standing policy — not a task, keep applying it)

* **No new function without a corresponding test entry.** `test_navigation.py` and `test_organization.py` are the templates to follow.
* A new computed field on an *existing* API response gets a new assertion appended to that response's existing test, not a new test function — reserve new test functions for new functions, new branches, or new error paths (see `docs/dev_history.md`'s 2026-07-30 test-suite-consolidation entry for why).

## Gateway / Player Onboarding

* [ ] `xsettlers_mcp/auth.py` roster check is still config-file-based, not hardened — same "trust identity for now" caveat as originally spec'd, unchanged.
* [ ] Multi-game support (concurrent games, not just switching scenarios) is still out of scope — `select_scenario` explicitly rejects switching once a game is active rather than supporting multiple simultaneous games.

### Multi-game lobby — superseded by an external service (GameHouse), not built internally

The long-term vision described here originally (a master lobby where a player authenticates once, sees several available games, picks one, waits for real players to fill in, NPCs backfill remaining slots) is now being delivered by a **separate sibling project, GameHouse** (`../gamehouse`), rather than as internal xsettlers machinery — see the new "GameHouse handoff" subsection below for what's actually built and live-verified. The architectural groundwork this section originally pointed at (one-DB-per-game decision, `players.is_npc`, `bootstrap_game()`'s `roster_override`) is exactly what GameHouse's handoff turned out to need, unmodified — see `docs/dev_history.md`. Remaining internal-only items:

* [x] ~~**Matchmaking logic, and NPC fill-in at roster time specifically.**~~ **Resolved externally, 2026-08-07.** GameHouse's `join_lobby`/`close_lobby` do real matchmaking and NPC backfill; `assign_npc_profile()` is now wired into a real handoff flow via `xsettlers_mcp/gamehouse.py`'s `start_session()` — not xsettlers' own internal lobby, but a real, live-verified path.
* [ ] Per-game DB file provisioning/routing, and `db/connection.py`'s `DB_PATH` becoming per-game — still open; GameHouse's handoff assumes "one shared game per deployed instance" exactly as xsettlers already did, doesn't change this.
* [x] ~~`home_sector_by_player` is a fixed 2-element position-indexed list; a variable-size roster will `IndexError`.~~ **Resolved 2026-07-30.** Scenarios now declare a `participants` list (directory email + that player's `home_sector`), resolved into `Seat` objects by `config/loader.py`'s `resolve_seats()`. Player count is a property of the scenario, so variable roster sizes work with no code change — `config/game_solo.yaml` is the 1-player proof. Positional pairing between the roster and the scenario is gone entirely.
* [ ] `events.game_id INTEGER` exists in the schema but is never populated by any `INSERT INTO events` anywhere in the codebase — vestigial under the DB-per-game model. Fine to leave (dropping columns is out of scope), just noting it's dead.
* [ ] `roster_override` is now actually called (by `start_session()`, see below) — narrows but doesn't close the original note here: a GameHouse-driven session still doesn't touch `config/game_config.yaml`'s directory or `authenticate()` at all, it's a fully separate identity path (see "GameHouse handoff" below for exactly how those two paths coexist).

### GameHouse handoff — built and live-verified, 2026-08-07

`xsettlers_mcp/gamehouse.py` is xsettlers' side of `../gamehouse`'s wire contract (`docs/data_model.md` there). Scoped to Diaspora (`config/game0.yaml`) only — xsettlers registers as a scenario-less game (`scenarios=[]`), since GameHouse's registration model carries one lobby shape per registered game and multi-scenario support isn't resolved on either side yet.

**Built:**
* `register_with_gamehouse()` — xsettlers acts as an MCP client, once at server startup (`xsettlers_mcp/server.py`'s `main()`), publishing its lobby shape to GameHouse's `register_game`. Best-effort: a dev environment with no `GAMEHOUSE_URL`/`XSETTLERS_PUBLIC_URL` set (the common case) skips silently rather than blocking startup.
* `start_session(session_token, players, scenario_key=None)` — the actual handoff GameHouse calls once a lobby closes. Builds a `roster_override` (see above) and calls the existing, unmodified `bootstrap_game()`.
* **The existing static-roster auth (`xsettlers_mcp/auth.py`, `config/game_config.yaml`'s directory) is deliberately untouched.** `start_session` is an additional bootstrap path alongside `select_scenario()`, not a replacement — it generates ordinary `player_token`s that every existing gameplay tool already knows how to check via the same `SELECT id FROM players WHERE player_token=?` pattern. Whether to eventually retire the static-roster path, or keep both indefinitely, is not decided.
* **Live-verified end-to-end across two real separate processes**, not just unit tests: real `welcome`/`verify_code` login on GameHouse, `join_lobby` filling a lobby and closing it, a genuine HTTP push of `start_session` from GameHouse to xsettlers, xsettlers bootstrapping real ships/pods, and the returned `player_token` confirmed working against `get_player_state`.
* One real bug this surfaced and fixed: `start_session`'s declared MCP tool schema said `scenario_key` was `"type":"string"`, which JSON Schema rejects for `null` — and GameHouse always sends `null` explicitly for a scenario-less game. The Python function itself handled `None` fine; the schema declaration didn't. Caught only by the live round trip, not by any unit test (calling the Python function directly, even through `call_tool()`, bypasses the MCP SDK's `jsonschema.validate()` entirely) — `tests/test_gamehouse.py::test_start_session_tool_schema_permits_null_scenario_key` now checks the schema declaration itself, not just the function.

**Still open, named directly in GameHouse's own docs as required game-side surface that doesn't exist yet:**
* [ ] A results-object hand-back to GameHouse at game completion (already-interpreted score, not raw play-state — see GameHouse's "the game interprets, GameHouse only hosts" division).
* [ ] A run-state query GameHouse can poll, so it can tell a returning Person whether their game is actually still alive before offering to reconnect.
* [ ] Multi-scenario support on both sides — `scenario_key` is accepted but not yet branched on; today it's always `None` in real traffic.

### GameHouse `join_lobby` dedupe bug — lives in `../gamehouse`, not here

Live-verified 2026-08-10: the same real Person joined the same lobby twice (client-side retry), filling both of Diaspora's 2 seats with one identical `player_id`. `close_lobby` (`gamehouse_mcp/lobby.py`) builds `players` straight from `lobby_member` rows with no `DISTINCT`/dedup, so it pushed `start_session` with two identical `player_id`s. On xsettlers' side, `start_session`'s roster loop (`xsettlers_mcp/gamehouse.py`) derives `email = f"gamehouse-{gh_id}@handoff"` per seat with no disambiguation, so the two seats collided on `players.email`'s `UNIQUE` constraint (`db/schema.py`) and the automatic push crashed outright — confirmed via `db/bootstrap.py`'s plain `INSERT` with no dedup/upsert and no exception handling anywhere in the call chain. The fix belongs in GameHouse (reject a second `join_lobby` from a `person_id` already in that lobby, or dedupe before building the roster) — noted here only because the live-verification and the resulting broken hand-off happened from this side. Deferred until the multi-game work below is settled.

### Multi-game support — deferred design decision, 2026-08-10

Chasing the dedupe bug above raised a bigger question during the same live test: what happens if GameHouse pushes a *second* `start_session` while a game is already in progress? Verified safe **today** — `xsettlers_mcp/gamehouse.py`'s active-game check, `db/bootstrap.py`'s independent sector-count guard, and `INSERT OR IGNORE` on the `games`/`game_state` singleton rows are three independent layers, and a mismatched second push is rejected with an error rather than touching any existing data. But "rejected with an error" is only tolerable because the MVP runs one shared game per deployed instance (see "Design decision for future multi-game support" above). Real multi-game support needs more than reject-and-bounce, and the shape of it is decided even though none of it is built yet:

* [ ] `bootstrap_game()` should mint each game under a uniquely-identified database rather than writing to the single shared `DB_PATH` file it uses today — this is that same one-SQLite-DB-file-per-game-instance design, now scoped concretely: the unique identifier is chosen at bootstrap time, and a not-yet-designed lobby/router layer would route a `player_token` to the right file. Until this lands, xsettlers can only ever run one active game per deployment, and every subsequent handoff attempt just bounces off the guards above instead of starting its own game.
* [ ] A game-ending tool (doesn't exist yet — see the results-hand-back/run-state items above) should, on completion, archive that game's database (move it out of the live path, not delete it) and mark the game's status as complete in whatever registry ends up tracking multiple games.
* [ ] Once a game can be marked complete, **GameHouse must never offer a Person the option to join or reconnect to a completed game.** This lives on GameHouse's side (`../gamehouse`) — `list_games`/`join_lobby`/`open_games` would need to check completion status (via the results hand-back or a run-state poll) before presenting a game as available, not just check whether a `game_journal` row exists.

### NPC strategy profiles — remaining work

Core system is built (see `docs/dev_history.md`'s 2026-07-30 entry: schema, registry, execution hook, `assign_npc_profile()`). `fan_out_consolidate` now runs on the ship's log (see above) rather than hand-rolled polling — the general dispatch mechanism these strategies actually need now exists. **Roster-time NPC assignment is now wired up** — not to an internal xsettlers lobby, but to GameHouse's `start_session()` handoff (see above), which calls `assign_npc_profile()` for real for every `npc`-kind entry in an incoming roster.

#### Fleet-strategy taxonomy — named 2026-08-05, **built and registered 2026-08-06**

A conversation (not a plan-mode design pass) named four behavioral archetypes and, more importantly, reframed what they attach to: **these are fleet strategies, not player strategies.** A player (NPC or, eventually, human) can run several fleets at once, each on its own strategy — e.g. one fleet turtling at the home colony while another fans out. `npc_profiles` today is `player_id`-keyed (one strategy/config/memory blob per player, see `db/npc_profiles.py`) — fleet-scoped assignment needs a `fleet_id` instead, and **fleets don't exist as a concept in the data model at all yet**: `organizations.player_id` is the only ownership link; there is no notion of a named subset of a player's organizations. **In the current MVP a player has exactly one implicit fleet — everything they own** — so `player_id`-keying happens to be correct today by coincidence, not by design; multi-fleet-per-player is explicitly future work, not a bug in the current model. Rollout: fleet strategies stay NPC-only until "advanced" versions of the game expose explicit fleet-strategy assignment to human players.

All four are now real, registered functions in `engine/npc.py`'s `STRATEGIES` dict (not just named/characterized) — live-verified via a real NPC seated through the GameHouse handoff above, running `fan_out` under `end_of_turn()` and actually reacting to scan results.

The four named styles, as characterized in conversation (broad guidelines, not full specs):

* **turtle** — hold still, take no actions, ever. Trivial to implement (`return memory` unchanged). Already has one real data point: this is exactly what "Player Two" did in the Diaspora mock-run comparison (see `docs/dev_history.md` if/when that session gets written up) — 2240 points, 14.9% behind a burst-and-colonize opponent.
* **fan_out** — distribute outward *and find opportunity*. Not the same as today's `fan_out_consolidate`, which is a blind fixed-offset pattern (jump N sectors in 4 preset directions regardless of what's there). A true opportunity-seeking fan_out would need to scan first and let what's *found* — richer sectors, open territory — steer the second leg, rather than a hardcoded destination.
* **burst-and-colonize** — fast, simultaneous multi-direction departure (claim ground before evaluating, unlike fan_out) plus committing a fixed fraction of the fleet to colonizing early, betting the 1.5x production multiplier compounding over the rest of the game beats staying fully mobile. Also has a real data point: this is what "Vincent" ran in the same Diaspora mock-run (6 ships fanned out, 2 colonized turn 1) — 2574 points, the winning side.
* **frontier-map, stay-frosty** — continuous reconnaissance: ships never settle, they keep relocating and re-aiming scanners (org sensors and scan pods both) to maximize revealed territory and keep it from decaying back into fog. No colonizing — mobility is the identity. Real cost, not free: scanning charges food (`POD_CONSUMPTION_RECIPE`) and a moving ship doesn't produce energy in transit, so this strategy trades score for map coverage by design — its payoff is informational (and, once combat stops being a stub, early-warning), not economic.

Not designed: the `fleet_id` schema itself, how a fleet is defined/assigned (all-or-nothing per player today via `is_npc`+`npc_profiles`), or concrete parameters for any of the four styles beyond the broad behavioral description above.

#### Round-robin tournament, 2026-08-10 — and the `fan_out` rewrite it motivated

`scripts/run_tournament.py` played every pair of the (by then) five registered strategies once each (`turtle`, `fan_out`, `fan_out_consolidate`, `burst_and_colonize`, `frontier_map_stay_frosty`), one subprocess/scratch DB per matchup (`scripts/simulate_npc_matchup.py --json-out`; repeatedly reloading the SpatiaLite extension across connections in one long-running process was segfaulting on the second matchup). `scripts/build_tournament_report.py` renders the results as a static-SVG report (`tourney/report.html`, data under `tourney/data/`, both gitignored-scratch except the committed output).

Every strategy scored identically regardless of opponent — no combat, no resource contention at this map scale — so the standings are a fixed, transitive ranking of solo performance, not real head-to-head play: `burst_and_colonize` (2582) beats `fan_out_consolidate` (2296) beats `fan_out` (2277) beats `turtle` (2240) beats `frontier_map_stay_frosty` (1864, energy collapses to ~0 by turn 5 from constant movement).

`fan_out` placing behind the "blind fixed-offset" `fan_out_consolidate` was the interesting result: `fan_out`'s v1 had each scout commit individually to whatever its own scan found, with no comparison across scouts or quality threshold (see the v1 caveat this section used to carry, before the rewrite below) — so a scout in a poor direction would happily settle there while a richer sector sat one bearing over, undiscovered by that scout. `fan_out` was rewritten so scouts no longer commit individually: each records its find (coords + `energy_capacity`) and waits; once every scout's scan has resolved, the whole fleet converges on the single best sector found across all of them. Not re-run against the tournament field yet — the ranking above still reflects the pre-rewrite behavior.
