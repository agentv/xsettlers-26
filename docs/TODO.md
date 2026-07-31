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

* [ ] **Player-settable organization names** — no tool exists to rename an org post-bootstrap (`name` is written once at bootstrap and never touched again by any tool in `organization_tools.py`). Needs a new tool, likely `rename_organization(player_token, org_id, name)` — ownership-gated like everything else in that module, with some not-yet-decided sanity bound on length/characters. First instance of a broader question worth keeping in mind: `mission` is currently the only org characteristic a player can edit post-bootstrap (via `set_mission`/`set_pod_mission`) — if more editable characteristics get added later, worth revisiting whether they each get their own bespoke tool or start warranting a more general "edit organization" entry point.
* [ ] `organization_tools.py` — `set_pod_mission(mission='scan')`: if the parent ship is currently in transit (`mission == 'move'`), allow the assignment but return a warning that the scan pod will be suppressed until arrival.
* [ ] Consider a DB-level `CHECK` constraint enforcing `org_type != 'colony' OR is_mobile = 0` on `organizations` — currently only enforced by application code (bootstrap + colonize resolution), not the schema itself.

## Distance metric — Euclidean for the MVP, revisit later

* [ ] **Distance is Euclidean everywhere, and that is a deliberate MVP choice, not an oversight** (logged 2026-07-31 at the player's request). Movement (`navigation_tools.py`: `ceil(euclidean / jump_range_per_turn)`), scan range (`_scan_target_status`), and the neighborhood viewport (`show_sector_neighborhood`) all use straight-line distance on integer coordinates. Consequences observed in play-testing:
  - A diagonal move costs the same as three orthogonal steps (√8 ≈ 2.83 → `ceil` 3), so diagonals are consistently poor value for the displacement gained.
  - Scan radius produces a plus-shape at range 1 and a 12-cell rosette at range 2, never a square. Range 3 would reach 28 cells in-plane; the growth is quadratic in-plane and cubic once `z` is ever used, which is what killed the old `get_scan_targets` pick-list (see `docs/dev_history.md`, 2026-07-29).

  The alternative worth weighing is **Chebyshev** distance (`max(|dx|,|dy|,|dz|)`), under which a diagonal costs the same as an orthogonal step and radius *r* is exactly the (2r+1)³ box — much closer to how players read a grid. Long-term the intent is something more sophisticated than either; the likely shape is that movement and sensing stop sharing one metric, since "how far can I travel" and "how far can I see" are not obviously the same geometry. **Staying Euclidean for the MVP** — noted so the eventual change is a decision rather than a surprise.

## Config

* [ ] **`config/game_config.yaml`'s `game:` block is mostly dead, shadowed by env vars.** Only `max_players` and `score_weights` are consumed. `tick_seconds` (vs `GAME_TICK_SECONDS`), `turn_limit` (vs `TURN_LIMIT`), and `confidence_decay_per_turn` (vs `CONFIDENCE_DECAY_PER_TURN`) are each parsed into `GameSettings` by `config/loader.py` and then never read; `dimensions` and `feature_flags` are read by nothing anywhere. This is a live trap — editing a value in the YAML looks like it should work and silently does nothing. Fix is to pick a precedence rule (proposed: YAML supplies the default, env overrides it) and apply it to all of them at once, rather than wiring up one field and leaving the rest inconsistent. Raised 2026-07-30 while changing the fog decay model.

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
* **Variation does not exist.** `db/sectors.py`'s `DEFAULT_SECTOR_RESOURCE_UNITS`
  is a flat 1000 for every sector, so there is *fresh* soil but never *better*
  soil. Until sectors differ, "look around and pick a good spot" has nothing
  to look at — moving is a refuel run, not a judgment call. Variation, not
  scarcity, is what turns the map into a decision surface, and it is the
  prerequisite for transit stress being interesting rather than merely
  punishing. Rolled **at discovery**, per the existing `# TODO: randomize
  per-sector later` note in `db/sectors.py`. Player's stated preference
  (2026-07-30): the distribution should centre **meaner than the current flat
  1000** — a rich sector should be a find, not the baseline.

Also noted: `sectors.food_capacity` and `sectors.goods_capacity` exist and are
seeded, but nothing reads them — only energy is sector-sourced. Vestigial
under the current recipe model; decide whether they become real (multi-resource
soil) or get dropped when this work happens.

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

* **Colonies should out-produce ships.** Stability should pay: a colony's
  production advantage is what makes staying put correct *as long as the
  sector still yields*, which is the counterweight to transit stress. Today
  they are mechanically identical — same 6-pod loadout, same E:20/F:20/G:10,
  measured in the play-test. The `is_mobile` vs `org_type` split already
  exists to hang this on (`is_mobile` is the behavioural flag, `org_type` the
  semantic label), so the modifier has a natural home without new schema.

* **Goods should demand heavier inputs.** Direction: making goods — the
  scoring resource — should get materially more expensive in raw resources.
  Current baseline in `engine/production.py`: goods produce 5/turn against
  energy and food at 10, and cost 2 energy + 1 food to run, so goods are
  already the expensive, slow one. The intent is to widen that gap, not
  introduce it.

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

* [ ] `models/` directory is stubbed; CRUD logic currently lives in tool files — refactor once POC is stable.

## TDD rule (standing policy — not a task, keep applying it)

* **No new function without a corresponding test entry.** `test_navigation.py` and `test_organization.py` are the templates to follow.
* A new computed field on an *existing* API response gets a new assertion appended to that response's existing test, not a new test function — reserve new test functions for new functions, new branches, or new error paths (see `docs/dev_history.md`'s 2026-07-30 test-suite-consolidation entry for why).

