# XSettlers — Known TODOs

Only things that still need doing. Settled decisions and hard-won findings are
in `docs/dev_history.md`; everything else is in the code.

## DB & Engine

* [ ] `engine/turn.py` — `_handle_defend` and `_handle_attack` are stubs; implement combat later.
* [x] ~~`engine/turn.py` — **rival detection is unbuilt**~~ Built 2026-08-18. A scan now reveals the organizations standing in its target sector as well as the sector's own resources, each org rolling its own d6 detection check (`db/sightings.py`, threshold 6 of 6 — certain for now, and the die is rolled anyway so lowering it later changes odds without shifting a seeded run's roll sequence). Sightings land in the new `org_sightings` table, one row per (observer, org), upserted on re-sighting, and a `scan.contact` event names what was detected. `show_sector_neighborhood` now distinguishes "R" (a rival there now, shown only where you stand) from "r" (one a scan saw there, dated with `sighted_at_turn`) — so stale cells report history honestly instead of being blanked. **Still unbuilt: the `pod.scanned`/`org.scanned` events**, and no NPC strategy scans toward an opponent, so nothing in the library produces contact on its own — see the Crowd note under NPC strategies.
* [ ] `engine/turn.py` — scan pods pay their food cost while in transit; only the *reveal* is suppressed (the `org["sector_id"] != -1` check gates the reveal/range branch, not the recipe drain above it). The whole scan should be suppressed in transit, cost included.

## MCP Tools

* [ ] **Move-tasking response template — canonical.** This is the report a player wants back after ordering a ship to move, and it should be what `confirm_move` (and `set_mission(mission='move')`, which delegates to it) returns. Today they return a bare dict of raw fields and the client improvises the rest. Four parts, in order:

  1. **What was ordered, previewed then committed** — `turns_needed` and `arrival_turn` per ship, then the confirmation. Preview before commit is the intended flow (see the `set_mission` tool description); showing both makes the cost of the order visible before its effect.
  2. **The whole fleet, not just the ships that moved** — `id`, `name`, `where`, `mission`, one row per org. In-transit rows must name the destination (`in transit → (10,12,0)`), because `show_civilization_status`'s `status` string is deliberately terse and a player who just issued a move needs to see it reflected. Showing unmoved orgs is the point: the question after tasking two ships is "what do I still have available," not "did those two ships accept the order."
  3. **What it cost in aggregate** — how many orgs are now off the board, what fraction of total holdings went with them, what remains at home.
  4. **One forward-looking consequence** — the thing that will matter on arrival but is not visible in the table. Example: two destinations 2 sectors out against a scan range of 1, so the two arrival footprints overlap neither each other nor home — two isolated islands of vision. This part is judgment, not a computed field.

  Implementation shape: parts 1–3 are mechanical and belong in a `display` block on the move response, following the same hints convention as `show_organization` and `render_map` (see `docs/ui_and_rendering_design.md`) so any client renders them identically. Part 4 is not mechanizable and stays with whatever agent is narrating.

* [ ] `show_civilization_status` returns `current_turn: None` — `turn_limit` resolves (20) but the turn number does not, so the fleet report cannot answer "what turn is it" without a separate `get_current_turn()` call. The fleet report is exactly where a player looks for turn context, and its own docstring promises it.

