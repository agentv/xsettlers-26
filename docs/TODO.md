# XSettlers — Known TODOs

Consolidated from the Project Shell canvas. Organized by area.

## DB & Engine

* [ ] `engine/turn.py` — consumption deduction deferred until org-level resource pool is defined
* [ ] `engine/turn.py` — `_handle_defend` and `_handle_attack` are stubs; implement combat system later
* [ ] `engine/turn.py` — add colonization event resolution pass: query events table for `colonize_complete` where `resolve_at_turn <= current_turn`, flip `org_type` to `'colony'` and `is_mobile` to `0`, write `ship.colonized` event; run this pass **before** mission/task dispatch so a completing colony can affect production in the same tick
* [ ] `engine/turn.py` — pod execution pass: scan pods must check `org.mission == 'move'`; if ship is in transit, suppress scan silently. Produce pods and consumption run unconditionally.
* [ ] `db/schema.py` — confirm `events` table DDL includes `resolve_at_turn INTEGER` and `payload TEXT` columns
* [ ] `db/events.py` — add `colonize_complete` as a recognized event type with fields: `org_id`, `resolve_at_turn`, `origin_sector_id`
* [ ] `tests/test_bootstrap.py` — stub exists; fill in: idempotency, sector/player/org/pod creation, player_sectors stamping, geometry column
* [ ] **Sector-seeding gap** — `config/game_config.yaml`'s comment claims bootstrap generates the full 256-sector (16×16) grid described in Game Instance: MVP, but `bootstrap_game()` only inserts the 3 sectors literally listed in `cfg.sectors` (origin + 2 home sectors). The rest of the grid does not exist until someone adds procedural generation. Found during the 2026-07-18 canvas migration; transcribed as-authored, not yet fixed.

## MCP Tools

* [ ] `navigation_tools.py` — `confirm_move` uses Euclidean distance; refine jump mechanics (e.g. warp lanes, hex grid)
* [ ] `organization_tools.py` — `set_mission("move", ...)` should route to `confirm_move` flow, not set directly
* [ ] `organization_tools.py` — `set_mission` and `set_pod_mission` now implemented; remove from open list once tests pass
* [ ] `organization_tools.py` — `set_mission('colonize')`: immediately flip `is_mobile = 0` and write a `colonize_complete` event to the queue for `current_turn + 3`; do **not** flip `org_type` here — that happens at resolution
* [ ] `organization_tools.py` — `set_pod_mission(mission='scan')`: if the parent ship is currently in transit (`mission == 'move'`), allow the assignment but return a warning that the scan pod will be suppressed until arrival

## Infrastructure

* [ ] `fly.toml` — stub needs volume configuration before deploying (Dockerfile now added at repo root)
* [ ] Add a CI workflow (`.github/workflows/`) — deferred for now, planned before/around the first push upstream
* [ ] **Hosting: Fly.io.** Single confirmed deployment target — no other option under consideration.
* [ ] Authentication / trust boundary — `mcp/auth.py` trusts `slack_user_id` at face value; harden for production (blocked on the gateway build-out below)
* [ ] Multi-game support — `mcp/game_select.py` is a stub returning one game; future extension adds a `games` table and named config files (blocked on the gateway build-out below)
* [ ] Gateway pre-flight — see "Gateway" section below; currently **none of this exists in code**, `mcp/server.py` bypasses it entirely

## Design (Data Model canvas)

* [ ] Review sector schema for ownership field creep
* [ ] Evaluate denormalized active player ID vector on Sector (future)
* [ ] Evaluate Neo4j Community Edition (future)

## Models refactor

* [ ] `models/` directory is stubbed; CRUD logic currently lives in tool files — refactor once POC is stable

## TDD rule

* **No new function without a corresponding test entry.** `test_navigation.py` and `test_organization.py` are the templates to follow.

## Open Design Questions (need a decision, not just code)

* [ ] **Movement API naming** — Product Requirements + MCP Tools Scaffold (the code that actually got built) use a two-step `preview_move` → `confirm_move` (+ `cancel_move`) flow. MCP Server Layer Design's doc still describes a single-call `queue_move` tool instead. Never reconciled. `preview_move`/`confirm_move`/`cancel_move` is what's implemented in `mcp/tools/navigation_tools.py`; `docs/mcp_server_layer_design.md` needs a decision on whether to update its wording to match or whether `queue_move` was meant as a separate higher-level wrapper.
* [ ] **Home colony at start** — Game Instance: MVP canvas states each player begins with a home colony pre-placed at turn 0; the DB & Engine Scaffold's `config/game0.yaml` sets `home_colony: false`. Unresolved as of the 2026-07-18 migration — pick one and align both.

## Gateway

**Status as of 2026-07-18 canvas migration: not built.** No canvas contained actual code for this layer — only prose describing the intended flow (MCP Server Layer Design's "Gateway Layer" section). `mcp/server.py` currently calls `init_schema()`/`bootstrap_game()` directly in `main()`, bypassing any auth/game-select pre-flight. The items below are the original design intent, still accurate as a spec, just not yet implemented:

* [ ] `mcp/gateway.py` — orchestrates auth → game select → bootstrap pre-flight on every tool call; apply as decorator or pre-flight wrapper in `server.py` tool handlers
* [ ] `mcp/server.py` — wrap every tool handler with gateway pre-flight: `gateway.authenticate()` → `gateway.select_game()` → `gateway.ensure_bootstrapped()`
* [ ] `mcp/auth.py` — `authenticate(slack_user_id)` → Player; trust Slack identity for now, harden later
* [ ] `mcp/game_select.py` — `select_game(player)` → game_id stub; one game only
* [ ] `tests/test_gateway.py` — cover: valid auth pass-through, unknown player rejection, bootstrap pre-flight idempotency
* [ ] `tests/test_auth.py` — cover: known player resolves correctly, unknown slack_user_id returns error
