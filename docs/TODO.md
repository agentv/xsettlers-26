# XSettlers — Known TODOs

Only things that still need to be done. Completed work and resolved
decisions have been moved to `docs/dev_history.md` (split out 2026-07-30) —
check there for the "why" behind anything that looks like it should already
exist.

## DB & Engine

* [ ] `engine/turn.py` — `_handle_defend` and `_handle_attack` are stubs; implement combat system later.
* [ ] `engine/turn.py` — **rival detection is still unbuilt** (`# TODO: emit pod.scanned event; detect rivals` in scan resolution). No `alert.rival_detected` event is ever emitted and the schema has no sighting-history table, so rival positions can only be read live from `organizations`. `show_sector_neighborhood` works around this by reporting rivals *only* in sectors at confidence 100 (ones the player occupies) — anywhere else would leak current intel onto a decayed cell. Building sighting storage would let rivals appear on stale cells honestly, stamped with the turn last seen. See `docs/ui_and_rendering_design.md`'s Cell vocabulary.
* [ ] `engine/turn.py` — scan pods still pay their food cost while in transit; only the *reveal* is currently suppressed (the `org["sector_id"] != -1` check gates just the reveal/range-check branch, not the recipe-drain above it). Should suppress the whole scan mission, cost included, while in transit — not just the reveal.

## MCP Tools

* [ ] **Player-settable organization names** — no tool exists to rename an org post-bootstrap (`name` is written once at bootstrap and never touched again by any tool in `organization_tools.py`). Needs a new tool, likely `rename_organization(player_token, org_id, name)` — ownership-gated like everything else in that module, with some not-yet-decided sanity bound on length/characters. First instance of a broader question worth keeping in mind: `mission` is currently the only org characteristic a player can edit post-bootstrap (via `set_mission`/`set_pod_mission`) — if more editable characteristics get added later, worth revisiting whether they each get their own bespoke tool or start warranting a more general "edit organization" entry point.
* [ ] `organization_tools.py` — `set_pod_mission(mission='scan')`: if the parent ship is currently in transit (`mission == 'move'`), allow the assignment but return a warning that the scan pod will be suppressed until arrival.
* [ ] Consider a DB-level `CHECK` constraint enforcing `org_type != 'colony' OR is_mobile = 0` on `organizations` — currently only enforced by application code (bootstrap + colonize resolution), not the schema itself.

## Config

* [ ] **`config/game_config.yaml`'s `game:` block is mostly dead, shadowed by env vars.** Only `max_players` and `score_weights` are consumed. `tick_seconds` (vs `GAME_TICK_SECONDS`), `turn_limit` (vs `TURN_LIMIT`), and `confidence_decay_per_turn` (vs `CONFIDENCE_DECAY_PER_TURN`) are each parsed into `GameSettings` by `config/loader.py` and then never read; `dimensions` and `feature_flags` are read by nothing anywhere. This is a live trap — editing a value in the YAML looks like it should work and silently does nothing. Fix is to pick a precedence rule (proposed: YAML supplies the default, env overrides it) and apply it to all of them at once, rather than wiring up one field and leaving the rest inconsistent. Raised 2026-07-30 while changing the fog decay model.

## Infrastructure

* [ ] Add a CI workflow (`.github/workflows/`) — deferred for now, planned before/around the first push upstream.
* [ ] **Known gap: `config/game_config.yaml`'s roster is committed to git but `player_token` is now a real credential.** Committed values are placeholders (`REPLACE_WITH_GENERATED_TOKEN_*`), not real secrets — don't paste real generated tokens into this file as-is. No mechanism yet lets `xsettlers_mcp/auth.py` read real per-player secrets from somewhere outside git (analogous to how `MCP_SHARED_SECRET` is handled via `fly secrets` + a gitignored local `.env`) while keeping the "roster is one YAML file" design. Deferred until real (non-`@example.com`) players are actually onboarded.
* [ ] **Per-player identity is not independently verified.** The shared secret (`MCP_SHARED_SECRET`) only gates *reaching* the endpoint; `player_token` only proves "caller knows this player's credential," not "caller is cryptographically the platform account this credential was issued to." Since there's no platform (Slack, etc.) vouching for the caller anymore, this is arguably a cleaner model than before, not a worse one — but it means the whole system rests on `player_token` values staying secret. Real hardening beyond that would need per-client signing/OAuth, out of scope until there's an actual multi-client integration to harden against. Accepted risk, not a bug — kept here so it isn't rediscovered as a surprise.

## Dev/Admin Tooling (explicitly NOT part of the player-facing MCP interface)

* [ ] **Clock pause/resume for experimentation** — need a way to freeze `engine/clock.py`'s background tick so DB state holds still while poking at it (today's workaround — kill the server, call `end_of_turn()` manually in a loop, restart — works but is a manual dance, not a real mechanism). Explicitly scoped as **developer/admin-only, never a `server.py` tool registration, never reachable over `/mcp`** — this must not become something a player token can invoke. Not designed yet — open questions: an env var the clock checks each tick vs. a signal/file-flag vs. a small out-of-band control (e.g. a second, unauthenticated-but-localhost-only endpoint, or just a CLI flag/script) that never touches `xsettlers_mcp/server.py`'s tool dispatch at all. Whatever the shape, it needs to be impossible to trigger through the same channel a player's `player_token` reaches.

