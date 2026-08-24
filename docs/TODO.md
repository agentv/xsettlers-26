# XSettlers — Known TODOs

Only things that still need doing. Settled decisions and hard-won findings are
in `docs/dev_history.md`; everything else is in the code.

## DB & Engine

* [ ] `engine/turn.py` — `_handle_defend` and `_handle_attack` are stubs; implement combat later. `set_mission` refuses `defend`/`attack` outright (`UNIMPLEMENTED_MISSIONS`, "Weapons are inoperable"), so the stubs are unreachable today — deliberately kept, along with step 5's dispatch, as the seam combat lands in. Both missions stay in `VALID_ORG_MISSIONS`: dropped from it, the rejection would enumerate the survivors and read as "this game has no combat", which is false. Building combat is deleting the refusal set and filling the stubs.
* [ ] `engine/turn.py` — the `pod.scanned`/`org.scanned` events are still unbuilt, and no NPC strategy scans toward an opponent, so nothing in the library produces contact on its own. Rival detection itself shipped 2026-08-18 (`db/sightings.py`); see `docs/dev_history.md`.
* [ ] `engine/turn.py` — scan pods pay their food cost while in transit; only the *reveal* is suppressed (the `org["sector_id"] != -1` check gates the reveal/range branch, not the recipe drain above it). The whole scan should be suppressed in transit, cost included.

## MCP Tools

* [ ] **Move-tasking response template — canonical.** This is the report a player wants back after ordering a ship to move, and it should be what `confirm_move` (and `set_mission(mission='move')`, which delegates to it) returns. Today they return a bare dict of raw fields and the client improvises the rest. Four parts, in order:

  1. **What was ordered, previewed then committed** — `turns_needed` and `arrival_turn` per ship, then the confirmation. Preview before commit is the intended flow (see the `set_mission` tool description); showing both makes the cost of the order visible before its effect.
  2. **The whole fleet, not just the ships that moved** — `id`, `name`, `where`, `mission`, one row per org. In-transit rows must name the destination (`in transit → (10,12,0)`), because `show_civilization_status`'s `status` string is deliberately terse and a player who just issued a move needs to see it reflected. Showing unmoved orgs is the point: the question after tasking two ships is "what do I still have available," not "did those two ships accept the order."
  3. **What it cost in aggregate** — how many orgs are now off the board, what fraction of total holdings went with them, what remains at home.
  4. **One forward-looking consequence** — the thing that will matter on arrival but is not visible in the table. Example: two destinations 2 sectors out against a scan range of 1, so the two arrival footprints overlap neither each other nor home — two isolated islands of vision. This part is judgment, not a computed field.

  Implementation shape: parts 1–3 are mechanical and belong in a `display` block on the move response, following the same hints convention as `show_organization` and `render_map` (see `docs/ui_and_rendering_design.md`) so any client renders them identically. Part 4 is not mechanizable and stays with whatever agent is narrating.

