# XSettlers — Design Direction

Decided or explored design that is **not built**. Split out of `docs/TODO.md`,
which is for work that still needs doing; this is the reasoning behind where
the game is going, read when you are working on the area it covers.

## Distance metric — Euclidean for the MVP, revisit later

* [ ] **Distance is Euclidean everywhere, and that is a deliberate MVP choice, not an oversight.** Movement (`navigation_tools.py`: `ceil(euclidean / jump_range_per_turn)`), scan range, and the neighborhood viewport all use straight-line distance on integer coordinates. Consequences observed in play:
  - A diagonal move costs the same as three orthogonal steps (√8 ≈ 2.83 → `ceil` 3), so diagonals are consistently poor value for the displacement gained.
  - Scan radius produces a plus-shape at range 1 and a 12-cell rosette at range 2, never a square. Range 3 would reach 28 cells in-plane; growth is quadratic in-plane and cubic once `z` is used.

  The alternative worth weighing is **Chebyshev** distance (`max(|dx|,|dy|,|dz|)`), under which a diagonal costs the same as an orthogonal step and radius *r* is exactly the (2r+1)³ box — much closer to how players read a grid. Long-term the intent is something more sophisticated than either; the likely shape is that movement and sensing stop sharing one metric, since "how far can I travel" and "how far can I see" are not obviously the same geometry. **Staying Euclidean for the MVP** — noted so the eventual change is a decision rather than a surprise.

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

* [x] ~~**Task forces** — direction, not built~~ Built 2026-08-22. A
  player-named, explicitly managed roster of a player's own ships — never
  colonies, and a ship belongs to at most one at a time, both enforced at the
  tool layer (`xsettlers_mcp/tools/task_force_tools.py`): `create_task_force`,
  `add_to_task_force`, `remove_from_task_force`, `disband_task_force`,
  `list_task_forces`. Storage is a `task_forces` table plus a nullable
  `task_force_id` on `organizations` — no new engine mechanics, no new
  turn-resolution step. Membership changes only by direct action; the one
  automatic exception is a member that colonizes, which clears
  `task_force_id` in the same statement `engine/turn.py`'s `_handle_colonize`
  uses to flip `org_type`, since a task force cannot hold anything but a ship.
  No co-location requirement anywhere, matching the direction as designed.

  `order_task_force` fans a **mission** order (`set_mission`'s own
  mission/params) out to every current member's org id, independently — not a
  transaction, so one member that cannot currently accept the order (in
  transit, mid-colonization, wrong state) fails only for that member and
  reports why, while the rest still go through. **Scoped narrower than the
  original direction**: only `set_mission` is fanned. A `set_pod_task` fan-out
  was left out because, unlike a destination or a mission, a pod id does not
  generalize across members with different pod loadouts — the direction's
  "same pod retask" language doesn't specify a selector (by index? by
  matching current task?), and that's a real design decision, not a detail to
  default silently. Pick one and add it as its own tool when a fleet actually
  needs pod-level task-force orders.

### Resource transfer between organizations — **direction**, not built

A new org-level action — `transfer` — moving one resource type and an amount
from one of a player's own organizations to another. Org-scoped, not
pod-scoped: it draws from and credits an org's pooled total, the same figure
`apply_colonize` already reads to check affordability, not any particular
pod's storage.

Ordering it requires the two organizations to currently share a sector.
Nothing is escrowed at that point — the resource stays live in the sender's
own economy, spendable by its own production and upkeep, right up until
resolution.

**Resolves one tick later, and has to be the first thing `end_of_turn()` does
— ahead of arrivals, ahead of everything.** Co-location is rechecked at
resolution using each org's position as of the *start* of that turn, before
any of the turn's own movement can change it. Running this step after
arrivals would let a transfer complete on a sector pairing that only came into
existence during the very turn being resolved — two orgs that only just met,
credited as though they'd been together for the whole wait. Resolving
transfers first closes that off: a transfer only ever completes between
organizations that were already together going into the turn.

If the two organizations are no longer co-located at resolution: the transfer
does not happen. The sender keeps everything, exactly as if it had never been
ordered.

If they are still co-located: the sender loses whatever of the resource it
currently holds, capped at the amount originally ordered — never more than
what's actually there, so a sender that has spent some of it down in the
intervening turn simply sends less rather than being refused outright. The
receiver gains that amount capped at its own free capacity (total storage
capacity across its pods, less whatever it already holds); anything beyond
that is destroyed — not returned to the sender, not held anywhere.

Needs a **credit** counterpart to the org-pool drain `apply_colonize` already
uses to pay its cost — that helper only ever drains an org's pooled resource
today; crediting one, spread across whichever of the receiving org's pods have
room, is new.

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
  table keyed by class?), or how it interacts with the `pods_per_ship` templates
  in `config/game*.yaml`. Note a ship class cannot be defined by its "pod-type
  mix": pods have no type, only an assignable task, so what a ship carries is
  identical between two ships and only what their crews are doing differs.
* [ ] Review sector schema for ownership field creep.
* [ ] Evaluate denormalized active player ID vector on Sector (future).
* [ ] Evaluate Neo4j Community Edition (future).

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
* [x] Archiving is automatic now: `xsettlers_mcp/gamehouse.py`'s results
  reporter loop calls `db/archive.archive_active_database()` once
  `_game_settled()` is true (game over, and either there was no GameHouse
  session to wait for or the hand-back already succeeded) — it moves
  `DB_PATH` to `<DB_PATH>.finished-<UTC timestamp>` and reinitializes an
  empty schema in its place, so the running process can accept the next
  `select_scenario`/`start_session` without a restart. Note the results
  hand-back itself is *not* this: it reports the outcome and leaves the
  database where it is; archiving is a separate, later step gated on the
  hand-back being done so it can never strand a pending scoreboard.
* [ ] Still missing: a registry that tracks multiple finished games (today
  a finished game is just a timestamped file on disk, nothing queryable),
  and anything to route a `player_token` to the right one — both blocked on
  the per-game-DB routing item above.
* [ ] Once a game can be marked complete, **GameHouse must never offer a Person
  the option to join or reconnect to a completed game.** Half of this is now
  free: the results hand-back flips `game_journal.status` to `completed`, so
  `open_games` already has the signal for a game that ended normally. What it
  still cannot see is a game that died without finishing.

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

* [ ] **Scoring and seeing are mutually exclusive, and seeing loses.**
  `fan_out`, `frontier_map_stay_frosty` and `survey` all sit below the control.
  Each collapses by turn 4–5: they spend the early turns in transit, where
  energy production is suppressed but consumption is not, and never recover
  enough to colonize. Movement is priced entirely in foregone production, which
  a fleet running an energy deficit cannot afford.

  `survey` makes the shape of it plain. It reveals **80 sectors** and detects
  every ship of a homesteading rival by turn 3, and finishes at 1475 — below
  `turtle` (1536), which reveals **one** sector and never learns anyone else
  exists. Meanwhile `sprawl` wins the field at 2648 having scanned nothing at
  all. Nothing currently lets a player both look and score, so contact is a
  hobby rather than a strategy, and detection has no consumer that benefits
  from it. Decide whether transit should be survivable before adding more
  reconnaissance strategies to a field that punishes reconnaissance.