## Gateway / Player Onboarding

* [ ] `xsettlers_mcp/auth.py` roster check is still config-file-based, not hardened — same "trust identity for now" caveat as originally spec'd, unchanged.
* [ ] Multi-game support (concurrent games, not just switching scenarios) is still out of scope — `select_scenario` explicitly rejects switching once a game is active rather than supporting multiple simultaneous games.

### Multi-game lobby — still not built

The long-term vision (see `docs/dev_history.md` for the architectural groundwork already in place: one-DB-per-game decision, `players.is_npc`, `bootstrap_game()`'s `roster_override`): a master lobby where a player authenticates once, sees several available games, picks one, waits for real players to fill in, and NPCs backfill remaining slots. Still open:

* [ ] **Matchmaking logic, and NPC fill-in at roster time specifically.** NPC decision-making itself now exists (`engine/npc.py` — see dev history's 2026-07-30 NPC strategy profiles entry), but nothing yet assigns an NPC to backfill an open lobby slot automatically; `assign_npc_profile()` is still a manual call, not wired into any join/roster flow.
* [ ] Per-game DB file provisioning/routing, and `db/connection.py`'s `DB_PATH` becoming per-game — today it's one global env var read fresh per call (correct for one game per deployment); a lobby will need a "resolve DB path for this game" mechanism layered on top later, not a change to `get_connection()` itself.
* [x] ~~`home_sector_by_player` is a fixed 2-element position-indexed list; a variable-size roster will `IndexError`.~~ **Resolved 2026-07-30.** Scenarios now declare a `participants` list (directory email + that player's `home_sector`), resolved into `Seat` objects by `config/loader.py`'s `resolve_seats()`. Player count is a property of the scenario, so variable roster sizes work with no code change — `config/game_solo.yaml` is the 1-player proof. Positional pairing between the roster and the scenario is gone entirely.
* [ ] `events.game_id INTEGER` exists in the schema but is never populated by any `INSERT INTO events` anywhere in the codebase — vestigial under the DB-per-game model. Fine to leave (dropping columns is out of scope), just noting it's dead.
* [ ] `roster_override` is still the one path that can desync identity. `authenticate()` now reads the same scenario `participants` list that `bootstrap_game()` seeds from, so the config path is reconciled — but a caller passing `roster_override` assembles seats that exist in no YAML file, and `authenticate()` will not recognize those players. A real lobby needs the directory itself to become dynamic (or `authenticate()` to fall back to the `players` table once a game is bootstrapped). Narrower than it was, not closed.

### NPC strategy profiles — remaining work

Core system is built (see `docs/dev_history.md`'s 2026-07-30 entry: schema, registry, execution hook, `assign_npc_profile()`). Still open:

* [ ] Only one registered strategy (`fan_out_consolidate`) exists — no `set_mission`/`set_pod_mission`-driven strategies yet; the registry supports them, nothing uses them.
* [ ] No roster-time NPC assignment — still a manual `assign_npc_profile()` call, not wired into any lobby/bootstrap flow (see "Multi-game lobby" above).
