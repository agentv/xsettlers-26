# XSettlers — Dev History

Not a changelog. The code and `git log` already record what was built; this
file holds only the things neither of them can tell you:

* **decisions with standing consequences** — including alternatives that were
  considered and rejected, so they don't get re-proposed as if new;
* **findings that cost real play-testing to learn**, and would be expensive to
  rediscover;
* **recovery pointers** — where something deliberately deleted can be found.

If a fact is visible in the code, it does not belong here. See `docs/TODO.md`
for what is still outstanding.

## Decisions

**One SQLite database per game instance**, not a shared database with a
`game_id` column threaded through every table. `games` is a singleton
(`CHECK (id = 1)`) and `select_scenario` rejects switching once a game is
active, so one DB already holds at most one game. A future lobby is a thin
router pointing a player at the right DB file; the alternative would mean
touching `organizations`, `pods`, `events`, `arrival_queue`, `player_sectors`,
and nearly every query in `engine/` and `xsettlers_mcp/tools/`.

**No `gateway.py`, and no per-call wrapper around tool dispatch.** The
original MCP Server Layer design sketched one. Every gameplay tool already
resolves the token, and `players` is empty until `select_scenario` triggers
bootstrap, so every tool rejects on its own before a scenario is picked.
`select_scenario` is the one real gate. `tests/test_gateway.py` is the
end-to-end proof.

**Movement stays Cartesian/Euclidean.** A discrete direction-constrained model
was considered — hexagonal cells in one plane (6 neighbours), or a
face-centred-cubic lattice in 3D (12 neighbours). Deferred, not rejected on
merit: both current scenarios are confined to `z=0`, and the schema stores
plain integer coordinates with no Euclidean semantics baked in, so a later
switch is contained rework rather than a migration. The larger change would be
the *mechanic* — free jump-to-any-coordinate becomes hop-to-adjacent-cell,
which changes the player-facing flow from naming a destination to naming a
direction. Hex-in-one-plane is the natural first step if it is ever revisited.

**Perimeter auth removed; `/mcp` is open, knowingly.** A static
`Authorization: Bearer` header is incompatible with MCP connector flows, which
take a URL and optionally OAuth with no field for a static header — a
connector could only ever get a 401. Reachability won. See the SECURITY
POSTURE comment in `xsettlers_mcp/server.py` for the standing risk.

**A scan reveals only its target sector.** A radius-5 halo around the aim was
considered and rejected: if scanning is to cost something, it must not also be
cheap area coverage.

**Fog decays by flat subtraction, not proportionally.** Proportional decay on
an integer column never reaches zero (`round(4 * 0.9) == 4` is a fixed point),
so every sector a player had ever seen lingered forever and every
`confidence > 0` filter was filtering nothing. When this changed, the knob was
*renamed* (`CONFIDENCE_DECAY` → `CONFIDENCE_DECAY_PER_TURN`) rather than
redefined, so an existing `.env` carrying `0.9` fails loudly instead of
silently meaning "111 turns to forget".

**NPC strategies are data, all of them.** An earlier split — fixed openings as
YAML, anything reactive as a Python function — was collapsed once it was clear
the *reactive* shape is the one future NPCs need, not the fixed openings. A
strategy is a document of `order` and `decide` steps (`config/npc_strategies/`,
walked by `npc/strategy.py`).

The thing that made this possible without turning `org_command_queue` into a
rule engine: conditions live in the *interpreter*, one layer above the log,
which stays one-shot and unconditional. The interpreter decides, then emits
ordinary orders.

Two properties this was chosen for, beyond removing the split. A document
carries no expressions, so accepting a strategy someone else wrote grants no
capability — which is what would make trading them safe. And fog of war becomes
structural: every `decide` source in `npc/decide.py` requires
`player_sectors.confidence > 0`, so no document can name a sector its owner has
not scanned, where a Python strategy could always have queried `sectors`
directly and cheated.