* [ ] **Is energy production meant to be suppressed in transit?** A ship with `mission='move'` reports `E:0, F:20, G:10` — pod tasking unchanged (still 2 energy pods), but energy output stops while food and goods continue. Since food and goods each consume energy to run (`engine/production.py`'s recipes) and energy is the one input that needs none, a long voyage burns down its carried energy with no way to replenish it, and can arrive unable to restart its own economy. That is either a nice hidden cost of exploration or an accident of how transit suppression was written. Decide which before treating it as a rule.

* [ ] **A general "edit organization" entry point may eventually be warranted.** A player can edit three things about an org — name, mission, pod task — each via its own bespoke tool. If more editable characteristics arrive, revisit whether they each get a tool or whether one general editor is the better shape. Not urgent; noted so the third bespoke tool isn't followed by a fourth without anyone asking.
* [ ] `organization_tools.py` — `set_pod_task(task='scan')`: if the parent ship is in transit, allow the assignment but return a warning that the scan will be suppressed (though still charged) until arrival.
* [ ] Consider a DB-level `CHECK` constraint enforcing `org_type != 'colony' OR is_mobile = 0` on `organizations` — currently enforced only by application code (bootstrap + colonize resolution), not the schema.
* [ ] **Tick countdown is computed in two places** — `scripts/status.py`'s `_clock_status` and `views/format.py`'s `tick_countdown` both parse `next_tick_at` and `divmod` it into minutes/seconds (~5 lines each). Deliberately **not** merged: they differ in what they can know. The CLI runs as its own process and can health-check the server to distinguish "paused" from "counting down"; the in-process one cannot, so a `None` there already means "not running". Both files comment on the relationship. Worth revisiting only if a third caller appears — at which point the shared piece is the formatting, not the liveness judgement.

## Config

* [ ] **`config/game_config.yaml`'s `game:` block is mostly dead, shadowed by env vars.** Only `max_players` and `score_weights` are consumed. `tick_seconds` (vs `GAME_TICK_SECONDS`), `turn_limit` (vs `TURN_LIMIT`), and `confidence_decay_per_turn` (vs `CONFIDENCE_DECAY_PER_TURN`) are each parsed into `GameSettings` by `config/loader.py` and then never read. This is a live trap — editing a value in the YAML looks like it should work and silently does nothing. The fix is to pick a precedence rule (proposed: YAML supplies the default, env overrides it) and apply it to all three at once rather than wiring up one and leaving the rest inconsistent. The three are reserved, not dead; re-add feature flags only alongside code that reads them.

## Infrastructure

* [ ] Add a CI workflow (`.github/workflows/`) — planned before/around the first push upstream.
* [ ] **The roster in `config/game_config.yaml` is committed to git, but `player_token` is a real credential.** Committed values are placeholders (`REPLACE_WITH_GENERATED_TOKEN_*`) — don't paste real generated tokens into this file. No mechanism yet lets `xsettlers_mcp/auth.py` read real per-player secrets from outside git (analogous to `fly secrets` + a gitignored `.env`) while keeping the "roster is one YAML file" design. Deferred until real (non-`@example.com`) players are onboarded.
* [ ] **`/mcp` is open, and `player_token` is the only access control.** Anyone who knows the URL can call any tool, and `player_token` only proves "caller knows this player's credential". Compounding it: the roster's tokens are placeholders in a **public** repository that also documents this server's URL, and there is no rate limiting. Accepted knowingly (the game holds nothing of value and is unadvertised) — but this is the single largest gap, and it stops being acceptable the moment either fact changes. The real fix is OAuth on the endpoint plus per-player tokens held outside git; that is the same piece of work as the roster item above.

## Models refactor

* [ ] CRUD logic lives in tool files (`xsettlers_mcp/tools/*.py`) — pull it into
  a model layer once the POC is stable. There is no `models/` package today;
  create one when there is something to put in it, not before.

## Gateway / Player Onboarding

* [ ] `xsettlers_mcp/auth.py`'s roster check is config-file-based, not hardened
  — the "trust identity for now" caveat still stands.
* [ ] Multi-game support (concurrent games, not just switching scenarios) is out
  of scope — `select_scenario` rejects switching once a game is active.
* [ ] Per-game DB file provisioning/routing, and `db/connection.py`'s `DB_PATH`
  becoming per-game. GameHouse's handoff assumes "one shared game per deployed
  instance" exactly as xsettlers does, so it doesn't change this.
* [ ] `events.game_id INTEGER` exists in the schema but is never populated —
  vestigial under the DB-per-game model. Fine to leave; noting it's dead.
* [ ] A GameHouse-driven session doesn't touch `config/game_config.yaml`'s
  directory or `authenticate()` at all — it is a fully separate identity path.
  Whether to eventually retire the static-roster path or keep both is undecided.

### GameHouse handoff — still open

`xsettlers_mcp/gamehouse.py` is xsettlers' side of `../gamehouse`'s wire
contract. Named in GameHouse's own docs as required game-side surface that
doesn't exist yet:

* [ ] A run-state query GameHouse can poll, so it can tell a returning Person
  whether their game is still alive before offering to reconnect. Note the
  results hand-back below already gives GameHouse the *end* of a game
  (`game_journal.status` flips to `completed`), so what is still missing is
  liveness *during* play, not completion.
* [ ] Multi-scenario support on both sides — `scenario_key` is accepted but not
  branched on; today it is always `None` in real traffic.

### GameHouse `join_lobby` dedupe bug — lives in `../gamehouse`, not here

The same real Person joining one lobby twice (client-side retry) fills both of
Diaspora's seats with one identical `player_id`. `close_lobby`
(`gamehouse_mcp/lobby.py`) builds `players` straight from `lobby_member` rows
with no dedup, so it pushes `start_session` with two identical `player_id`s. On
this side, `start_session` derives `email = f"gamehouse-{gh_id}@handoff"` per
seat with no disambiguation, so the two seats collide on `players.email`'s
`UNIQUE` constraint and the push crashes outright — `db/bootstrap.py` does a
plain `INSERT` with no upsert and nothing in the call chain handles the
exception. The fix belongs in GameHouse (reject a second `join_lobby` from a
`person_id` already in that lobby, or dedupe before building the roster).
Deferred until the multi-game work below is settled.

## NPC strategies

* [ ] **A document cannot size a selection relative to the fleet.** `ships`
  takes `all`, `idle`, or a fixed `slice`/`stride`/`offset` — there is no way
  to say "a quarter of whatever fleet I have". A fractional selector
  (`{fraction: 0.25}`) is the obvious fix, deferred since nothing shipped
  needs it.
* [ ] **No minimum-fleet guard.** A document written for 8 ships just selects
  fewer on a short fleet rather than declining to act. If a precondition is
  ever wanted it belongs in the document format, not rediscovered per
  strategy.

### NPC builder — what the document format still needs

The point of strategies-as-data is a later phase where a strategy is *authored*
rather than written — and eventually authored by a *player* and traded. Two
properties the current model already has that make that possible: a document
carries no expressions, so accepting one grants no capability; and every
`decide` source is fog-limited in `npc/decide.py`, so an authored strategy
cannot see what its owner has not scanned. What exists is the format and its
validator (`npc/strategy.validate_strategy`, run at assign time by
`assign_npc_profile()` so an authoring tool gets errors synchronously).
Outstanding before a builder is worth writing:

* [ ] **No way to list or describe the vocabulary programmatically.** A builder
  needs the legal actions, phases, selector keys, gates, sources, rank fields
  and picks as data it can render into a form. They are constants
  (`ACTION_NAMES`, `TRIGGER_PHASES`, `ORDER_KEYS`, `DECIDE_KEYS`, and
  `npc/decide.py`'s `GATES`/`SOURCES`/`RANK_FIELDS`/`PICKS`) plus prose in the
  module docstrings.
* [ ] **`assign_npc_profile()` is dev/test-only**, so an authored document has
  no path into a live game except through the GameHouse handoff's roster. A
  builder needs one — and it is the piece that turns "a player can write a
  strategy" into "a player can play one".
* [ ] Fleet-relative selectors and a fleet-minimum guard (see above).
