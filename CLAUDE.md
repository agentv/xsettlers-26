# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

XSettlers is a multiplayer space strategy game, playable from any MCP-speaking client. This repo is the Python MCP server: a client calls MCP tools with a player's token attached, tools query/mutate a SQLite database, and a background clock resolves turns on a fixed interval. **There is no separate web/API layer** — `xsettlers_mcp/server.py` *is* the server, on MCP's streamable HTTP transport (Starlette + uvicorn, `POST /mcp`, `GET /health`) so a network-hosted deployment can be reached remotely. There is no stdio path; stdio only works for a client that spawns the process locally.

**Identity is client-agnostic.** Every tool's first argument is `player_token` — an opaque per-player secret compared with `hmac.compare_digest` in `xsettlers_mcp/auth.py`, not a platform credential. Slack, curl, another LLM agent, or anything else holding a valid token authenticates identically.

**`/mcp` has no perimeter auth — it is open to the internet**; access control rests entirely on `player_token`. See the SECURITY POSTURE comment in `xsettlers_mcp/server.py` for why, and what is thin about it.

**The local package is `xsettlers_mcp/`, never `mcp/`.** That name collides with the third-party `mcp` SDK package `server.py` itself imports. `docs/dev_history.md` has the failure mode and why no test catches it.

Documentation lives in `docs/` and describes what is **built**. `docs/TODO.md` is open work only — read it to find out what is outstanding. `docs/dev_history.md` holds settled decisions, play-testing findings and recovery pointers; it is a **lookup, not orientation** — open it when a pointer sends you there for the reasoning behind a specific rule, not to get your bearings. Reading it front to back is how a session pays for every decision ever made to answer one question.

**Game design does not live here.** Numbers, scenarios, strategies, what a view should show, `design_direction.md` and `scenarios_and_strategies.md` are all in `../xsettlers-designer`. The split is by question: *how does it work* here, *what should it be* there. Don't read them for an engineering question, and don't re-derive a decision they already settled.

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

# Score a cleanup. Both must improve, or the commit is not the cleanup it claims.
scripts/shrink.py [ref]             # files / code / prose / doc lines
scripts/shrink.py --context [ref]   # tokens a fresh session pays before any work
```

The local `xsettlers.db` is scratch — safe to `rm` and restart clean.

Requires **Python 3.12** (per the Dockerfile) and nothing else — storage is the standard-library `sqlite3` module, no extension to load. Deploy target is Fly.io (`fly.toml`, `Dockerfile`), persistent volume at `/data`.

Config is env-driven (`.env.example`, via `python-dotenv`): `DB_PATH`, `GAME_CONFIG_PATH`, `CONFIDENCE_DECAY_PER_TURN`, `GAME_TICK_SECONDS`, `TURN_LIMIT`. **These env vars shadow `config/game_config.yaml`, whose `game:` block is largely inert** — the shadowed fields are kept deliberately for a precedence rule tracked in `docs/TODO.md`, so don't "clean them up", and check that something reads a field before changing its value there.

**Two sibling repos, neither imported here, both able to break from a change here.**

- `../xsettlers-designer` runs tournaments and matchup simulation. It installs this repo editable (that is what `pyproject.toml` is for) and drives `engine.turn.end_of_turn()` in-process — so it is a **real consumer of `engine/`, `db/` and `npc/`**, and `pyproject.toml`'s package list has to keep matching this repo's top-level packages. The Fly build ignores `pyproject.toml` entirely.
- `../gamehouse` exchanges traffic three ways, all in `xsettlers_mcp/gamehouse.py`, which documents the wire contract and the results envelope. To run the pair locally: start GameHouse from its own root on port 8090 (`DB_PATH=gamehouse.db PORT=8090 .venv/bin/python3 -m gamehouse_mcp.server`), and start xsettlers with `GAMEHOUSE_URL=http://localhost:8090/mcp` and `XSETTLERS_PUBLIC_URL=http://localhost:8080/mcp` — without both, registration silently no-ops by design.

