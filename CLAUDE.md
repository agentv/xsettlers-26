# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

XSettlers is a multiplayer space strategy game, playable from any MCP-speaking client. This repo is the Python MCP server: a client calls MCP tools with a player's token attached, tools query/mutate a SpatiaLite database, and a background clock resolves turns on a fixed interval. There is no separate web/API layer — `xsettlers_mcp/server.py` *is* the server, serving MCP's **streamable HTTP** transport (Starlette + uvicorn, `POST /mcp`, `GET /health`) so a network-hosted deployment (Fly.io) can be reached remotely. There is no stdio path: stdio only works for a client that spawns the process locally.

**Identity is client-agnostic.** Every tool's first argument is `player_token` — an opaque per-player secret compared with `hmac.compare_digest` in `xsettlers_mcp/auth.py`, not a platform credential. Slack, curl, another LLM agent, or anything else that knows a valid token authenticates identically.

**`/mcp` has no perimeter auth — it is open to the internet.** A static `Authorization: Bearer` gate is impossible here: MCP client connector flows take a server URL and optionally OAuth, with no field for a static header, so a gated endpoint could only ever return 401. Access control rests entirely on `player_token`. Two things make that thin: the roster's tokens are placeholders in a public repo, and there is no rate limiting — accepted knowingly while the game holds nothing of value, and untrue the moment that changes. See the SECURITY POSTURE comment in `xsettlers_mcp/server.py` and `docs/TODO.md`.

**The local package is `xsettlers_mcp/`, never `mcp/`.** That name collides with the third-party `mcp` SDK package that `xsettlers_mcp/server.py` itself imports (`from mcp.server import Server`, `from mcp import types`) — whichever `mcp` Python resolves first wins process-wide, and the local package always wins, making the server circularly self-import instead of reaching the SDK. No test catches this, because no test imports `mcp.server` directly.

Documentation lives in `docs/` and is the source of truth for design. Read `docs/TODO.md` first when picking up work; `docs/dev_history.md` holds settled decisions, findings from play-testing, and recovery pointers.

## Commands

```bash
# Install deps
pip install -r requirements.txt

# Run the server (streamable HTTP MCP server + background clock, run together via asyncio.gather)
# Listens on :8080 (PORT env var override) -- POST /mcp, GET /health
python -m xsettlers_mcp.server

# Run all tests
pytest

# Run a single test file / test
pytest tests/test_navigation.py
pytest tests/test_navigation.py::test_confirm_move_parks_at_sentinel -v

# Operator scripts (never MCP tools -- not reachable with a player_token).
# These act on a LIVE server; game-design tooling lives in ../xsettlers-designer.
scripts/clock.py freeze|unfreeze|status   # hold the background tick without stopping the server
scripts/status.py game|fleet              # fixed-format status report
```

The local `xsettlers.db` is scratch — safe to `rm` and restart clean.

**Game-design tooling is a separate repo (`../xsettlers-designer`), not part of this codebase.** Strategy tournaments, fast-forwarded matchup simulation and analysis reports live there. The dependency points one way: the designer repo installs this one editable (`pip install -e ../xsettlers26` — that is what `pyproject.toml` here exists for) and drives `engine.turn.end_of_turn()` in-process, because there is no wire call for "resolve a turn now" and there should not be one. Nothing here imports it. Two consequences worth knowing before changing anything in `engine/`, `db/` or `npc/`: it is a real consumer of those modules, and `pyproject.toml`'s package list has to keep matching this repo's top-level packages. The Fly build ignores `pyproject.toml` entirely — the Dockerfile runs `pip install -r requirements.txt` and `COPY . .`, never `pip install .`.

