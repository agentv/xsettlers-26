# XSettlers — Scenarios and NPC Strategies

The catalogue of what actually ships: every playable scenario, every NPC
strategy in the library, and what each one does when you run it. Written to
feed the Player Guide — this doc is the inventory, `player_guide.md` is the
rulebook.

Every figure here is **measured, not estimated**: a 20-turn game on seed 42,
one strategy per fleet. Because nothing in the game yet contests anything, a
solo run and a head-to-head run produce identical scores, so the numbers below
are equally true of either.

Regenerate them with the designer harness in `../xsettlers-designer`:

```bash
xs-tournament --scenario game0 --seed 42
```

Anything that changes `engine/production.py`, `config/loader.py`'s
`HOME_SECTOR_ENERGY`, or the strategy library invalidates this table. Re-run
before trusting it.

---

## Scenarios

Four scenarios, **one economy**. All of them use 8 ships, a 2/2/2 pod loadout
at 100 capacity, `starting_fill: 0.3`, and `home_sector_energy: 2200`. They
differ in three things only: how many players there are, whether a colony is
waiting at home, and how far apart the homes sit.

| Scenario | File | Players | Homes | Apart | Home colony |
|---|---|---|---|---|---|
| Diaspora | `config/game0.yaml` | 2 | (25,25,0) (25,50,0) | 25.00 | no |
| Outbreak | `config/game1.yaml` | 2 | (25,25,0) (12,12,0) | 18.38 | yes |
| The Crowd | `config/game_crowd.yaml` | 2 | (15,20,0) (20,15,0) | 7.07 | no |
| Solo | `config/game_solo.yaml` | 1 | (10,10,0) | — | yes |

**Diaspora** is the default. Everyone begins mobile and picks their own moment
to colonize.

**Outbreak** starts each player with an established colony at home — the only
scenario where a colony exists before anyone decides to build one.

**The Crowd** exists to ask whether fleets ever meet. Blue and Red are mirrored
across the x=y diagonal, so neither has a positional edge, and the separation
is roughly one scout hop plus a scan. Note the scale it is asking about: scan
range is **2 sectors** against a separation of **7.07**, so neither player can
see the other from home even in principle.

**Solo** is the proof that roster size is data rather than code. A solo game is
a participants list with one entry; nothing branches on player count.

No shipped scenario declares a `map:` block yet, so every sector rolls the
ordinary 500–1000 (see "Built, not yet used" below).

---

## NPC strategies

Every strategy is a document in `config/npc_strategies/*.yaml`, walked by
`npc/strategy.py`. There is no Python strategy and no registry of them —
adding one is adding a file.

| Strategy | Role | Score | Colonies | Sectors seen | Still on map | Steps |
|---|---|---|---|---|---|---|
| `sprawl` | settles | 2648 | 8 | 9 | 8 | move → colonize |
| `homestead` | settles | 2605 | 8 | 1 | 1 | colonize |
| `turtle` | inert | 1536 | 0 | 1 | 1 | — |
| `survey` | looks | 1475 | 0 | 89 | 24 | move → aim_scan ↺ |
| `fan_out` | looks | 1464 | 0 | 9 | 1 | move → aim_scan → decide → move |
| `frontier_map_stay_frosty` | looks | 1458 | 0 | 25 | 8 | move → aim_scan ↺ |

"Sectors seen" is every sector the player ever revealed; "still on map" is how
many were still above confidence 0 at the final turn — the difference is fog of
war having caught up with a fleet that moved on.

**`sprawl`** — one ship to each of the eight adjoining sectors, colonizing on
arrival. It leads because eight colonies draw on eight separate energy pools
rather than sharing one. It uses the map to win without ever looking at it.

**`homestead`** — colonize every ship where it stands on turn one, and never
move again. Three lines of YAML, and the strategy that established the map was
decoration. Still within 2% of the lead.

**`turtle`** — an empty document, and a real control rather than a placeholder.
It used to beat everything; it now loses to both settlers, because a ship that
never colonizes runs an energy deficit and slowly starves.

**`survey`** — scouts all eight directions, scanning ahead every turn. The only
strategy written to find *people* rather than places. It reveals 89 sectors and
detects every ship of a static rival by turn 3, and finishes below `turtle`,
which reveals one sector and never learns anyone else exists.

