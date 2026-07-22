# XSettlers — Known TODOs

Consolidated from the Project Shell canvas. Organized by area.

## DB & Engine

* [ ] `engine/turn.py` — consumption deduction deferred until org-level resource pool is defined
* [ ] `engine/turn.py` — `_handle_defend` and `_handle_attack` are stubs; implement combat system later
* [x] `engine/turn.py` — colonization event resolution pass implemented (2026-07-18): gated on `events.resolve_at_turn <= current_turn` for `event_type='colonize_complete'`, joined against `organizations` and idempotent via the `org_type='ship'` filter (flips to `'colony'` and naturally stops matching). Writes `ship.colonized` directly via SQL rather than `db.events.record_event` — importing it here would create a circular import (`db/events.py` imports `get_current_turn` from this module).
* [ ] `engine/turn.py` — pod execution pass: scan pods must check `org.mission == 'move'`; if ship is in transit, suppress scan silently. Produce pods and consumption run unconditionally.
* [x] `db/schema.py` — `resolve_at_turn INTEGER` added to `events` (2026-07-18), plus an index on `(event_type, resolve_at_turn)`.
* [x] `db/events.py` — `record_event()` takes an optional `resolve_at_turn` (2026-07-18). Payload for `colonize_complete` uses `org_id`/`sector_id` (the sector being colonized) rather than `origin_sector_id` as originally sketched here — `origin_sector_id` is a movement/rubber-band concept and doesn't apply to colonization.
* [ ] `tests/test_bootstrap.py` — stub exists; fill in: idempotency, sector/player/org/pod creation, player_sectors stamping, geometry column
* [ ] **Sector-seeding gap** — `config/game_config.yaml`'s comment claims bootstrap generates the full 256-sector (16×16) grid described in Game Instance: MVP, but `bootstrap_game()` only inserts the 3 sectors literally listed in `cfg.sectors` (origin + 2 home sectors). The rest of the grid does not exist until someone adds procedural generation. Found during the 2026-07-18 canvas migration; transcribed as-authored, not yet fixed.

## MCP Tools

* [ ] `navigation_tools.py` — `confirm_move` uses Euclidean distance; refine jump mechanics (e.g. warp lanes, hex grid)
* [ ] `organization_tools.py` — `set_mission("move", ...)` should route to `confirm_move` flow, not set directly
* [ ] `organization_tools.py` — `set_mission` and `set_pod_mission` now implemented; remove from open list once tests pass
* [x] `organization_tools.py` — `set_mission('colonize')` implemented (2026-07-18): flips `is_mobile=0` immediately, does not touch `org_type`, schedules `colonize_complete` for `current_turn + 3`.
* [ ] `organization_tools.py` — `set_pod_mission(mission='scan')`: if the parent ship is currently in transit (`mission == 'move'`), allow the assignment but return a warning that the scan pod will be suppressed until arrival
* [x] **Org-lock rule implemented (2026-07-18)** — `is_mobile` now uniformly represents "can this org's mission be reassigned right now," covering all three lock states: colony (permanent, blocks `move` only — `defend`/`attack`/`idle` remain assignable), mid-colonization (temporary, blocks *all* reassignment — "committed... for the duration"), and in-transit (temporary, blocks *all* reassignment via a `sector_id == -1` check in `set_mission`, independent of `is_mobile`). `confirm_move` now sets `is_mobile=0` on parking at the sentinel; both the arrival-processing path in `engine/turn.py` and `cancel_move` restore `is_mobile=1`. Previously a ship mid-transit still showed `is_mobile=1` and `set_mission` had no guard against reassigning it, which could silently desync `organizations.mission` from a still-live `arrival_queue` row.
* [ ] Consider a DB-level `CHECK` constraint enforcing `org_type != 'colony' OR is_mobile = 0` on `organizations` — currently only enforced by application code (bootstrap + colonize resolution), not the schema itself.

## Infrastructure