* [ ] **Is energy production meant to be suppressed in transit?** A ship with `mission='move'` reports `E:0, F:20, G:10` — pod tasking unchanged (still 2 energy pods), but energy output stops while food and goods continue. Since food and goods each consume energy to run (`engine/production.py`'s recipes) and energy is the one input that needs none, a long voyage burns down its carried energy with no way to replenish it, and can arrive unable to restart its own economy. That is either a nice hidden cost of exploration or an accident of how transit suppression was written. Decide which before treating it as a rule.

* [ ] **A general "edit organization" entry point may eventually be warranted.** A player can edit three things about an org — name, mission, pod task — each via its own bespoke tool. If more editable characteristics arrive, revisit whether they each get a tool or whether one general editor is the better shape. Not urgent; noted so the third bespoke tool isn't followed by a fourth without anyone asking.
* [ ] `organization_tools.py` — `set_pod_task(task='scan')`: if the parent ship is in transit, allow the assignment but return a warning that the scan will be suppressed (though still charged) until arrival.
* [ ] Consider a DB-level `CHECK` constraint enforcing `org_type != 'colony' OR is_mobile = 0` on `organizations` — currently enforced only by application code (bootstrap + colonize resolution), not the schema.
* [ ] **Tick countdown is computed in two places** — `scripts/status.py`'s `_clock_status` and `views/format.py`'s `tick_countdown` both parse `next_tick_at` and `divmod` it into minutes/seconds (~5 lines each). Deliberately **not** merged: they differ in what they can know. The CLI runs as its own process and can health-check the server to distinguish "paused" from "counting down"; the in-process one cannot, so a `None` there already means "not running". Both files comment on the relationship. Worth revisiting only if a third caller appears — at which point the shared piece is the formatting, not the liveness judgement.

## Distance metric — Euclidean for the MVP, revisit later

* [ ] **Distance is Euclidean everywhere, and that is a deliberate MVP choice, not an oversight.** Movement (`navigation_tools.py`: `ceil(euclidean / jump_range_per_turn)`), scan range, and the neighborhood viewport all use straight-line distance on integer coordinates. Consequences observed in play:
  - A diagonal move costs the same as three orthogonal steps (√8 ≈ 2.83 → `ceil` 3), so diagonals are consistently poor value for the displacement gained.
  - Scan radius produces a plus-shape at range 1 and a 12-cell rosette at range 2, never a square. Range 3 would reach 28 cells in-plane; growth is quadratic in-plane and cubic once `z` is used.

  The alternative worth weighing is **Chebyshev** distance (`max(|dx|,|dy|,|dz|)`), under which a diagonal costs the same as an orthogonal step and radius *r* is exactly the (2r+1)³ box — much closer to how players read a grid. Long-term the intent is something more sophisticated than either; the likely shape is that movement and sensing stop sharing one metric, since "how far can I travel" and "how far can I see" are not obviously the same geometry. **Staying Euclidean for the MVP** — noted so the eventual change is a decision rather than a surprise.

## Config

* [ ] **`config/game_config.yaml`'s `game:` block is mostly dead, shadowed by env vars.** Only `max_players` and `score_weights` are consumed. `tick_seconds` (vs `GAME_TICK_SECONDS`), `turn_limit` (vs `TURN_LIMIT`), and `confidence_decay_per_turn` (vs `CONFIDENCE_DECAY_PER_TURN`) are each parsed into `GameSettings` by `config/loader.py` and then never read. This is a live trap — editing a value in the YAML looks like it should work and silently does nothing. The fix is to pick a precedence rule (proposed: YAML supplies the default, env overrides it) and apply it to all three at once rather than wiring up one and leaving the rest inconsistent. The three are reserved, not dead; re-add feature flags only alongside code that reads them.

## Infrastructure

* [ ] Add a CI workflow (`.github/workflows/`) — planned before/around the first push upstream.
* [ ] **The roster in `config/game_config.yaml` is committed to git, but `player_token` is a real credential.** Committed values are placeholders (`REPLACE_WITH_GENERATED_TOKEN_*`) — don't paste real generated tokens into this file. No mechanism yet lets `xsettlers_mcp/auth.py` read real per-player secrets from outside git (analogous to `fly secrets` + a gitignored `.env`) while keeping the "roster is one YAML file" design. Deferred until real (non-`@example.com`) players are onboarded.
* [ ] **`/mcp` is open, and `player_token` is the only access control.** Anyone who knows the URL can call any tool, and `player_token` only proves "caller knows this player's credential". Compounding it: the roster's tokens are placeholders in a **public** repository that also documents this server's URL, and there is no rate limiting. Accepted knowingly (the game holds nothing of value and is unadvertised) — but this is the single largest gap, and it stops being acceptable the moment either fact changes. The real fix is OAuth on the endpoint plus per-player tokens held outside git; that is the same piece of work as the roster item above.

## Design direction — the economic arc

The three pieces below land in order, each making the next meaningful. Marked
**decided** where it is committed and **direction** where it is intent.

### 1. Transit stress — **decided**, not built

**Intent:** moving should cost. A ship in motion burns energy and forgoes
production, and the player accepts that in anticipation of finding better
ground. Without a price, moving is free, so it is not a decision — and the
whole intended skill loop (read your surroundings, judge where the resources
are, commit to planting roots) only becomes a skill when the move is a gamble
that can be got wrong.

**What happens today is the inverse, and by a wide margin.** Measured per org,
per turn:

| | energy | food | goods | score delta |
|---|---|---|---|---|
| at home | +1 | −5 | +4 | **+3** |
| in transit | −7 | −1 | +8 | **+15** |

Transit is worth five times as much score as sitting still. The mechanism: a
ship in transit produces no energy but keeps consuming it. So the stress
*exists* — the energy drain is real — but it is rewarded, because energy
scores 0 and draining it frees storage headroom that goods, which score 2,
immediately fill. Transit is a machine for converting worthless energy into
scoring goods.

**The deeper cause is starting at 100% storage.** At full capacity production
is waste and only consumption creates room, so *anything* that consumes
without replacing is a score engine. Fixing transit stress without addressing
the full-at-bootstrap start would likely just relocate the exploit.

**Not designed:** whether the cost is a flat per-turn energy drain, total
forfeit of production while moving, a cost scaling with distance, or a
combination. Note that suppressing *more* production is not obviously the fix
— suppressing energy production is what causes the current inversion.

### 2. Sector resource variation and scarcity — **direction**, explicitly later

The intent is to reduce available sector resources so that staying put stops
paying and relocation becomes urgent. Two things worth separating:

* **Depletion works.** Only energy is sector-sourced. Production draws it from
  the sector and decrements it, prorating output when the sector runs thin.
  Measured: the home sector went 1000 → 900 in one turn with 5 orgs on it, so
  a full fleet of 9 strips a sector in roughly five turns. Urgency-to-move is
  already in the engine, unbuilt only in the sense that nothing makes the
  player feel it yet.
* **Variation exists but does not yet change outcomes.** Measured across five
  map seeds on Outbreak, the spread moved the final margin by ~8 points in
  ~2750, and the passive player's score was byte-identical on every map. At 20
  turns almost nothing runs dry, so how much a sector holds never binds — only
  *whether* you are on one does. Richness starts to matter when depletion
  does: longer games, more colonies (which draw at 1.5×), or a leaner band. Do
  not conclude the roll is mistuned until depletion actually bites; the thing
  to change first is probably the horizon, not the dice.

Sector variation has exactly one axis: `energy_capacity`. Contention needs no
new mechanism — `reveal_sector()` is the single get-or-create entry point for
scans, arrivals and bootstrap alike, so whoever reveals a sector first
establishes its value and every later look, rival included, reads the
established (possibly already depleted) figure.

**Where a sector sits now changes what it rolls.** A scenario's `map:` block
declares hotspots — placed by hand, scattered from the map seed, or both — and
`reveal_sector()` scales the discovery roll by the largest multiplier covering
the coordinate (`db/sectors.py`'s `richness_multiplier`, layout in
`map_hotspots`). The multiplier scales the whole roll, so a ×3 region rolls
1500–3000 and its floor clears open space's ceiling. Nothing player-facing
reads the layout: a scenario's map is discoverable only by revealing into it.

This gives richness a lever it did not have, but does not by itself make
richness *bind* — see the reserve-not-a-rate question below, which is still
open and is still the substantive one.

#### Open questions — **not decided**

* ~~**Is `HOME_SECTOR_ENERGY = 100,000` higher than it needs to be?**~~
  **Decided 2026-08-16: 2,200.** Eight ships stacked at home draw 64
  energy/turn and the same eight as colonies draw 96, so 2,200 lasts a
  homesteading fleet about 24 turns — past the 20-turn horizon, ending with
  ~376 left. See the throughput entry below for why it is deliberately *not*
  tuned to empty inside a game.

* ~~**Running dry currently pays.**~~ **Fixed 2026-08-16 by the pod rate
  change**, not by touching the score weights. Under the old rates energy
  scored 0 while `produce_energy` cost food, so a dead sector *raised* the
  score rate (112 → 128/turn) and a poorer home scored strictly better —
  3464 at 100,000, 3524 at 2,200, 3568 at 1,800. Now that `produce_goods`
  costs 4 energy and yields 1, energy is genuinely scarce as an input to the
  double-weighted resource, and the ordering inverts: 2605 at 2,200, 2396 at
  1,600, 2102 at 1,200, 1853 at 800. Losing energy supply costs more than the
  food it stops burning. A local rebate still exists at the moment goods
  production stalls; it is no longer big enough to reverse the sign.

* **The binding constraint is throughput, not the reserve.** The pod rate
  change moved it. Per org, two energy pods produce 8/turn (12 as a colony)
  while the org spends 13 — 8 on goods production, 2 on food production, 3 on
  upkeep. So a ship runs −5 energy/turn and a colony −1 **regardless of how
  rich its sector is**, and every strategy converges on the same
  energy-starved equilibrium: a fleet's stored energy floors around turn 15 at
  `home_sector_energy` anywhere from 1,200 to 2,200. Nobody depletes anything
  any more — `sprawl`'s eight colonies draw ~204 each from sectors holding
  500–2,100, and `turtle` runs dry at turn 11 with 1,432 still in the ground
  under it.

  This is the reason `home_sector_energy` is no longer tuned to empty inside a
  game: it cannot bite before throughput does. Colonizing has correspondingly
  stopped being an advantage and become a solvency requirement (−1/turn versus
  −5), which is most of why `turtle` fell from 2240 to 1536. Whether the fix
  is more energy pods in the loadout, a cheaper goods recipe, or letting
  richness drive *rate* (see below) is undecided — but note that the last of
  those is now the same question as this one.

* **Should a sector ever deplete to *zero*?** Currently it can, and does:
  measured over 60 turns, five frontier sectors reached exactly 0 and became
  permanently dead ground (there is no regeneration). Whether total exhaustion
  belongs in the game is unsettled. Alternatives unexplored: a floor below
  which a sector cannot be drawn, slow regeneration, or diminishing returns
  that asymptote rather than terminate. Note the interaction with the abrupt
  failure mode — upkeep is drawn before production, so a fleet on dead ground
  stops completely rather than tapering.

* **Richness is a reserve, not a rate — and that is why it doesn't matter.**
  The most substantive of the three, and now measured precisely rather than
  suspected. `energy_capacity` is purely a depletion budget: a 1000-energy
  sector and a 500-energy one are *identical to work* until the poorer one
  runs out. The binding number is what one org actually draws — **~204 energy
  over a 20-turn game** for a colony with two energy pods (measured directly
  from a dispersed fleet's end-state sectors; it was 318 before the pod rates
  came down, so the rate change made this *worse*). Every sector in the
  discovery band clears that by 300+, so richness is invisible to any player
  who does not concentrate.

  Which sharpens the mechanism rather than only indicting it: richness binds
  exactly when *stacking* exceeds supply. Eight orgs on one sector draw 96/turn
  and can strip anything; one org per sector never exhausts even a bad roll. So
  "how much do I pile onto this find" is the decision richness currently prices,
  and hotspots are meaningful only to a player who concentrates. Confirmed with
  the map layer in place at the old rates: a ×3 region moved `fan_out` (which
  converges its whole fleet on one sector) 2108 → 2228, and moved `sprawl` (one
  colony per sector) not at all. Not re-measured since.

  The idea raised: let richness drive **production rate** as well as duration,
  so a good find pays immediately rather than eventually.
  Undesigned — whether that is a multiplier on pod output, a cap on draw rate,
  or something else, and how it interacts with `COLONY_PRODUCTION_MULTIPLIER`,
  which is already a rate multiplier and would compound with it.

* **Storage capacity does not scale with the colony multiplier.** At 60 turns
  both home colonies finished at 99.5% and 100% of capacity, so their 1.5×
  output became pure waste and the passive player overtook the colonizer on
  turn 47. Decide this before retuning the multiplier — the problem is not that
  1.5× is too strong or too weak, it is that the extra output has nowhere to go.

### 3. Combat — **direction**, phase three

Growing resources and *capturing* resources are alternative strategies, and in
a long game capture may be competitive with cultivation. `_handle_defend` and
`_handle_attack` are no-op stubs (tracked under DB & Engine). This is what
makes a third answer to "where do my resources come from" available alongside
produce-here and move-elsewhere.

### Rotating scanners — **direction**, post-MVP equipment advance

Today an org (or a scan pod) holds **one** offset, set by hand, persisting
until changed — a fixed bearing that travels with the hull. The idea is to let
a scanner hold a *sequence* of bearings and cycle one per turn, sweeping its
surroundings unattended: `N, E, S, W` on an org covers four sectors of rolling
coverage per hull, and nine orgs sweeping four cells each is 36 sectors a turn
with no manual orders.

Deferred because it changes balance rather than mechanics: scanning is meant to
cost something and be chosen, and automation makes it cheap by making it free
of attention. The intended home is an **equipment/technology advance** once
scenarios can grant differentiated kit — a scanner that sweeps is a better
scanner, and should be earned or fitted rather than being how scanning works by
default. Sequence it with sensor pods and ship classes, not before.

The groundwork is right: aim is stored as an offset, so a rotation is a list of
offsets and the resolution step already turns one into a sector.

### Further tensions — **direction**

Not sequenced against the arc above; these are the levers expected to shape the
game.

* **Scenario setup is the tension lever.** A scenario's characteristics — not
  engine constants — are where difficulty and pressure get dialled in, so
  variants can be tuned without code changes. Exposed today: `starting_fill`
  (scenario-wide, overridable per pod template), `pods_per_ship[].storage_capacity`,
  `ships_per_player`, `home_colony`, and each participant's `home_sector`.
  All three shipped scenarios state `starting_fill: 1.0` explicitly to preserve
  their original balance; the field defaults to 1.0 only for compatibility and
  is not a recommended starting point. Still open: nobody has actually tuned a
  scenario with it, and the richness question is deferred until the final-round
  scoring regimen is designed. Note the floor — production consumes resources
  to run, so a scenario starting at or near 0.0 deadlocks its own economy.

* **Per-player loadouts within a scenario.** `pods_per_ship`, `ships_per_player`
  and `starting_fill` are scenario-wide: every participant gets an identical
  fleet. The postulate is that a scenario should be able to give each player
  their *own* loadout — asymmetric starts as a first-class feature rather than a
  fairness violation. The natural home is the participant entry, which already
  carries per-player data (`home_sector`, `is_npc`), with scenario-wide values
  becoming defaults a participant may override — the same cascade
  `starting_fill` uses between scenario and pod template. Enables handicapping,
  asymmetric factions, and a tutorial where the human starts stronger than the
  NPCs. Not designed: whether a participant overrides the whole loadout or
  patches individual fields.

* **The colony numbers are provisional.** `COLONY_PRODUCTION_MULTIPLIER = 1.5`
  gross roughly triples a 6-pod org's net score rate because costs are fixed,
  which may prove too strong once several colonies compound; and
  `COLONIZATION_ENERGY_COST = 30` was picked to establish that conversion has a
  price at all, not sized against what a colony is worth. Retune against play
  data, not analysis.

* **Resource transfer between organizations** — no such tool exists; an org's
  stock is reachable only by its own pods. The play this enables: ship a cargo
  to a fledgling colony that has landed somewhere rich but has nothing to work
  with, letting it bootstrap into abundance. That makes a well-chosen colony
  site worth *investing in* rather than merely worth occupying, and gives ships
  a logistics role distinct from exploration. Undesigned: whether transfer
  requires co-location (almost certainly), whether it costs anything, and
  whether it is one tool or a pair (give/take).

## Design (Data Model)

* [ ] **Ship classes** — not built, not documented. Every `org_type='ship'` row
  is a single undifferentiated archetype; `org_type` is a flat ship/colony
  binary with no stat variation within "ship". The idea as raised:
  - **Ranger** — mobile, fast, longer range, traded against thinner skin
    (durability/cargo; exact tradeoff unspecified).
  - **Transit ability as the class-differentiating stat.** `jump_range_per_turn`
    is a per-call argument defaulting to 1, identical for every hull, so "fast
    ship" has nowhere to live. Once transit stress exists, how far a class moves
    and what that costs becomes the natural axis: a Ranger buys range and pays
    in cargo, a Colony hull is the reverse. Sequence after transit stress —
    range only means something once movement has a price.
  - **Legacy** — general-purpose mix, eventually weapons; the baseline class,
    closest to what every ship is today.
  - **Colony** — heavily loaded, purpose-built for a one-way trip to found a
    colony. Distinct from the `org_type='colony'` flip (what a ship *becomes*);
    this is about loadout before that transition starts.

  Not designed: what fields this needs (a `ship_class` column? a stat-modifier
  table keyed by class?), how it interacts with the `pods_per_ship` templates in
  `config/game*.yaml`, or how it relates to the deferred `pod_type` roster (a
  ship class could plausibly be *defined* by its pod-type mix rather than being
  a separate field).
* [ ] Review sector schema for ownership field creep.
* [ ] Evaluate denormalized active player ID vector on Sector (future).
* [ ] Evaluate Neo4j Community Edition (future).

## Models refactor

* [ ] CRUD logic lives in tool files (`xsettlers_mcp/tools/*.py`) — pull it into
  a model layer once the POC is stable. There is no `models/` package today;
  create one when there is something to put in it, not before.

## TDD rule (standing policy — not a task, keep applying it)

* **No new function without a corresponding test entry.** `test_navigation.py`
  and `test_organization.py` are the templates.
* A new computed field on an *existing* API response gets a new assertion
  appended to that response's existing test, not a new test function — reserve
  new test functions for new functions, new branches, or new error paths.

## Gateway / Player Onboarding

* [ ] `xsettlers_mcp/auth.py`'s roster check is config-file-based, not hardened
  — the "trust identity for now" caveat still stands.
* [ ] Multi-game support (concurrent games, not just switching scenarios) is out
  of scope — `select_scenario` rejects switching once a game is active.
* [ ] Per-game DB file provisioning/routing, and `db/connection.py`'s `DB_PATH`
  becoming per-game. GameHouse's handoff assumes "one shared game per deployed
  instance" exactly as xsettlers does, so it doesn't change this.
* [ ] `events.game_id INTEGER` exists in the schema but is never populated —
  vestigial under the DB-per-game model. Fine to leave; noting it's dead.
* [ ] A GameHouse-driven session doesn't touch `config/game_config.yaml`'s
  directory or `authenticate()` at all — it is a fully separate identity path.
  Whether to eventually retire the static-roster path or keep both is undecided.

### GameHouse handoff — still open

`xsettlers_mcp/gamehouse.py` is xsettlers' side of `../gamehouse`'s wire
contract. Named in GameHouse's own docs as required game-side surface that
doesn't exist yet:

* [ ] A run-state query GameHouse can poll, so it can tell a returning Person
  whether their game is still alive before offering to reconnect. Note the
  results hand-back below already gives GameHouse the *end* of a game
  (`game_journal.status` flips to `completed`), so what is still missing is
  liveness *during* play, not completion.
* [ ] Multi-scenario support on both sides — `scenario_key` is accepted but not
  branched on; today it is always `None` in real traffic.

### GameHouse `join_lobby` dedupe bug — lives in `../gamehouse`, not here

The same real Person joining one lobby twice (client-side retry) fills both of
Diaspora's seats with one identical `player_id`. `close_lobby`
(`gamehouse_mcp/lobby.py`) builds `players` straight from `lobby_member` rows
with no dedup, so it pushes `start_session` with two identical `player_id`s. On
this side, `start_session` derives `email = f"gamehouse-{gh_id}@handoff"` per
seat with no disambiguation, so the two seats collide on `players.email`'s
`UNIQUE` constraint and the push crashes outright — `db/bootstrap.py` does a
plain `INSERT` with no upsert and nothing in the call chain handles the
exception. The fix belongs in GameHouse (reject a second `join_lobby` from a
`person_id` already in that lobby, or dedupe before building the roster).
Deferred until the multi-game work below is settled.

### Multi-game support — deferred design decision

What happens if GameHouse pushes a *second* `start_session` while a game is in
progress? Verified safe today — `gamehouse.py`'s active-game check,
`db/bootstrap.py`'s independent sector-count guard, and `INSERT OR IGNORE` on
the `games`/`game_state` singleton rows are three independent layers, and a
mismatched second push is rejected rather than touching existing data. But
"rejected with an error" is only tolerable because the MVP runs one shared game
per deployed instance. Real multi-game support needs more, and its shape is
decided even though none of it is built:

* [ ] `bootstrap_game()` should mint each game under a uniquely-identified
  database rather than the single shared `DB_PATH`. The identifier is chosen at
  bootstrap time, and a not-yet-designed lobby/router layer routes a
  `player_token` to the right file. Until this lands, xsettlers runs one active
  game per deployment and every subsequent handoff bounces off the guards above.
* [ ] A game-ending tool (doesn't exist — see the run-state item) should, on
  completion, archive that game's database (move it out of the live path, not
  delete it) and mark the game complete in whatever registry tracks multiple
  games. Note the results hand-back is *not* this: it reports the outcome and
  leaves the database exactly where it is, so a finished game still occupies
  the single live `DB_PATH`.
* [ ] Once a game can be marked complete, **GameHouse must never offer a Person
  the option to join or reconnect to a completed game.** Half of this is now
  free: the results hand-back flips `game_journal.status` to `completed`, so
  `open_games` already has the signal for a game that ended normally. What it
  still cannot see is a game that died without finishing.

## NPC strategies

* [ ] **A document cannot size a selection relative to the fleet.** `ships`
  takes `all`, `idle`, or a fixed `slice`/`stride`/`offset` — there is no way
  to say "a quarter of whatever fleet I have". A fractional selector
  (`{fraction: 0.25}`) is the obvious fix, deferred since nothing shipped
  needs it.
* [ ] **No minimum-fleet guard.** A document written for 8 ships just selects
  fewer on a short fleet rather than declining to act. If a precondition is
  ever wanted it belongs in the document format, not rediscovered per
  strategy.

### Fleet strategies, not player strategies

**These are fleet strategies.** A player (NPC or eventually human) can run
several fleets at once, each on its own strategy — one fleet turtling at the
home colony while another fans out. `npc_profiles` is `player_id`-keyed (one
strategy/config/memory blob per player), and fleet-scoped assignment needs a
`fleet_id` instead — but **fleets don't exist in the data model at all**:
`organizations.player_id` is the only ownership link, with no notion of a named
subset of a player's organizations. In the current MVP a player has exactly one
implicit fleet — everything they own — so `player_id`-keying is correct today by
coincidence, not design. Fleet strategies stay NPC-only until "advanced"
versions expose explicit assignment to human players.

Not designed: the `fleet_id` schema, how a fleet is defined/assigned, or
concrete parameters for any of the four registered styles beyond their
behavioural descriptions (see `config/npc_strategies/`).

### NPC builder — what the document format still needs

The point of strategies-as-data is a later phase where a strategy is *authored*
rather than written — and eventually authored by a *player* and traded. Two
properties the current model already has that make that possible: a document
carries no expressions, so accepting one grants no capability; and every
`decide` source is fog-limited in `npc/decide.py`, so an authored strategy
cannot see what its owner has not scanned. What exists is the format and its
validator (`npc/strategy.validate_strategy`, run at assign time by
`assign_npc_profile()` so an authoring tool gets errors synchronously).
Outstanding before a builder is worth writing:

* [ ] **No way to list or describe the vocabulary programmatically.** A builder
  needs the legal actions, phases, selector keys, gates, sources, rank fields
  and picks as data it can render into a form. They are constants
  (`ACTION_NAMES`, `TRIGGER_PHASES`, `ORDER_KEYS`, `DECIDE_KEYS`, and
  `npc/decide.py`'s `GATES`/`SOURCES`/`RANK_FIELDS`/`PICKS`) plus prose in the
  module docstrings.
* [ ] **`assign_npc_profile()` is dev/test-only**, so an authored document has
  no path into a live game except through the GameHouse handoff's roster. A
  builder needs one — and it is the piece that turns "a player can write a
  strategy" into "a player can play one".
* [ ] Fleet-relative selectors and a fleet-minimum guard (see above).

### Tournament standing — stale, needs a re-run

`../xsettlers-designer`'s `xs-tournament` plays every pair of registered strategies
once each, one subprocess and scratch DB per matchup (repeatedly reloading the
SpatiaLite extension across connections in one long-running process segfaults on
the second matchup). `xs-report` renders the results as a static-SVG report.
Both used to live in this repo's `scripts/`; the harness is a separate codebase
now, so a re-run means checking out the designer repo, not this one alone.

Pass `--seed` on any re-run. Sector richness is rolled at bootstrap, so an
unseeded tournament compares strategies on different boards and part of the
resulting ranking is noise — which is worth keeping in mind when reading the
standings below, since they were produced before that flag existed.

Every strategy scores identically regardless of opponent — no combat, no
resource contention at this map scale — so the standings are a fixed, transitive
ranking of solo performance, not real head-to-head play. Seeded (`--seed 42`)
on Diaspora, at the current pod rates:

    sprawl                     2648    one colony per surrounding sector
    homestead                  2605    colonize everything at home, turn one
    turtle                     1536    do nothing
    fan_out                    1464    scout, then converge on the best find
    frontier_map_stay_frosty   1458    never settle

* [x] ~~**Doing nothing still wins.**~~ Resolved 2026-08-16. `turtle` fell from
  2240 to 1536 when the pod rates changed: a ship now runs an energy deficit,
  so standing still is a slow bleed rather than a safe hold, and colonizing is
  what stops it. Two active strategies now beat the control, and `sprawl` —
  which disperses one colony per sector — leads the field for the first time.

* [ ] **`fan_out` and `frontier_map_stay_frosty` are still below the control.**
  Both collapse by turn 4–5: they spend the early turns in transit, where
  energy production is suppressed but consumption is not, and never recover
  enough to colonize. Movement is priced entirely in foregone production, which
  a fleet running an energy deficit cannot afford. Worth deciding whether
  transit should be survivable before adding more reconnaissance strategies to
  a field that punishes reconnaissance.
