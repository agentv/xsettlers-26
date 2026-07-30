# XSettlers — Game Instance: MVP

# Overview

This canvas defines the first playable MVP instance of XSettlers. It is a self-contained game definition — all parameters here map directly to `game_config.yaml` values. Different game types (larger maps, 3D space, combat enabled, different pod loadouts) are supported by creating a new config; no code changes are required for parameters marked as config-driven.

---

# Map

* **Coordinate system:** always 3D — sectors use integer coordinates `(x, y, z)`. In this instance, **z is locked at `0`** for all sectors. This is a game instance constraint, not an architectural one.
* **Grid size:** 16 × 16 = 256 sectors. Coordinates range from `(0,0,0)` to `(15,15,0)`.
* **Config flag:** `z_locked: true` in `game_config.yaml`. Set to `false` in a future game type to activate full 3D play — no code changes required.
* **Energy barrier:** all sectors with any negative coordinate are inaccessible. Only `x ≥ 0`, `y ≥ 0`, `z ≥ 0` are playable.
* **Sentinel sector:** `id = -1`, coords `(-1, -1, -1)` always, regardless of map dimensionality. Used as the transit parking state for ships in motion. Players are informed when their ship is here.
* **Resource distribution:** each sector has `energy_capacity`, `food_capacity`, and `goods_capacity` values seeded at bootstrap from `game_config.yaml`.

---

# Players & Starting Positions

* **Player count:** 2
* **Player 1 home sector:** `(3, 3)`
* **Player 2 home sector:** `(12, 12)`
* Straight-line distance between home sectors: ~12.7 — approximately 6 turns at default jump range. Close enough for mid-game contact; far enough for a meaningful early exploration phase.
* Each player begins with a **home colony** pre-placed at their home sector. Confidence is pinned at 100 for the home sector from turn 0.
* **Hook:** starting positions are config-driven. Future game types may use random placement, symmetric placement, or admin-assigned positions.

---

# Ships

* Each player begins with **8 ships**, all spawned at their home sector on turn 0.
* All 8 ships are **identical** at game start.
* Ships can move, scan (when not in transit), and carry pods that produce every turn.

## Starting Loadout (per ship)

Each ship carries **6 pods**:

| Pod Type | Count | Default Task | Produces |
|---|---|---|---|
| `energy` | 2 | `produce` | Energy |
| `factory` | 2 | `produce` | Goods |
| `farm` | 2 | `produce` | Food |

* All pods are **active from turn 1** with default tasks assigned at bootstrap. No setup action required.
* Players may reassign any pod's task at any time using `set_pod_task`. Valid tasks: `produce`, `mine`, `idle`.
* **Hook:** a future `reconfigure_pods(org_id, pod_manifest)` action will allow players to change pod types entirely (e.g. swap factory pods for additional sensor pods). The data model supports this already — pods are rows, not a fixed schema.

> **Note:** this table's `Pod Type` / `Default Task` vocabulary predates the mission-based pod model in [Data Model & Storage Design](data_model_and_storage_design.md) and [Product Requirements](product_requirements.md), where pods carry a `mission` (`produce_energy`/`produce_food`/`produce_goods`/`scan`/`idle`) rather than a `pod_type` + `task` pair. For bootstrap seeding, treat the 6/6/6 per-ship counts and colloquial names (`energy`→`produce_energy`, `factory`→`produce_goods`, `farm`→`produce_food`) as authoritative, mapped onto the `mission` field.

---

# Pod Types

Three pod types are **active in this game instance**. The full pod roster (including deferred types) is documented in the [Product Requirements](product_requirements.md).

* **`energy`** — produces energy each turn. Energy powers other pods and contributes to movement capacity. Future: energy deficit will throttle production.
* **`factory`** — produces goods each turn based on sector `goods_capacity` and task assignment.
* **`farm`** — produces food each turn based on sector `food_capacity` and task assignment.

**Deferred pod types** (not active in this instance): `crew`, `cargo`, `defense`, `attack`, `ship`, `sensor`. These are recognized in the data model but not instantiated. See the [Product Requirements](product_requirements.md) for the full roster with descriptions.

---

# Turn Limit & Win Condition

* **Turn limit:** 20 turns. The game ends automatically at the close of turn 20.
* **Win condition:** the player with the highest **total resource score** at end of turn 20 wins.
* **Score calculation:** sum of `storage_current` across all pods, across all organizations (ships and colonies) belonging to the player at the moment turn 20 resolves.
* All three resource types (energy, goods, food) count equally toward score in this instance.
* **Hook:** future game types may apply resource weightings, introduce objectives, or use a different scoring formula. Score calculation is config-driven.

---

# Game Loop (20 Turns)

The intended arc of play:

1. **Early turns (1–6):** explore outward from home sector. Scan adjacent sectors. Begin colonizing high-capacity sectors. All ships producing from turn 1.
2. **Mid game (7–14):** rival contact becomes possible. Contested sectors may appear. Colonization race intensifies. Pod task reassignment becomes strategic.
3. **Late game (15–20):** consolidate holdings. Maximize production in occupied sectors. Score is locked at end of turn 20 — late arrivals to rich sectors may not have time to accumulate.

---

# Feature Flags (this game instance)

| Feature | This Instance | Config Key |
|---|---|---|
| Z-axis | Locked at 0 | `z_locked: true` |
| Combat | Disabled | `combat_enabled: false` |
| Sensor pods | Disabled | `sensor_pods_enabled: false` |
| Pod reconfiguration | Disabled | `pod_reconfig_enabled: false` |
| Turn limit | 20 | `turn_limit: 20` |
| Score weighting | Equal (1×) | `score_weights: {energy: 1, goods: 1, food: 1}` |

---

# Out of Scope (this instance)

* Combat (`defend` / `attack` missions stubbed, not active)
* Sensor pods and variable scan range
* Pod reconfiguration mid-game
* Resource consumption / energy deficit mechanics
* More than 2 players