* [x] **Local dev environment (2026-07-22)** — this sandbox's Python 3.9 lacked `sqlite3.Connection.enable_load_extension` entirely (separate from the project's Python 3.12 target), so the DB-dependent test suite had never actually been run for real. Fixed by installing Homebrew + Python 3.12 + spatialite-tools locally; full suite now passes (41/41). Surfaced and fixed two real bugs this uncovered: an off-by-one in `engine/turn.py`'s colonize-resolution turn check, and a double-seed bug in `tests/test_mcp.py`.
* [x] **Docker (2026-07-22)** — local Docker via Colima (not Docker Desktop, to avoid its GUI/privileged-helper install). Building/running the real `Dockerfile` surfaced three bugs, now fixed: an unused `pysqlite3` dependency that broke the build on the compiler-less `python:3.12-slim` base (removed); a missing `libsqlite3-mod-spatialite` apt package that would've crashed the container at runtime (added); and the package rename below.
* [x] **`mcp/` renamed to `xsettlers_mcp/` (2026-07-22)** — the local package collided with the third-party `mcp` SDK package it imports (`from mcp.server import Server`, `from mcp import types`); whichever `mcp` Python resolved first won process-wide, and the local package always won, so `mcp/server.py` circularly self-imported instead of reaching the SDK. This broke the server as an entrypoint both locally and in Docker — never caught before because no test imports `mcp.server` directly and nobody had run the server as an entrypoint until this work.
* [x] **Transport: stdio → streamable HTTP (2026-07-22)** — the server previously ran over `stdio_server()` (JSON-RPC over the process's own stdin/stdout), which only works for a client that spawns the server locally. `fly.toml` was configured as an HTTP service (`internal_port = 8080`, TLS/HTTP handlers) with nothing in the code that ever listened on a socket — deployed as-is, Fly's proxy would have had no live endpoint to route Slackbot's calls to. Rewired `xsettlers_mcp/server.py` to serve `mcp.server.streamable_http_manager.StreamableHTTPSessionManager` (stateless mode) over Starlette + uvicorn, mounted at `POST /mcp`, plus a `GET /health` check. Verified with a real `initialize` JSON-RPC call, both locally and inside the rebuilt Docker image. `fly.toml` now has a matching `[[services.http_checks]]` against `/health`.
* [ ] **Still needed before a real Fly.io deploy**: `flyctl` isn't installed on the dev machine; `fly auth login` (browser OAuth, needs a human); the `xsettlers_data` volume needs to be created once (`fly volumes create xsettlers_data --size 1`) before the first `fly deploy`.
* [ ] Add a CI workflow (`.github/workflows/`) — deferred for now, planned before/around the first push upstream
* [ ] **Hosting: Fly.io.** Single confirmed deployment target — no other option under consideration.
* [x] **Perimeter auth stopgap (2026-07-22)** — `xsettlers_mcp/server.py`'s `/mcp` route now requires `Authorization: Bearer <MCP_SHARED_SECRET>` (a single static shared secret, not per-player keys, checked with `hmac.compare_digest`; fails closed if the env var is unset). Deployed via `fly secrets set MCP_SHARED_SECRET=...`. This closes the "anonymous internet stranger can call /mcp" gap opened by the streamable-HTTP switch — `/health` stays open for Fly's health checks, which don't send the header.
* [ ] **Still open: per-player identity is not verified.** The shared secret only gates *reaching* the endpoint. `xsettlers_mcp/auth.py` still trusts whatever `slack_user_id` is passed in a request's arguments at face value — anyone who holds the shared secret (e.g. Slackbot itself, or the secret if it leaks) can still act as any player in the roster by passing their `slack_user_id`. Real hardening needs Slack to cryptographically vouch for which user is making the call (e.g. Slack's own request signing, or per-user tokens), not just a perimeter secret. Deliberately deferred for now — explicit choice to avoid per-user key management until Slackbot integration is actually being built.
* [ ] Multi-game support (concurrent games, real lobby/matchmaking) — `xsettlers_mcp/game_select.py` and the `games` table are no longer stubs (see "Gateway / Player Onboarding" below for what's actually built); the future multi-game/lobby direction is now documented there rather than here.
* [ ] Gateway pre-flight — see "Gateway" section below; currently **none of this exists in code**, `xsettlers_mcp/server.py` bypasses it entirely

## Design (Data Model canvas)

* [ ] Review sector schema for ownership field creep
* [ ] Evaluate denormalized active player ID vector on Sector (future)
* [ ] Evaluate Neo4j Community Edition (future)

## Models refactor

* [ ] `models/` directory is stubbed; CRUD logic currently lives in tool files — refactor once POC is stable

## TDD rule

* **No new function without a corresponding test entry.** `test_navigation.py` and `test_organization.py` are the templates to follow.

## Open Design Questions (need a decision, not just code)

* [ ] **Movement API naming** — Product Requirements + MCP Tools Scaffold (the code that actually got built) use a two-step `preview_move` → `confirm_move` (+ `cancel_move`) flow. MCP Server Layer Design's doc still describes a single-call `queue_move` tool instead. Never reconciled. `preview_move`/`confirm_move`/`cancel_move` is what's implemented in `xsettlers_mcp/tools/navigation_tools.py`; `docs/mcp_server_layer_design.md` needs a decision on whether to update its wording to match or whether `queue_move` was meant as a separate higher-level wrapper.
* [ ] **Home colony at start** — Game Instance: MVP canvas states each player begins with a home colony pre-placed at turn 0; the DB & Engine Scaffold's `config/game0.yaml` sets `home_colony: false`. Unresolved as of the 2026-07-18 migration — pick one and align both.

## Gateway / Player Onboarding

**Status as of 2026-07-19: built, but deliberately not as originally spec'd.** The MCP Server Layer Design canvas's original design ("gateway.py wraps every tool call with an auth → select-game → bootstrap pre-flight") was revised during implementation — see below for why. This is a real, player-facing flow now, not stubs: a player authenticates against the roster, lists available scenarios, and picks one, which lazily bootstraps that scenario on first selection.

* [x] `xsettlers_mcp/auth.py` — `authenticate(slack_user_id)` implemented. Checks the static roster in `config/game_config.yaml` directly; does **not** depend on the DB or on a game being bootstrapped, since the roster is config-driven and exists before any scenario is chosen.
* [x] `xsettlers_mcp/game_select.py` — implemented: `list_scenarios()` discovers scenarios by scanning `config/game*.yaml` (each scenario file now declares its own `name`/`description` — see `config/game0.yaml`); `get_active_game()` reads the new `games` table; `select_scenario(slack_user_id, scenario_name)` validates against the roster + scenario list, then lazily bootstraps via `bootstrap_game()` if no game is active yet. Rejects switching to a different scenario once one is already active (single shared game per deployed instance for the MVP).
* [x] `config/loader.py` — `load_config()` gained a `scenario_override` param so `select_scenario` can force a specific scenario file instead of the one named in `game_config.yaml`.
* [x] `db/schema.py` / `db/bootstrap.py` — new single-row `games` table (`scenario_name`, `scenario_file`, `selected_by`, `bootstrapped_at`) is the real record of which scenario is active; `bootstrap_game()` now accepts `scenario_file`/`scenario_name`/`selected_by` and writes this row.
* [x] `xsettlers_mcp/server.py` — `list_scenarios`/`select_scenario` added as real MCP tools; `main()` no longer auto-bootstraps at startup (`init_schema()` only — tables, no seeding).
* [x] `engine/turn.py` — `end_of_turn()` now no-ops if the `games` table is empty, so `engine/clock.py`'s clock (which starts ticking immediately at server startup) doesn't silently burn through turns before any scenario has been selected.
* **No separate `xsettlers_mcp/gateway.py` module, and no per-call gateway wrapper around every tool dispatch.** This was a deliberate simplification during implementation: every gameplay tool already does its own `SELECT id FROM players WHERE slack_user_id=?` ownership check internally, and `players` stays empty until `select_scenario` triggers bootstrap — so before a scenario is picked, every tool already rejects with "Player not found" on its own. Re-checking auth centrally on every single call would have been redundant. `select_scenario` (gated by `authenticate`) is the one real gate. See `tests/test_gateway.py` for the end-to-end proof of this behavior (no module named `gateway.py` backs it, deliberately).
* [x] `tests/test_auth.py`, `tests/test_game_select.py`, `tests/test_gateway.py` — written 2026-07-19. Exercise the roster check, scenario listing/selection/bootstrap/idempotency, and the end-to-end "blocked before selection, works after" gate. Verified for real on 2026-07-22 once a working Python 3.12 + spatialite environment existed locally — all passing.
* [ ] **`xsettlers_mcp/auth.py` roster check is still config-file-based, not hardened** — same "trust Slack identity for now" caveat as originally spec'd, unchanged.
* [ ] Multi-game support (concurrent games, not just switching scenarios) is still out of scope — `select_scenario` explicitly rejects switching once a game is active rather than supporting multiple simultaneous games.

### Future: multi-game lobby (anticipated 2026-07-19, not built)

The user's longer-term vision: a master lobby where a player authenticates once, sees several available games, picks one, waits for real players to fill in, and NPCs backfill remaining slots. Explicitly not being built now, but the architectural fork was decided so bootstrap doesn't need painful rework later:

* **Decision: one SQLite database per game instance**, not a shared database with a `game_id` column threaded through every table. Rationale: this is already what's built today — `games` is a singleton (`CHECK (id = 1)`), and `select_scenario` already rejects switching scenarios once one is active, i.e. one DB already holds at most one game. A future lobby becomes a thin router pointing a player at the right DB file; it does **not** require touching `organizations`, `pods`, `events`, `arrival_queue`, `player_sectors`, or any query in `engine/turn.py`, `engine/production.py`, or `xsettlers_mcp/tools/*.py`. The rejected alternative (shared DB + `game_id` everywhere) would have meant touching nearly every file built this session — too invasive for "anticipate, don't build."
* [x] `players.is_npc INTEGER DEFAULT 0` added (2026-07-19) — removes a future migration need when NPC fill-in is actually built. Backward-compatible; no existing `INSERT INTO players` call site needed to change.
* [x] `bootstrap_game()` gained a `roster_override` param (2026-07-19) — escape hatch for a future lobby to hand bootstrap a dynamically-assembled roster (real players + NPCs) instead of only ever reading `config/game_config.yaml`'s static list. Not called by anything yet — `xsettlers_mcp/game_select.py`'s `select_scenario()` is unchanged and still always uses the config file's roster.
* **Explicitly deferred, not touched**: matchmaking logic, NPC behavior/AI, per-game DB file provisioning/routing, and `db/connection.py`'s `DB_PATH` becoming per-game (today it's one global env var read fresh per call — correct for one game per deployment; a lobby will need a "resolve DB path for this game" mechanism layered on top later, not a change to `get_connection()` itself).
* **Known gap, flagged not fixed**: `config/game0.yaml`'s `home_sector_by_player` is a fixed 2-element, position-indexed list matching `config/game_config.yaml`'s 2 hardcoded players. A variable-size lobby roster will `IndexError` on `sc.home_sector_by_player[idx]` in `db/bootstrap.py` once roster size diverges from scenario file size. Reworking scenario files to support variable rosters is out of scope for now.
* **Two more gaps found while planning this, fine to leave alone but worth knowing about**:
  - `events.game_id INTEGER` exists in the schema but is never populated by any `INSERT INTO events` anywhere in the codebase — vestigial under the DB-per-game model. Left as-is (dropping columns is out of scope here).
  - `xsettlers_mcp/auth.py`'s `authenticate()` always reads `config/game_config.yaml`'s static roster directly, independent of whatever roster `bootstrap_game()` was actually seeded with. Once something actually calls `roster_override` with a dynamically-assembled roster, newly-seeded players won't be recognized by `authenticate()` until it's updated too — `auth.py` and bootstrap's roster source need to be reconciled together when the lobby is actually built, not before.