The vocabulary is deliberately minimal and grows by adding *names* to
`npc/decide.py`'s registries, never expressions. Rejected: putting gates on
queue rows; a general expression language; `known_sectors` as the decide source
(a player's home sector carries `HOME_SECTOR_ENERGY`, so ranking all known
sectors by energy picks home every time and no strategy could ever choose to go
anywhere — the source is `scan_targets`, what the fleet's own scans found).

**GameHouse never interprets a score, and never calls back to have one
interpreted.** The results hand-back separates an *envelope* every game
guarantees — `placement` (1 = best) and `score` — from a payload that stays
opaque. That is what makes games-played / best / average / average-placement
pure arithmetic on GameHouse's side: aggregating a number is not interpreting a
game, so the callback hooks that were considered are unnecessary.

`placement` is direction-free (1 is best whether a game is won high or low);
`score` is not, and is comparable only within one game title — which is already
how GameHouse's `player` table is keyed. `scoreboard_schema` declares
`higher_is_better` for whenever a second game makes that matter.

**The hand-back fires from a poll in the server layer, not a hook at
game-over.** Both paths that end a game (`engine/clock.py`'s tick and
`engine/turn.py`'s `check_consensus_acceleration`) live in `engine/`, which may
not import `xsettlers_mcp/`. Rejected: a second function-level import exception
to the layering rule. The poll also covers both paths with one trigger and
recovers across a restart between game-over and a failed push — a hook would do
neither.

Its guard is a **data condition — "is there a session to report to?" — never a
mode flag**, which is why `../xsettlers-designer` needs no knowledge of any of
this: a harness DB simply has no `game_session` row.

**NPC profile assignment is dev/test-only**, deliberately not an MCP tool —
the same boundary as the clock-pause mechanism. Game setup is not something a
player invokes.

**The local package must never be named `mcp/`.** It collides with the
third-party `mcp` SDK it imports; the local package wins resolution
process-wide, and the server self-imports instead of reaching the SDK. Nothing
catches this in tests, because no test imports `mcp.server` directly.

**A scan pays its cost in transit and reveals nothing, on purpose.** Only the
reveal is suppressed while a ship sits at the sentinel sector; the recipe drain
runs first and unconditionally, for a scan pod and an org's own sensors alike
(`engine/turn.py` steps 3c/3d). A player who leaves a scanner deployed on a
voyage wastes its upkeep, and standing the pod down before departure is the
move. Deliberately **not** surfaced as a warning at `set_pod_task` time and not
spelled out in `docs/player_guide.md` — the guide says a scan costs energy and
food, and separately that a ship in transit will not report, and leaves the
inference to the player. A warning would remove the trap that makes the lesson
worth learning. `tests/test_scanning.py` pins the behavior.

**Combat is post-MVP and is not tracked as open work.** `set_mission` refuses
`defend`/`attack` outright (`UNIMPLEMENTED_MISSIONS`, "Weapons are inoperable"),
so `engine/turn.py`'s `_handle_defend`/`_handle_attack` stubs are unreachable
today. Both stubs and step 5's dispatch stay as the seam combat lands in, and
both missions stay in `VALID_ORG_MISSIONS`: dropped from it, the rejection would
enumerate the survivors and read as "this game has no combat", which is false.
Building combat is deleting the refusal set and filling the stubs — well after
the MVP, which is why it is here and not in `docs/TODO.md`.

**No `models/` package until there is something to put in it.** CRUD lives in
`xsettlers_mcp/tools/*.py`. Pulling it into a model layer is worth doing once
the POC is stable, not before, so it is not open work.

**The tick countdown is computed in two places on purpose.**
`scripts/status.py`'s `_clock_status` and `views/format.py`'s `tick_countdown`
both parse `next_tick_at` and `divmod` it (~5 lines each), and are deliberately
not merged: they differ in what they can know. The CLI is its own process and
can health-check the server to tell "paused" from "counting down"; the
in-process one cannot, so a `None` there already means "not running". Both
files comment on the relationship. Revisit only if a third caller appears — at
which point the shared piece is the formatting, not the liveness judgement.

**An NPC strategy *builder* is a later phase.** The format and its validator
exist (`npc/strategy.validate_strategy`, run at assign time), and two properties
already make authoring safe: a document carries no expressions, and every
`decide` source is fog-limited. What a builder would additionally need — the
vocabulary as machine-readable data (today: `ACTION_NAMES`, `TRIGGER_PHASES`,
`ORDER_KEYS`, `DECIDE_KEYS`, and `npc/decide.py`'s
`GATES`/`SOURCES`/`RANK_FIELDS`/`PICKS` plus module docstrings), a live path for
an authored document beyond dev/test assignment, and fleet-relative selectors —
is scoped to that phase, not this one.

**Energy production is suppressed in transit, and that is a cost of moving.**
A ship under `mission='move'` reports `E:0` with its pod tasking unchanged,
while food and goods keep running — and since both consume energy while energy
consumes nothing, a long voyage burns down carried energy with no way to
replenish it and can arrive unable to restart its own economy. Ruled deliberate,
the same way the scan-in-transit cost was: moving is expensive, and a player
learns that by paying for it. Not surfaced as a warning, and not spelled out in
the player guide.

**Task forces (built 2026-08-22).** A player-named, explicitly managed roster of
a player's own ships — never colonies, and a ship belongs to at most one at a
time, both enforced at the tool layer (`xsettlers_mcp/tools/task_force_tools.py`:
`create_task_force`, `add_to_task_force`, `remove_from_task_force`,
`disband_task_force`, `list_task_forces`). Storage is a `task_forces` table plus
a nullable `organizations.task_force_id` — no new engine mechanics and no new
turn-resolution step. There is no co-location requirement anywhere, by design.
Membership changes only by direct action, with one automatic exception: a member
that colonizes leaves, `task_force_id` cleared in the same statement
`engine/turn.py`'s `_handle_colonize` uses to flip `org_type`, since a task force
cannot hold anything but a ship.

`order_task_force` fans a **mission** order out to every current member's own org
id through `set_mission` itself — a fan-out, not a transaction, so every per-org
lock `set_mission` already enforces applies per ship, and a member that cannot
accept (in transit, mid-colonization) fails alone and reports why while the rest
go through. Deliberately **scoped narrower than the original direction**: only
`set_mission` is fanned. A `set_pod_task` fan-out is still open work — see
`docs/TODO.md`.

**Scenario selection belongs to GameHouse, and the list travels by push.** A
Person picks a game *and* a scenario from GameHouse's `list_games()` before
`join_lobby` is ever called (settled on GameHouse's side 2026-08-07,
`../gamehouse/docs/data_model.md`); the choice reaches xsettlers as
`start_session`'s `scenario_key`. The list gets there at **registration** —
xsettlers announces its scenarios via `register_game`, GameHouse stores them in
`game_scenario` and replaces them wholesale on the next registration. It is not
a pull: GameHouse never interrogates this service for its catalogue, which is
what lets it describe a game to a Person while that game is restarting, and what
keeps lobby matching (on the (game, scenario) pair) reading from one local
table. xsettlers' own `list_scenarios`/`select_scenario` stay live for direct,
non-GameHouse play — the two identity paths do not meet.

**The turn interval starts when the game does, not when the process does.**
`run_clock()` runs from server startup, but a game is bootstrapped at an
arbitrary moment inside a tick window. `end_of_turn()` no-ops without a `games`
row, so no turns were ever *burned* pre-selection — but `elapsed` kept
accumulating, so turn 1 inherited whatever was left of the window already
running and could be seconds long at a 300s cadence. The clock now holds
`elapsed` at zero until a game exists and restarts the window when one appears.
`engine/clock.py` reads `games` directly rather than through
`xsettlers_mcp.game_select`, since engine/ never imports upward.

**Lobby sizing moved onto the scenario in GameHouse (2026-08-26).** A
`register_game` scenarios entry is now either a bare key or an object carrying
that scenario's own `min_players`/`max_players`/`wait_window_seconds`, with the
game-level numbers as the fallback; `gamehouse_mcp/registry.lobby_shape()`
resolves the pair and every lobby decision reads through it. xsettlers publishes
all four scenarios with their own sizing (`registered_scenarios()`), so Solo — 1
player on a 0s window — is offerable alongside Diaspora's 2 for the first time.
Before this, one registration carried one shape for the whole service and Solo
had to be withheld rather than mis-lobbied. The bare-key form is unchanged, so
neither side needed the other to deploy first.

**The GameHouse duplicate-join crash is closed from both ends.** `join_lobby` is
now idempotent per person upstream, so one Person retrying a join no longer
fills two seats with one `player_id`. On this side `start_session` refuses a
roster containing the same `player_id` twice instead of raising out of
`bootstrap_game()` on `players.email`'s UNIQUE constraint — the synthesized
`gamehouse-<id>@handoff` address is what collides, and an error GameHouse can
read beats a traceback.

## Findings from play

**A tool declared in three places will eventually disagree.** Before the
`@mcp_tool` registry, a tool's schema meant a hand-written inputSchema in
list_tools() plus a matching entry in call_tool()'s dispatch dict plus the
import in server.py -- three places for each of 22 tools to agree by hand.
One disagreement cost a live cross-process handoff: start_session's
hand-written schema said `scenario_key` was a string, which JSON Schema
rejects for `null`, while the Python function happily accepted `None`.
`@mcp_tool(description=...)` derives the schema from the function signature
instead, so it cannot drift from what the function actually takes.

**Starting at full capacity breaks the economy.** A fleet at 100% fill cannot
accumulate, so production is pure waste and only *spending* moves the score.
Measured: holdings sat at exactly 5400 for four turns at 100% fill while score
crept up purely by reshuffling toward goods; the same scenario at 25% grew
~288/turn. Hence `starting_fill` as a per-scenario dial.

**Fleet capacity has to be scarce to matter.** A full 20-turn playthrough
ended in an exact tie at 14400 points each, with neither fleet ever close to
depleting or overflowing anything — net drift of about -60 energy and +100
goods per turn against a 14400 capacity. Pod loadout was cut from 18 to 6 per
ship to make scarcity a real decision.

**Turn resolution is cheap.** `end_of_turn()` averages ~20ms at bootstrap
scale (16 orgs, 96 pods) including the per-player ledger writes — about 0.007%
of a 300s tick. Moving any of this to a task queue or background thread would
be over-engineering; everything runs synchronously and inline.

**Both NPC ports were proven equivalent, not assumed.** The pattern: seeded
runs (`--seed`) of the same matchup before and after, diffing standings,
per-turn holdings, *and* the `events` table. Repeat it for any refactor
claiming to change nothing.

The first port (openings from Python into YAML) was byte-identical including
`seq`. The second (every strategy into a document) was identical in standings,
per-turn holdings and events — 65 rows for `fan_out`, 121 for
`frontier_map_stay_frosty` — with one expected difference: intra-turn `seq`
renumbers, because a document orders all moves then all aims where the Python
interleaved move-then-aim per ship. Same events, same turns, same payloads,
different order within the turn.

**Retired rather than ported:** `burst_and_colonize` and `fan_out_consolidate`.
Both are fixed openings, already analysed, and not interesting to play against.
Retiring them also avoided the one thing the document model cannot express at
bootstrap — an `after_arrival` order, which `queue_command` will only accept
against a move already under way.

**The game-design harness is a separate codebase** (`../xsettlers-designer`), and the
split is between gameplay code and designer activity, not between "app" and
"scripts". Tournament running, matchup simulation and analysis reporting moved
out; `scripts/clock.py` and `scripts/status.py` stayed, because they operate a
*live* server rather than designing a game.

The designer repo installs this one editable and calls `engine.turn.end_of_turn()`
in-process. Alternatives rejected: driving it over MCP (there is no "resolve a
turn now" tool, and adding one means an admin surface on an endpoint with no
perimeter auth — and it would be slow besides); vendoring a copy of the engine
(guaranteed drift); a built wheel rather than an editable install
(`npc/library.py` resolves `config/npc_strategies/` relative to `__file__` and
scenarios are read as ordinary files, so a wheel would need every YAML declared
as package data and would still hand the designer repo a frozen copy of the scenarios it
exists to edit).

This is what `pyproject.toml` in this repo is for, and its only consumer. The
Fly build does not use it.

**Seeded tournaments.** `SECTOR_ROLL_SEED` always existed in `db/sectors.py`
but no harness set it, so every tournament before the extraction played each
matchup on a different board. The designer repo exposes it as `--seed`. Standings
recorded before that are comparable within a matchup but not across them.

## Recovery pointers

**The tournament harness.** `scripts/simulate_npc_matchup.py`,
`scripts/run_tournament.py`, `scripts/build_tournament_report.py` and the
`tourney/` results directory were removed from this repo after the extraction
was verified byte-identical (same seeded standings, same per-turn holdings,
same rendered report). They live in `../xsettlers-designer` now. For the pre-move
versions, recover from commit `b6032c5` rather than reconstructing them.

**Schema migrations.** `db/schema.py` carries no *general* migration step — the
`CREATE TABLE` statements are the whole schema. The one exception is
`_add_missing_columns()`, which adds a nullable column to a table that already
exists on a deployed volume: no version table, no ordering, no data rewriting,
idempotent on every boot. It was added rather than skipped because
`CREATE TABLE IF NOT EXISTS` silently does nothing for an existing table, and
the deployed volume holds a *finished* GameHouse game — which has both a
session token and recorded final scores, so the results reporter sails past
every guard and into a query for a column that isn't there, logging a failure
every poll forever. Adding a column without adding it to `ADDED_COLUMNS` is how
that bug comes back.

One-time migrations for
databases created before 2026-08-02 (`slack_user_id` → `player_token`,
`dest_sector_id` → `dest_x/y/z`, `storage_current` → per-resource columns,
`pods.mission` → `pods.task`, absolute `scan_target_*` → relative
`scan_offset_*`) were removed after confirming they were no-ops against the
only live database. If an older database ever turns up, recover them from
commit `14b5fa2` rather than rewriting them from memory.

**Scenario names swapped once.** "Diaspora" is `config/game0.yaml` (ships
only, no home colony); "Outbreak" is `config/game1.yaml` (colony + fleet).
Anything written before that swap has them the other way round.

## Rival detection (built 2026-08-18)

~~`engine/turn.py` — **rival detection is unbuilt**~~ Built 2026-08-18. A scan now reveals the organizations standing in its target sector as well as the sector's own resources, each org rolling its own d6 detection check (`db/sightings.py`, threshold 6 of 6 — certain for now, and the die is rolled anyway so lowering it later changes odds without shifting a seeded run's roll sequence). Sightings land in the new `org_sightings` table, one row per (observer, org), upserted on re-sighting, and a `scan.contact` event names what was detected. Intel is per sector and ages on the ordinary fog-of-war schedule: sightings are read only through `player_sectors`, so they inherit the sector's confidence and blink out with it at 0, and a scan is authoritative for its sector — what it finds replaces what you believed, so an emptied sector stops reporting a ghost. `show_sector_neighborhood` distinguishes "R" (a rival there now, shown only where you stand) from "r" (one a scan saw there). **Still unbuilt: the `pod.scanned`/`org.scanned` events**, and no NPC strategy scans toward an opponent, so nothing in the library produces contact on its own — see the Crowd note under NPC strategies.
