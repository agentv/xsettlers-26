# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

XSettlers is a multiplayer space strategy game, originally designed to be played entirely through Slack but now client-agnostic (see below). This repo is the Python MCP (Model Context Protocol) server: any MCP-speaking client calls MCP tools with a player's token attached, tools query/mutate a SpatiaLite database, and a background clock resolves turns on a fixed interval. There is no separate web/API layer — `xsettlers_mcp/server.py` *is* the server, serving MCP's **streamable HTTP** transport (Starlette + uvicorn, `POST /mcp`, `GET /health`) so a network-hosted deployment (Fly.io) can actually be reached remotely. (Switched from `stdio_server()` on 2026-07-22 — stdio only works for a client that spawns the process locally, which doesn't fit a cloud deployment.)

**Identity is client-agnostic, not Slack-specific.** Every tool's first argument is `player_token` (renamed from `slack_user_id` on 2026-07-22) — an opaque per-player secret compared with `hmac.compare_digest` in `xsettlers_mcp/auth.py`, not a Slack-platform credential. Slack, curl, another LLM agent, or anything else that knows a valid token authenticates identically; nothing in the auth path is Slack-specific anymore. Separately, `MCP_SHARED_SECRET` (an `Authorization: Bearer` header, checked in `xsettlers_mcp/server.py`) gates *reaching* `/mcp` at all — the two are independent layers: the shared secret says "you're allowed to talk to this server," `player_token` says "you're acting as this specific player." Neither is full authentication in a hardened sense — see `docs/TODO.md`'s "Still open: per-player identity is not verified" note.

**The local package is `xsettlers_mcp/`, not `mcp/`.** It was renamed from `mcp/` (2026-07-22) because that name collided with the third-party `mcp` SDK package (`pip install mcp`) that `xsettlers_mcp/server.py` itself imports (`from mcp.server import Server`, `from mcp import types`) — whichever `mcp` Python resolves first wins process-wide, and the local package always won, causing `xsettlers_mcp/server.py` to circularly self-import instead of reaching the SDK. Never rename it back to `mcp/`.

Documentation lives in `docs/` and is the source of truth for design (migrated from Slack canvases on 2026-07-18). Read `docs/TODO.md` first when picking up work — it tracks not just outstanding TODOs but *reconciled discrepancies* between the design docs and what's actually implemented (e.g. the "gateway" design vs. what got built instead).

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
```

**Requires a real Python (3.12, per the Dockerfile) with `sqlite3.Connection.enable_load_extension` and `mod_spatialite` installed** (`brew install spatialite-tools` on macOS). Every DB call goes through `db/connection.get_connection()`, which loads the `mod_spatialite` extension — this will fail on Python builds without extension-loading support (e.g. some sandboxed/minimal Python 3.9 installs).

Config is env-driven (see `.env.example`, loaded via `python-dotenv`): `DB_PATH`, `GAME_CONFIG_PATH`, `CONFIDENCE_DECAY`, `GAME_TICK_SECONDS`.

Deploy target is Fly.io (`fly.toml`, `Dockerfile`) — persistent volume mounted at `/data` holds the SpatiaLite `.db` file.

## Architecture

### Request flow

```
Any MCP client (Slack, curl, an LLM agent) → POST /mcp (carries player_token)
                          │
                    xsettlers_mcp/server.py  (list_tools / call_tool dispatch)
                          │
                    xsettlers_mcp/tools/*.py  (player_tools, sector_tools, navigation_tools, organization_tools)
                          │
                    db/connection.py → SpatiaLite (.db file)
```

There is **no separate `gateway.py`** despite what `docs/mcp_server_layer_design.md` originally sketched. Instead, every gameplay tool does its own `SELECT id FROM players WHERE player_token=?` ownership check inline. Before a scenario is selected, `players` is empty, so every tool naturally rejects with "Player not found" — that's the actual gate, no central pre-flight wrapper needed. `xsettlers_mcp/game_select.select_scenario()` (backed by `xsettlers_mcp/auth.authenticate()`) is the one real gatekeeping call. See `tests/test_gateway.py` for the end-to-end proof of this behavior.

### Scenario selection & bootstrap

The MVP runs **one shared game per deployed instance** (the `games` table is a `CHECK (id = 1)` singleton). Flow:

1. `list_scenarios()` (`xsettlers_mcp/game_select.py`) discovers scenarios by globbing `config/game*.yaml` (excluding `game_config.yaml`, which holds shared game settings + the fixed player roster, not a scenario).
2. `select_scenario(player_token, scenario_name)` authenticates against the roster (`xsettlers_mcp/auth.py` — trusts the token's roster match at face value, no deeper identity verification), then calls `db/bootstrap.bootstrap_game()` on first selection for that scenario. Switching scenarios once a game is active is rejected.
3. `bootstrap_game()` seeds sectors, players (from `game_config.yaml`'s roster, unless `roster_override` is passed — an unused escape hatch for a future dynamic lobby), starting ships + pods (from the scenario file's `pods_per_ship` templates), and stamps home sectors visible at confidence 100.

`engine/clock.run_clock()` starts ticking immediately at server startup regardless of whether a scenario has been picked; `engine/turn.end_of_turn()` no-ops if the `games` table is empty so no turns are silently burned pre-selection.

**Design decision for future multi-game support**: one SQLite DB file per game instance, not a shared DB with a `game_id` column threaded through every table. A future lobby would just route a player to the right DB file rather than requiring changes to `organizations`/`pods`/`events`/etc. or any query in `engine/*` or `xsettlers_mcp/tools/*`.

### Data model

Object graph: **Players → Organizations (Ships or Colonies) → Pods**, with **Sectors** as neutral, unowned grid cells. Ships and Colonies share a single `organizations` table (single-table inheritance, `org_type` discriminator). A player's presence in a sector is always derived from where their organizations are located — never stored on the sector itself.

Key fields and their split responsibilities:
- `organizations.is_mobile` vs `org_type` — `is_mobile` is a *behavioral* flag (can this org's mission be reassigned right now?); `org_type` is a *semantic* label (ship vs colony). They're intentionally decoupled: a ship mid-colonization has `org_type='ship'` but `is_mobile=0`.
- **Three org-lock states**, all keyed off `is_mobile`/`sector_id`, enforced in `set_mission` (`xsettlers_mcp/tools/organization_tools.py`): in-transit (`sector_id == -1`, locked entirely — must `cancel_move` first), colony (locked against `move` only), mid-colonization (locked entirely for the 3-turn window).
- **Mission vs task terminology**: pods use `mission` (`idle`/`produce_energy`/`produce_food`/`produce_goods`/`scan`), *not* the older `task`/`set_pod_task` vocabulary from `docs/mcp_server_layer_design.md` — that doc is retained for its hosting/gateway content but is superseded on this point by `docs/product_requirements.md` and `docs/data_model_and_storage_design.md`.
- Sectors use a sparse/lazy model — only instantiated on interaction — plus a `POINTZ` geometry column (`sectors.location`) for spatial queries. A sentinel sector `(-1,-1,-1)` represents "in transit" and is created by `db/schema.init_schema()`.
- `player_sectors` is the fog-of-war table: sparse, confidence-scored (100 on discovery/presence, decays ~10%/tick via `CONFIDENCE_DECAY` when unoccupied, never deleted at confidence 0 — becomes a stale "ghost memory" row).
- `events` is a write-ahead log with a **hybrid payload strategy**: player-action events store deltas, engine `turn.snapshot` events store full state (for replay/disaster recovery). `events.resolve_at_turn` drives deferred resolution (e.g. `colonize_complete` fires 3 turns after `set_mission('colonize', ...)`).
- `models/` is currently an empty stub package — CRUD logic lives directly in `xsettlers_mcp/tools/*.py` for now; a refactor to pull it out is tracked in `docs/TODO.md` but not yet done. Don't expect model classes to exist there.

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

### Config

`config/game_config.yaml` holds shared game settings (tick interval, confidence decay, max players, turn limit, feature flags, score weights) and the **fixed player roster** — there is no runtime player-join path; changing the roster means bootstrapping a new database. It points to a `starting_configuration_file` (e.g. `config/game0.yaml`), which is loaded separately via `load_starting_configuration()` and declares its own `name`/`description` (shown to players choosing a scenario), home sectors per player, ship count, and pod loadout templates. Adding a new playable scenario is just adding a new `config/game<N>.yaml` — no code change, no touching `game_config.yaml`.

**Committed file, real credentials tension**: each roster entry's `player_token` is that player's actual auth credential (see above), but `game_config.yaml` is tracked in git and baked into the Docker image at build time. The committed values are intentionally obvious placeholders (`REPLACE_WITH_GENERATED_TOKEN_*`), not real secrets — this hasn't been resolved for a real multi-player deployment yet (no mechanism exists to keep real tokens out of git while still letting `xsettlers_mcp/auth.py` read them at runtime). Don't commit real generated tokens into this file.

### Testing conventions

`tests/conftest.py` provides an autouse `fresh_db` fixture (fresh SpatiaLite file per test via `monkeypatch.setenv("DB_PATH", ...)`) plus seed helpers (`seed_player`, `seed_sector`, `seed_ship`, `seed_pod`, `seed_player_sector`). It also auto-seeds an active `games` row so most tests don't need to think about scenario selection — tests that specifically exercise the pre-selection state (`test_game_select.py`) must `DELETE FROM games` to opt back out.

Per `docs/TODO.md`'s TDD rule: **no new function without a corresponding test entry** — `test_navigation.py` and `test_organization.py` are the templates to follow.