**`fan_out`** — scatter, wait for every scout's reading, then send the whole
fleet to the richest find. The document that forced the `decide` vocabulary
into existence. Its convergence is also what strips a single sector fastest,
which makes it the strategy most exposed to sector richness.

**`frontier_map_stay_frosty`** — never settle; every landed ship is immediately
sent further out. Mobility as identity, and the clearest demonstration that
transit is currently a slow death.

### The flock role

Several existing strategies already serve as **flocks** — stationary NPCs that
accumulate resources and become a strategic opportunity for a player who finds
them. `homestead` and `sprawl` build colonies and stockpile without ever
moving, scanning, or defending; `turtle` holds position and accumulates from
turn one. A player who locates one of these has found something worth acting
on.

Nothing yet lets them act on it: `_handle_defend` and `_handle_attack` are
stubs, so there is no mechanism to take anything from anyone. The flock role is
occupied; the interaction that would make it pay is not built.

### The `decide` vocabulary

The hook for information that only exists mid-game. It grows by adding **names
to registries** in `npc/decide.py`, never by adding expressions — that is what
keeps a document inert data, and therefore safe to accept from someone else.

| Kind | Available |
|---|---|
| gates | `all_scans_resolved` |
| sources | `scan_targets` |
| `rank_by` | `energy_capacity` |
| `pick` | `max`, `min` |

Every source requires `confidence > 0`, which is what makes fog of war
structural: no document can name a sector its owner has not seen.

---

## The economy in force

Per pod, per turn (`engine/production.py`):

| Task | Produces | Costs | Net E | Net F | Net G |
|---|---|---|---|---|---|
| `produce_energy` | 4 energy | 1 food | +4 | −1 | · |
| `produce_food` | 5 food | 1 energy, 1 goods | −1 | +5 | −1 |
| `produce_goods` | 1 goods | 4 energy, 1 food | −4 | −1 | +1 |
| `scan` | — | 1 food, 2 energy | −2 | −1 | · |
| `idle` | — | — | · | · | · |

- **Org upkeep** — 5 food + 3 energy per organization per turn, flat regardless
  of pod count, charged in transit too. A tax on holding many small orgs.
- **Colony bonus** — ×1.5 on output only, costs unchanged, for a one-time 30
  energy. Net production is a small difference of large numbers, so a 1.5×
  output multiplier becomes roughly a 2.8× swing in score.
- **Score weights** — energy 0, food 1, goods 2. Energy is purely an input.
- **The binding constraint is throughput, not the reserve.** Two energy pods
  make 8/turn (12 as a colony) against 13 spent on goods, food and upkeep, so
  every org runs an energy deficit whatever its ground holds. Nobody depletes a
  sector any more — fleets starve first, around turn 15.

---

## Built, not yet used

**The scenario map layer.** A scenario may declare `hotspots` by hand and a
seeded `scatter` rule; richness scales the whole discovery roll, so a ×3 region
rolls 1500–3000 and its floor clears open space's ceiling. Overlapping regions
take the largest multiplier, never the product. The layout is secret by
construction — no tool reads it, so a player discovers a region by revealing
into it. **No shipped scenario declares one**, and richness only binds a player
who concentrates enough orgs on one sector to strip it.

**Scan detection.** A scan reveals the organizations standing in its target
sector as well as the sector's resources. Each org rolls a d6 against
`DETECTION_THRESHOLD` (currently 6 of 6 — certain, and still rolled, so
lowering it later changes odds without shifting a seeded run's roll sequence).
Resources are never missed; only fleets can hide. Intel is per sector, replaced
by each look, and ages on the ordinary fog-of-war schedule. **Only `survey`
uses it.**

---

## The open problem

**Scoring and seeing are mutually exclusive, and seeing loses.** All three
strategies that scan sit below the do-nothing control. Transit suppresses
energy production but not consumption, so every turn spent looking is paid for
in production a deficit-running fleet cannot spare.

`survey` pays 89 sectors of vision and complete knowledge of an enemy fleet for
last place. `sprawl` wins having scanned nothing at all.

This is the constraint underneath most of what comes next. Flocks are already
in the field, but a player cannot profitably go and find one; dynamic NPCs will
face the same toll. Deciding whether transit should be survivable comes before
either. Tracked in `docs/TODO.md`.