**Two things to check rather than assume**: run `fly status --app xsettlers` before trusting anything about what is deployed — the deployed commit has lagged `main` by whole branches. And re-read `../gamehouse/docs/data_model.md` before touching `xsettlers_mcp/gamehouse.py`; that wire contract is a fast-moving sibling project, not a stable external dependency, and it has changed without warning mid-session.

## Architecture

### Request flow

```
Any MCP client (Slack, curl, an LLM agent) → POST /mcp (carries player_token)
                          │
                    xsettlers_mcp/server.py  (list_tools / call_tool dispatch)
                          │
                    xsettlers_mcp/tools/*.py  (player_tools, sector_tools, navigation_tools,
                          │                    organization_tools, organization_reports,
                          │                    task_force_tools)
                          │                   all gated by session.py's @player_tool
                          │
                    db/connection.py → SQLite (.db file)
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

**Nothing in `engine/`, `db/`, `views/` or `config/` may import from `xsettlers_mcp/` or `npc/`.** There is exactly one exception, and it is a function-level import: `engine/turn.py`'s step 0 calls `npc.strategies.run_npc_decisions`, deferred to call time because a module-level import would close the loop `engine → npc → tools → engine`. Don't add a second such import.

`views/` is a leaf and must stay one. A report (`xsettlers_mcp/tools/organization_reports.py`) owns its queries and decides which fields go in the `display` block; it does not format its own strings — that is `views/format.py`, laid out by `views/render.py`.

Graphics follow the same rule and add a second seam. `views/svg_renderer.py` splits into `layout_org_card(data) -> (marks, dims)`, which computes geometry as plain dicts with no markup in them, and `emit_svg(marks, dims) -> str`, which draws marks and knows nothing about cards. Keep that split: a rasterizer or an HTML card reuses one half without the other, and text escaping stays in one place. `response_format='html_svg'` on `call_tool()` returns the JSON plus a server-rendered SVG for any tool listed in `server.py`'s `SVG_RENDERERS`, and falls back to the markdown table for the rest — **the server draws; no client ever executes JavaScript**, which is what keeps the graphics client-agnostic.

There is **no `gateway.py`**. Every gameplay tool carries the `@player_tool` decorator (`xsettlers_mcp/tools/session.py`), which authenticates `player_token` and hands the tool an open `PlayerSession` so it never manages a connection itself; a tool cannot forget the check, because the raw token never reaches its body. `players` is empty until a scenario is selected, so every tool naturally rejects before bootstrap — that, not a wrapper, is the gate. `xsettlers_mcp/game_select.select_scenario()` is the one real gatekeeping call; `tests/test_gateway.py` is the end-to-end proof.

Two tools call `PlayerSession.release()` to commit and close early before delegating to code that opens its own connection (`set_mission`→`confirm_move`, `declare_end_turn`→`end_of_turn()`); `db/connection.py` sets no busy_timeout, so a second writer fails immediately rather than waiting.

### Scenario selection & bootstrap

The MVP runs **one shared game per deployed instance** (the `games` table is a `CHECK (id = 1)` singleton). `list_scenarios()` finds scenarios by globbing `config/game*.yaml`, excluding `game_config.yaml`; `select_scenario()` authenticates twice on purpose — identity first, then participation — so an unrecognized token learns nothing about which scenarios exist, and then calls `db/bootstrap.bootstrap_game()`. There is no default scenario, and switching once a game is active is rejected. All of it is in `xsettlers_mcp/game_select.py`.

`engine/clock.run_clock()` ticks from server startup whether or not a scenario has been picked; `end_of_turn()` no-ops on an empty `games` table, so no turns are silently burned pre-selection.

**Design decision for future multi-game support**: one SQLite DB file per game instance, not a shared DB with a `game_id` column threaded through every table. A future lobby routes a player to the right DB file, requiring no change to any table or to any query in `engine/*` or `xsettlers_mcp/tools/*`.

### Data model

Object graph: **Players → Organizations (Ships or Colonies) → Pods**, with **Sectors** as neutral, unowned grid cells. Ships and Colonies share a single `organizations` table (single-table inheritance, `org_type` discriminator). A player's presence in a sector is always derived from where their organizations are — never stored on the sector itself.

The rules below bite from anywhere. Each carries its full reasoning at the point of use cited — read that before changing one.

- **`is_mobile` is behavioral, `org_type` is semantic, and they are deliberately decoupled** — a ship mid-colonization is `org_type='ship'` with `is_mobile=0`. The three org-lock states keyed off them are enumerated and enforced in `set_mission` (`xsettlers_mcp/tools/organization_tools.py`).
- **Organizations have a `mission`; pods have a `task`.** `mission` is what the vehicle is doing (`idle`/`move`/`colonize`/`defend`/`attack`), `task` is what the crew is doing (`idle`/`produce_*`/`scan`). Don't collapse them to one word — a player reading "mission: idle" would conclude the pods are idle too.
- **Sectors are sparse** — instantiated only on interaction, by `reveal_sector()` (`db/sectors.py`), which is also where richness is rolled. The sentinel sector `(-1,-1,-1)` means "in transit", so any query over positions must expect `sector_id = -1`.
- **No tool exposes `map_hotspots`.** A player learns a region exists by revealing into it, and that is what lets a scenario keep its map secret. Don't add a read path.
- **Every player-facing read filters `confidence > 0`.** `player_sectors` is the fog-of-war table; at confidence 0 a sector blinks out of the map entirely rather than showing as a stale ghost. The row is never deleted, so the filter is the only thing enforcing it (`db/sectors.py`).
- **An aim is an offset from the scanner's own sector**, not absolute coordinates, so it survives a move (`engine/bearings.py`). Aiming is implemented once in `engine/scanning.py` and resolved once in `engine/turn.py`'s `_resolve_scan`, for an org's own sensors and a scan pod alike.
- **`events` is a write-ahead log**, and `events.resolve_at_turn` is how anything deferred fires (`colonize_complete`, three turns after `set_mission('colonize', ...)`). Its snapshots are a ledger, not a recovery checkpoint — there is no replay.
- **There is no `models/` package.** CRUD lives in `xsettlers_mcp/tools/*.py`; pulling it into a model layer waits on the POC being stable (`docs/dev_history.md`). Do it then, not before.

### Turn resolution (`engine/turn.py`)

`end_of_turn()` runs in a fixed order — this order is load-bearing, not incidental:

1. Reset `end_turn_declared` on all players
2. Resolve arrivals from `arrival_queue` (ships land, mission resets to idle, destination sector stamped visible)
3. Org upkeep, then per-pod consumption and production, then scan resolution (an in-transit ship still pays its scan cost; only the reveal is suppressed). Costs are drawn from the org's pooled stock across all its pods (`engine/org_resources.py`) and output is prorated to the fraction of input actually available, not gated all-or-nothing
4. Colonization resolution — matured `colonize_complete` events flip `org_type` ship→colony; idempotent via the `org_type='ship'` filter (once flipped, it stops matching)
5. Mission dispatch for `defend`/`attack` (currently stubs — `_handle_defend`/`_handle_attack` are no-ops)
6. Fog-of-war decay for unoccupied sectors
7. Holdings snapshot (must run *after* all mutations, hence the explicit `conn.commit()` before it)
8. Increment turn counter; check `is_game_over()` (turn limit reached) and calculate final scores if so

The clock (`engine/clock.py`) calls `end_of_turn()` on a fixed interval (`GAME_TICK_SECONDS`); `check_consensus_acceleration()` lets all-players-declared consensus fire it early.

### NPC strategies — data first, code only when it must be

**An NPC is just a player row with `is_npc=1` plus an `npc_profiles` row**; strategies act by calling the same tool functions a human calls through MCP, so every ownership check works unmodified. `run_npc_decisions()` is step 0 of `end_of_turn()` and completes before the turn's own transaction opens.

**Every strategy is a document in `config/npc_strategies/*.yaml`. There is no Python-function strategy and no registry of them** — adding a strategy is adding a file. `npc/strategy.py` walks the document; `npc/decide.py` holds the four registries (gates, sources, rank fields, picks) a document may name. Both explain their own semantics.

Three rules that reach outside those modules:

- **Resist adding expressions to the vocabulary.** It grows by adding names to `npc/decide.py`'s registries. Staying inert data is what makes a strategy safe to accept from someone else, and what keeps fog of war structural — every source requires `confidence > 0`, so no document can name a sector its owner hasn't seen.
- **Conditions live in the interpreter, never in the ship's log** — `org_command_queue` stays one-shot and unconditional.
- **`npc/library.strategy_names()` is a cross-repo contract**: `xsettlers_mcp/gamehouse.py` validates rosters against it and `../xsettlers-designer`'s tournament runner plays it.

The ship's log (`org_command_queue`, `engine/ship_log.py`) carries scheduled orders. Its action whitelist is `{move, set_pod_task, colonize, aim_scan}`, each dispatching into an engine-layer `apply_*` helper rather than the self-connecting tool wrapper — **the wrapper would deadlock inside the turn transaction.**

### Config

**This service is a library of games, not one game** — that shapes the whole config split.

`config/game_config.yaml` holds engine-wide settings and the **player directory**: who exists on this service, one `player_token` each. It says nothing about who plays what, and carries no pointer to a scenario. `config/game<N>.yaml` *is* a scenario — pod templates, `starting_fill`, an optional secret `map` block, and its own **`participants`** list naming directory players by email with each one's `home_sector`. Both files document their own fields.

- **The length of the participants list is the scenario's player count.** A solo game and a five-player game differ only in YAML; no code branches on player count. `max_players` is an engine ceiling, never a floor.
- **Adding a playable scenario is adding a `config/game<N>.yaml`** — no code change, and no touching `game_config.yaml` unless the scenario needs a player the directory lacks.
- `config/loader.py`'s `resolve_seats()` pairs each participant with their directory entry into one `Seat`. Nothing downstream pairs two lists by index.
- **Don't commit real player tokens.** A directory entry's `player_token` is that player's actual auth credential, but this file is tracked in git and baked into the Docker image at build time. The committed values are deliberate placeholders (`REPLACE_WITH_GENERATED_TOKEN_*`); keeping real ones out of git while `xsettlers_mcp/auth.py` still reads them at runtime is unresolved.

### Testing conventions

`tests/conftest.py` provides an autouse `fresh_db` fixture (a fresh DB file per test via `monkeypatch.setenv("DB_PATH", ...)`) plus seed helpers. It auto-seeds an active `games` row, so a test exercising the pre-selection state (`test_game_select.py`) must `DELETE FROM games` to opt back out.

**New behavior needs a test; relocated behavior does not.** A function extracted from existing call sites without changing behavior is a relocation — no new test, no new test file, and its proof is that the callers' existing tests still pass unchanged. Only a new branch or error path introduced during the extraction earns an assertion, appended to the file that owns the subject.

**A test file follows a subject, not a module.** `test_scanning.py` covers aiming, legality and end-of-turn resolution together, because an org's sensors and a scan pod are supposed to behave identically and only a shared file proves it. Put a new test where its subject lives, not in whichever file happens to import the function.

`test_registry.py` is small but collects 88 of the suite's 544 tests — three parametrized sweeps over all 29 tools. That is one property per tool, not redundancy, and it is what catches schema/signature drift. Leave it alone.

## Writing comments and docs

Comments state what the code does and what constraints hold **now**. They do
not narrate what the code used to do, when it changed, how many copies there
used to be, or what was considered and rejected — that is what
`docs/dev_history.md` is for, and most of it doesn't need recording at all.

Keep a rule even when it reads like history ("north is −y", "this step order is
load-bearing", "no busy_timeout, so a second writer fails immediately"). Drop
the date stamp, the "used to", and the changelog entry.