**Running against GameHouse locally**: GameHouse is a sibling repo (`../gamehouse`), started from its own root with `DB_PATH=gamehouse.db PORT=8090 .venv/bin/python3 -m gamehouse_mcp.server` — port 8090 deliberately, since it collides with xsettlers on the default 8080. Registration between the two needs `GAMEHOUSE_URL=http://localhost:8090/mcp` and `XSETTLERS_PUBLIC_URL=http://localhost:8080/mcp` set when starting xsettlers; without them it silently no-ops by design (see `register_with_gamehouse`'s docstring).

**Two things to check rather than assume**: run `fly status --app xsettlers` before trusting anything about what is deployed — the deployed commit has lagged `main` by whole branches. And re-read `../gamehouse/docs/data_model.md` before touching `xsettlers_mcp/gamehouse.py`; that wire contract is a fast-moving sibling project, not a stable external dependency, and it has changed without warning mid-session.

**Requires a real Python (3.12, per the Dockerfile) with `sqlite3.Connection.enable_load_extension` and `mod_spatialite` installed** (`brew install spatialite-tools` on macOS). Every DB call goes through `db/connection.get_connection()`, which loads the `mod_spatialite` extension — this fails on Python builds without extension-loading support.

Config is env-driven (see `.env.example`, loaded via `python-dotenv`): `DB_PATH`, `GAME_CONFIG_PATH`, `CONFIDENCE_DECAY_PER_TURN`, `GAME_TICK_SECONDS`, `TURN_LIMIT`.

**Env vars shadow `config/game_config.yaml`, and most of that file's `game:` block is inert.** Only `max_players` (`config/loader.py`, `db/bootstrap.py`) and `score_weights` (`engine/turn.py`, `organization_reports.py`, both via `engine/scoring.py`) are actually read. `tick_seconds`, `turn_limit`, and `confidence_decay_per_turn` are each parsed into `GameSettings` and then ignored in favour of an env var — they are kept deliberately, reserved for the precedence rule (YAML as default, env as override) tracked in `docs/TODO.md`, so don't "clean them up". The trap is changing a value in the YAML and expecting it to take effect — check whether anything consumes the field first.

Deploy target is Fly.io (`fly.toml`, `Dockerfile`) — persistent volume mounted at `/data` holds the SpatiaLite `.db` file.

## Architecture

### Request flow

```
Any MCP client (Slack, curl, an LLM agent) → POST /mcp (carries player_token)
                          │
                    xsettlers_mcp/server.py  (list_tools / call_tool dispatch)
                          │
                    xsettlers_mcp/tools/*.py  (player_tools, sector_tools, navigation_tools,
                          │                    organization_tools, organization_reports)
                          │                   all gated by session.py's @player_tool
                          │
                    db/connection.py → SpatiaLite (.db file)
```

### Layering — imports point one way

```
npc/                 strategies act by calling tool functions, so they sit ABOVE the tools
  ↓
xsettlers_mcp/       server.py, tools/*.py
  ↓
engine/              turn resolution, movement, production, bearings, ship's log
  ↓
db/                  connection, schema, sectors, orgs, events
```

**Nothing in `engine/`, `db/`, `views/` or `config/` may import from `xsettlers_mcp/` or `npc/`.** There is exactly one exception, and it is a function-level import: `engine/turn.py`'s step 0 calls `npc.strategies.run_npc_decisions`, deferred to call time because a module-level import would close the loop `engine → npc → tools → engine`. Adding a second such import is how the previous tangle started — nine circular-import workarounds routing around a cycle nobody had named.

`engine/bearings.py` is why the scan vocabulary (`SCAN_RANGE`, `SCAN_BEARINGS`, `resolve_bearing`, `bearing_name`, `get_scan_range`) lives below both consumers rather than in `sector_tools.py`: the engine resolves scans and the tool layer displays them, so the compass belongs under both. It is a leaf module and should stay one.

`npc/` holds `strategies.py` (code policies), `script.py` (the YAML program runner), `programs.py` (the named-program library) and `profiles.py` (assignment). `profiles.py` is there rather than under `db/` because validating a program at assign time needs `script.validate_program` — filed under `db/` it would drag the whole NPC layer back underneath the tool layer.

`views/` is a leaf: `format.py` turns one value into the string a player reads (`"E:20, F:20"`, `"P1-01"`, `"03:47"`), `render.py` lays those strings out as a markdown table or grid. Neither imports from `engine/`, `xsettlers_mcp/` or `npc/`. A report in `xsettlers_mcp/tools/organization_reports.py` owns the queries and decides which fields go in the `display` block; it does not do its own string formatting.

`engine/scanning.py` owns everything about aiming — what a legal aim is, and both `apply_*` functions that write one. "Scanning is scanning, whoever carries the equipment" is a rule this codebase states repeatedly; it is implemented once, here, and resolved once in `engine/turn.py`'s `_resolve_scan`. Don't reintroduce a pod-specific copy.

There is **no `gateway.py`** — no central pre-flight wrapper decides who may call what, despite what `docs/mcp_server_layer_design.md` sketches. Instead every gameplay tool carries the `@player_tool` decorator (`xsettlers_mcp/tools/session.py`), which resolves `player_token` against `players`, rejects an unknown token with "Player not found" before the tool body runs, and hands the tool an authenticated `PlayerSession` (open cursor + player row) so it never manages a connection itself. Before a scenario is selected `players` is empty, so every tool naturally rejects — that's the actual gate. `xsettlers_mcp/game_select.select_scenario()` (backed by `xsettlers_mcp/auth.authenticate()`) is the one real gatekeeping call. See `tests/test_gateway.py` for the end-to-end proof.

Two tools call `PlayerSession.release()` to commit and close early before delegating to code that opens its own connection (`set_mission`→`confirm_move`, `declare_end_turn`→`end_of_turn()`); `db/connection.py` sets no busy_timeout, so a second writer fails immediately rather than waiting.

### Scenario selection & bootstrap

The MVP runs **one shared game per deployed instance** (the `games` table is a `CHECK (id = 1)` singleton). Flow:

1. `list_scenarios(player_token)` (`xsettlers_mcp/game_select.py`) discovers scenarios by globbing `config/game*.yaml` (excluding `game_config.yaml`, which holds engine settings + the player directory, not a scenario). Given a token it returns only the scenarios that player is a *participant* in; an unrecognized token gets an empty list, not the library.
2. `select_scenario(player_token, scenario_name)` authenticates twice, deliberately: once with no scenario (who are you? — resolves the token against the directory) and again with the chosen scenario file (may you play *this* game? — requires being a participant). Identity is checked first so an unrecognized token learns nothing about which scenarios exist. Then `db/bootstrap.bootstrap_game()` on first selection. Switching scenarios once a game is active is rejected.
3. `bootstrap_game()` seeds sectors, players, starting ships + pods (from the scenario's `pods_per_ship` templates), and stamps home sectors visible at confidence 100. It requires a `scenario_file` — there is no default scenario.

`engine/clock.run_clock()` starts ticking immediately at server startup regardless of whether a scenario has been picked; `engine/turn.end_of_turn()` no-ops if the `games` table is empty so no turns are silently burned pre-selection.

**Design decision for future multi-game support**: one SQLite DB file per game instance, not a shared DB with a `game_id` column threaded through every table. A future lobby would just route a player to the right DB file rather than requiring changes to `organizations`/`pods`/`events`/etc. or any query in `engine/*` or `xsettlers_mcp/tools/*`.

### Data model

Object graph: **Players → Organizations (Ships or Colonies) → Pods**, with **Sectors** as neutral, unowned grid cells. Ships and Colonies share a single `organizations` table (single-table inheritance, `org_type` discriminator). A player's presence in a sector is always derived from where their organizations are located — never stored on the sector itself.

Key fields and their split responsibilities:
- `organizations.is_mobile` vs `org_type` — `is_mobile` is a *behavioral* flag (can this org's mission be reassigned right now?); `org_type` is a *semantic* label (ship vs colony). They're intentionally decoupled: a ship mid-colonization has `org_type='ship'` but `is_mobile=0`.
- **Three org-lock states**, all keyed off `is_mobile`/`sector_id`, enforced in `set_mission` (`xsettlers_mcp/tools/organization_tools.py`): in-transit (`sector_id == -1`, locked entirely — must `cancel_move` first), colony (locked against `move` only), mid-colonization (locked entirely for the 3-turn window).
- **Organizations have a `mission`; pods have a `task`.** One word for each of two different concepts: `organizations.mission` is what the *vehicle* is doing (`idle`/`move`/`colonize`/`defend`/`attack`), `pods.task` is what the *crew* is doing (`idle`/`produce_energy`/`produce_food`/`produce_goods`/`scan`). The tool is `set_pod_task`. One word covering both is actively misleading: a player reading "mission: idle" on a ship report reasonably concludes its pods are idle too. `docs/mcp_server_layer_design.md` uses the older vocabulary and is superseded on this point by `docs/product_requirements.md` and `docs/data_model_and_storage_design.md`.
- Sectors use a sparse/lazy model — only instantiated on interaction — plus a `POINTZ` geometry column (`sectors.location`) for spatial queries. A sentinel sector `(-1,-1,-1)` represents "in transit" and is created by `db/schema.init_schema()`.
- **Scanning**: every organization has innate sensors (one sector per turn, no pod required), and pods may additionally take the `scan` task — identical rules either way. Aim is an **offset from the scanner's own sector**, not absolute coordinates, so it survives a move; `engine/bearings.SCAN_BEARINGS` maps the 12 compass names onto the 12 sectors reachable at `SCAN_RANGE` 2, and **north is −y**. Because an offset's range is fixed, out-of-range aims are rejected at set time rather than failing at end-of-turn resolution. A scan reveals only its target sector — range governs reach, not breadth.
- `player_sectors` is the fog-of-war table: sparse, confidence-scored (100 on discovery/presence; an unoccupied sector loses a flat `CONFIDENCE_DECAY_PER_TURN` points per tick — subtraction, *not* a fraction of what remains, which on an integer column never reaches 0). At confidence 0 the sector **blinks out**: the row is never deleted, but every player-facing read filters `confidence > 0`, so it leaves the map entirely rather than showing as a stale "ghost". At the default 20/turn that's five turns from last sighting to gone. The constant lives in `db/sectors.py`, not `engine/turn.py`, because the read side needs it too.
- `events` is a write-ahead log: player-action events store deltas, and `turn.snapshot` events store a **per-player ledger row** each turn (holdings, score, derived waste — written by `engine/turn.py`'s `_snapshot_holdings`). This is *not* a full-state recovery checkpoint; there is no replay mechanism. `events.resolve_at_turn` drives deferred resolution (e.g. `colonize_complete` fires 3 turns after `set_mission('colonize', ...)`).
- **There is no `models/` package.** CRUD logic lives directly in `xsettlers_mcp/tools/*.py`. Pulling it out into a real model layer is tracked in `docs/TODO.md` — create the package then, not before.

### Turn resolution (`engine/turn.py`)

`end_of_turn()` runs in a fixed order — this order is load-bearing, not incidental:

1. Reset `end_turn_declared` on all players
2. Resolve arrivals from `arrival_queue` (ships land, mission resets to idle, destination sector stamped visible)
3. Pod consumption (currently logged only, not yet deducted — no org-level resource pool exists yet) then production, then scan resolution (stationary orgs only — in-transit ships suppress scanning)
4. Colonization resolution — matured `colonize_complete` events flip `org_type` ship→colony; idempotent via the `org_type='ship'` filter (once flipped, it stops matching)
5. Mission dispatch for `defend`/`attack` (currently stubs — `_handle_defend`/`_handle_attack` are no-ops)
6. Fog-of-war decay for unoccupied sectors
7. Holdings snapshot (must run *after* all mutations, hence the explicit `conn.commit()` before it)
8. Increment turn counter; check `is_game_over()` (turn limit reached) and calculate final scores if so

The clock (`engine/clock.py`) calls `end_of_turn()` on a fixed interval (`GAME_TICK_SECONDS`); `check_consensus_acceleration()` lets all-players-declared consensus fire it early.

### NPC strategies — data first, code only when it must be

**An NPC is just a player row with `is_npc=1` plus an `npc_profiles` row**; strategies act by calling the same tool functions a human calls through MCP, so every ownership check works unmodified. `run_npc_decisions()` is step 0 of `end_of_turn()`, and completes before the turn's own transaction opens.

A strategy is **either a YAML program or a Python function**, and the split is deliberate:

- **Plans** — fixed openings whose whole sequence is decided in advance — are data: `config/npc_programs/*.yaml`, executed by `npc/script.py`. `turtle`, `burst_and_colonize` and `fan_out_consolidate` all live here. Adding one is adding a file, no code change.
- **Policies** — strategies that read the world each turn and decide from it — stay as functions in `npc/strategies.py`. `fan_out` (waits for every scout's scan, then converges the fleet on the richest find) and `frontier_map_stay_frosty` (no terminal state) need conditions, repetition and cross-org coordination, which the ship's log deliberately isn't. **Write a new strategy as a program first**; only reach for a function when it has to look at the board before choosing.

`npc/strategies.strategy_names()` is the union of both and is what `xsettlers_mcp/gamehouse.py` validates rosters against and `../xsettlers-designer`'s tournament runner plays — so a strategy crossing the data/code line never changes what callers may ask for. Don't reintroduce a bare `STRATEGIES` lookup in those callers. One of those callers is now in another repo, so `strategy_names()` is a cross-repo contract, not just an internal one.

Programs are validated at **assign** time (`validate_program()`, called by `assign_npc_profile()`), not when they run — the same reasoning behind `queue_command`'s up-front param validation, one level up: a program is authored by a person and an error must reach them synchronously, not three turns later inside a clock tick. This matters because the whole point of the format is a future NPC builder (tracked in `docs/TODO.md`).

The ship's log (`org_command_queue`, `engine/ship_log.py`) is what makes programs possible; its action whitelist is `{move, set_pod_task, colonize, aim_scan}`, each dispatching into an engine-layer `apply_*` helper (`engine/movement.py`, `engine/pod_tasking.py`, `engine/missions.py`, `engine/scanning.py`) rather than the self-connecting tool wrappers, which would deadlock inside the turn transaction. A `move` takes either absolute `dest_x/y/z` or **relative `d_x/d_y/d_z`**, resolved against the org's position at fire time — that's what makes a program portable between home sectors, and it's why the negative-coordinate guard lives at fire time for that form. `colonize` is the one action that can be *refused* rather than only succeeding or raising (a ship that can't pay when the order fires), which is why `alert.queued_command_refused` is a separate event type from `alert.queued_command_failed` — don't merge them.

### Config

**This service is a library of games, not one game** — that shapes the whole config split.

`config/game_config.yaml` holds engine-wide settings (tick interval, confidence decay, max players, turn limit, score weights) and the **player directory** — who exists on this service, one entry per person, one `player_token` each. It says nothing about who plays what, and carries no pointer to a scenario; which scenario runs is a runtime choice made through `select_scenario()`.

`config/game<N>.yaml` is a scenario: `name`/`description` (shown when choosing), `ships_per_player`, `pods_per_ship` templates, `home_colony`, `starting_fill` (what fraction of capacity every pod holds at bootstrap, 0.0–1.0, overridable per pod template — how rich a game begins is a scenario decision, not an engine constant; it defaults to 1.0 only for compatibility, and starting at capacity distorts the early economy, since a full fleet cannot accumulate and its production is pure waste), and its own **`participants`** list — each entry naming a directory player by email plus that player's `home_sector` (and optional `is_npc`). **The length of the participants list is the scenario's player count**, so a solo game (`config/game_solo.yaml`) and a five-player game differ only in YAML; no code branches on player count. `max_players` is an engine ceiling, never a floor.

`config/loader.py`'s `resolve_seats()` pairs each participant with their directory entry into a `Seat` (identity *and* starting position on one object), which is what `bootstrap_game()` iterates. Nothing downstream pairs two lists by index. A participant not in the directory raises at load time rather than silently seating fewer players.

Adding a playable scenario is just adding a `config/game<N>.yaml` — no code change, no touching `game_config.yaml` unless the scenario needs a player the directory doesn't have yet.

**Committed file, real credentials tension**: each directory entry's `player_token` is that player's actual auth credential (see above), but `game_config.yaml` is tracked in git and baked into the Docker image at build time. The committed values are intentionally obvious placeholders (`REPLACE_WITH_GENERATED_TOKEN_*`), not real secrets — this is unresolved for a real multi-player deployment (no mechanism exists to keep real tokens out of git while still letting `xsettlers_mcp/auth.py` read them at runtime). Don't commit real generated tokens into this file.

### Testing conventions

`tests/conftest.py` provides an autouse `fresh_db` fixture (fresh SpatiaLite file per test via `monkeypatch.setenv("DB_PATH", ...)`) plus seed helpers (`seed_player`, `seed_sector`, `seed_ship`, `seed_pod`, `seed_player_sector`). It also auto-seeds an active `games` row so most tests don't need to think about scenario selection — tests that specifically exercise the pre-selection state (`test_game_select.py`) must `DELETE FROM games` to opt back out.

Per `docs/TODO.md`'s TDD rule: **no new function without a corresponding test entry** — `test_navigation.py` and `test_organization.py` are the templates to follow.

**A test file follows a subject, not a module.** `test_scanning.py` covers aiming, legality and end-of-turn resolution together, because an org's sensors and a scan pod are supposed to behave identically and only a shared file proves it. `test_economy.py` covers the production pass and pooled-resource rules together, because production is prorated by what the pool can pay for. Put a new test where its subject lives; don't add it to whichever file happens to import the function.

`test_registry.py` is small but collects 67 of the suite's 382 tests — three parametrized sweeps over all 22 tools. That is one property asserted per tool, not redundancy, and it is what catches schema/signature drift. Leave it alone.

## Writing comments and docs

Comments state what the code does and what constraints hold **now**. They do
not narrate what the code used to do, when it changed, how many copies there
used to be, or what was considered and rejected — that is what
`docs/dev_history.md` is for, and most of it doesn't need recording at all.

Keep a rule even when it reads like history ("north is −y", "this step order is
load-bearing", "no busy_timeout, so a second writer fails immediately"). Drop
the date stamp, the "used to", and the changelog entry.