## Design (Data Model canvas)

* [ ] **Ship classes** — not built, not documented anywhere yet. Today every `org_type='ship'` row is a single undifferentiated archetype — `org_type` is a flat ship/colony binary with no stat variation within "ship," and the closest adjacent concept, `pod_type` (`crew`/`cargo`/`defense`/`attack`/`ship`/`sensor`), is about individual pod roles, not ship-level archetypes — deferred, not instantiated. The idea as raised:
  - **Ranger class** — mobile, fast, longer range/faster movement, traded off against thinner "skin" (durability/cargo, exact tradeoff not yet specified)
  - **Legacy class** — general-purpose mix of resources, eventually weapons; the default/baseline class (closest to what every ship already is today, undifferentiated)
  - **Colony class** — heavily loaded with resources, purpose-built for a one-way trip to found a colony — distinct from the existing `org_type='colony'` flip (what a ship *becomes* after colonizing); this would be about a ship's *loadout/build* before that transition even starts

  Not designed: what fields this needs (a `ship_class` column on `organizations`? a stat-modifier table keyed by class?), how it interacts with the existing `pods_per_ship`/pod-loadout templates in `config/game*.yaml`, or how it relates to the deferred `pod_type` roster above (a ship class could plausibly be *defined* by its pod-type mix rather than being a separate field). Backlog item only — no decision made yet.
* [ ] Review sector schema for ownership field creep.
* [ ] Evaluate denormalized active player ID vector on Sector (future).
* [ ] Evaluate Neo4j Community Edition (future).

## Models refactor

* [ ] `models/` directory is stubbed; CRUD logic currently lives in tool files — refactor once POC is stable.

## TDD rule (standing policy — not a task, keep applying it)

* **No new function without a corresponding test entry.** `test_navigation.py` and `test_organization.py` are the templates to follow.
* A new computed field on an *existing* API response gets a new assertion appended to that response's existing test, not a new test function — reserve new test functions for new functions, new branches, or new error paths (see `docs/dev_history.md`'s 2026-07-30 test-suite-consolidation entry for why).

## Gateway / Player Onboarding

* [ ] `xsettlers_mcp/auth.py` roster check is still config-file-based, not hardened — same "trust identity for now" caveat as originally spec'd, unchanged.
* [ ] Multi-game support (concurrent games, not just switching scenarios) is still out of scope — `select_scenario` explicitly rejects switching once a game is active rather than supporting multiple simultaneous games.

### Multi-game lobby — still not built

The long-term vision (see `docs/dev_history.md` for the architectural groundwork already in place: one-DB-per-game decision, `players.is_npc`, `bootstrap_game()`'s `roster_override`): a master lobby where a player authenticates once, sees several available games, picks one, waits for real players to fill in, and NPCs backfill remaining slots. Still open:

* [ ] **Matchmaking logic, and NPC fill-in at roster time specifically.** NPC decision-making itself now exists (`engine/npc.py` — see dev history's 2026-07-30 NPC strategy profiles entry), but nothing yet assigns an NPC to backfill an open lobby slot automatically; `assign_npc_profile()` is still a manual call, not wired into any join/roster flow.
* [ ] Per-game DB file provisioning/routing, and `db/connection.py`'s `DB_PATH` becoming per-game — today it's one global env var read fresh per call (correct for one game per deployment); a lobby will need a "resolve DB path for this game" mechanism layered on top later, not a change to `get_connection()` itself.
* [x] ~~`home_sector_by_player` is a fixed 2-element position-indexed list; a variable-size roster will `IndexError`.~~ **Resolved 2026-07-30.** Scenarios now declare a `participants` list (directory email + that player's `home_sector`), resolved into `Seat` objects by `config/loader.py`'s `resolve_seats()`. Player count is a property of the scenario, so variable roster sizes work with no code change — `config/game_solo.yaml` is the 1-player proof. Positional pairing between the roster and the scenario is gone entirely.
* [ ] `events.game_id INTEGER` exists in the schema but is never populated by any `INSERT INTO events` anywhere in the codebase — vestigial under the DB-per-game model. Fine to leave (dropping columns is out of scope), just noting it's dead.
* [ ] `roster_override` is still the one path that can desync identity. `authenticate()` now reads the same scenario `participants` list that `bootstrap_game()` seeds from, so the config path is reconciled — but a caller passing `roster_override` assembles seats that exist in no YAML file, and `authenticate()` will not recognize those players. A real lobby needs the directory itself to become dynamic (or `authenticate()` to fall back to the `players` table once a game is bootstrapped). Narrower than it was, not closed.

### NPC strategy profiles — remaining work

Core system is built (see `docs/dev_history.md`'s 2026-07-30 entry: schema, registry, execution hook, `assign_npc_profile()`). Still open:

* [ ] Only one registered strategy (`fan_out_consolidate`) exists — no `set_mission`/`set_pod_mission`-driven strategies yet; the registry supports them, nothing uses them.
* [ ] No roster-time NPC assignment — still a manual `assign_npc_profile()` call, not wired into any lobby/bootstrap flow (see "Multi-game lobby" above).
