# XSettlers — Known TODOs

Open work **this phase intends to do**. An item that stops being something we
mean to build does not linger here with a "someday" caveat — it moves to
`docs/dev_history.md` as a settled decision. Combat, multi-game routing, a
`models/` package and an NPC strategy builder all left this file that way; look
for them there before proposing them as new.

## DB & Engine

* [ ] `engine/turn.py` — the `pod.scanned`/`org.scanned` events are still unbuilt, and no NPC strategy scans toward an opponent, so nothing in the library produces contact on its own. Rival detection itself shipped 2026-08-18 (`db/sightings.py`); see `docs/dev_history.md`.

* [ ] **Resource transfer between organizations.** Two organizations standing in the same sector may hand resources to one another. Settled shape:

  * **The giving org initiates.** There is no request, no accept, no handshake — a transfer is a push, and the receiver's consent is implied by co-location. A "request resources" verb is a different feature and is not this one.
  * **Same sector, and it must be a real sector.** Both orgs' `sector_id` must match *and* must not be `-1`: the sentinel is a parking slot, not a place, so two ships in transit are never co-located no matter what the column says. This is the one check most likely to be written wrong.
  * Resources are stored per pod and pooled per org, so the giving side draws from the pool the way a recipe does (`engine/org_resources.py`) and the receiving side has to land it somewhere — deciding *which* pod absorbs an incoming resource, and what happens when the receiver has no room, is the substance of the work.

  Open, to settle while building: whether a transfer resolves immediately or at end of turn; whether it crosses player ownership (allied hand-off) or is confined to one player's own fleet; whether `transfer` joins the ship's log's action whitelist (`{move, set_pod_task, colonize, aim_scan}`, `engine/ship_log.py`), which would require an engine-layer `apply_*` helper rather than the self-connecting tool wrapper.

* [ ] **Is energy production meant to be suppressed in transit?** A ship with `mission='move'` reports `E:0, F:20, G:10` — pod tasking unchanged (still 2 energy pods), but energy output stops while food and goods continue. Since food and goods each consume energy to run (`engine/production.py`'s recipes) and energy is the one input that needs none, a long voyage burns down its carried energy with no way to replenish it, and can arrive unable to restart its own economy. That is either a hidden cost of exploration or an accident of how transit suppression was written. The parallel case — a scan pod paying its food in transit — was ruled deliberate (see `docs/dev_history.md`); this one is still undecided.

## MCP Tools

* [ ] **Move-tasking response template — canonical.** This is the report a player wants back after ordering a ship to move, and it should be what `confirm_move` (and `set_mission(mission='move')`, which delegates to it) returns. Today they return a bare dict of raw fields and the client improvises the rest. Four parts, in order:

  1. **What was ordered, previewed then committed** — `turns_needed` and `arrival_turn` per ship, then the confirmation. Preview before commit is the intended flow (see the `set_mission` tool description); showing both makes the cost of the order visible before its effect.
  2. **The whole fleet, not just the ships that moved** — `id`, `name`, `where`, `mission`, one row per org. In-transit rows must name the destination (`in transit → (10,12,0)`), because `show_civilization_status`'s `status` string is deliberately terse and a player who just issued a move needs to see it reflected. Showing unmoved orgs is the point: the question after tasking two ships is "what do I still have available," not "did those two ships accept the order."
  3. **What it cost in aggregate** — how many orgs are now off the board, what fraction of total holdings went with them, what remains at home.
  4. **One forward-looking consequence** — the thing that will matter on arrival but is not visible in the table. Example: two destinations 2 sectors out against a scan range of 1, so the two arrival footprints overlap neither each other nor home — two isolated islands of vision. This part is judgment, not a computed field.

  Implementation shape: parts 1–3 are mechanical and belong in a `display` block on the move response, following the same hints convention as `show_organization` and `render_map` (see `docs/ui_and_rendering_design.md`) so any client renders them identically. Part 4 is not mechanizable and stays with whatever agent is narrating.

## Config

* [ ] **`config/game_config.yaml`'s `game:` block is mostly dead, shadowed by env vars.** Only `max_players` and `score_weights` are consumed. `tick_seconds` (vs `GAME_TICK_SECONDS`), `turn_limit` (vs `TURN_LIMIT`), and `confidence_decay_per_turn` (vs `CONFIDENCE_DECAY_PER_TURN`) are each parsed into `GameSettings` by `config/loader.py` and then never read. This is a live trap — editing a value in the YAML looks like it should work and silently does nothing. The fix is to pick a precedence rule (proposed: YAML supplies the default, env overrides it) and apply it to all three at once rather than wiring up one and leaving the rest inconsistent. The three are reserved, not dead; re-add feature flags only alongside code that reads them.

## Infrastructure

* [ ] Add a CI workflow (`.github/workflows/`) — planned before/around the first push upstream.

* [ ] **`/mcp` is open, and `player_token` is the only access control** — one piece of work with two halves, neither started. Anyone who knows the URL can call any tool, and `player_token` only proves "caller knows this player's credential". Compounding it: the roster in `config/game_config.yaml` is committed to a **public** repository that also documents this server's URL, so the committed tokens must stay placeholders (`REPLACE_WITH_GENERATED_TOKEN_*`) — and there is no mechanism yet letting `xsettlers_mcp/auth.py` read real per-player secrets from outside git (analogous to `fly secrets` + a gitignored `.env`) while keeping the "roster is one YAML file" design. There is also no rate limiting.

  Accepted knowingly for now — the game holds nothing of value and is unadvertised — and it stops being acceptable the moment either fact changes. The fix is OAuth on the endpoint plus per-player secrets held outside git. **First question to settle: whether the OAuth half is xsettlers' job at all**, or belongs to `../gamehouse`, which already owns Person-level identity and would be the natural place for it. Decide that before building either half; the secrets-outside-git half is ours regardless.

## GameHouse handoff

`xsettlers_mcp/gamehouse.py` is xsettlers' side of `../gamehouse`'s wire
contract. Re-read `../gamehouse/docs/data_model.md` before touching it — that
contract moves without warning.

* [ ] **A run-state query GameHouse can poll**, so it can tell a returning Person whether their game is still alive before offering to reconnect. Named in GameHouse's own docs as required game-side surface that doesn't exist yet. The results hand-back already gives GameHouse the *end* of a game (`game_journal.status` flips to `completed`), so what is missing is liveness *during* play, not completion.

* [ ] **`join_lobby` dedupe crashes `start_session` on this side.** The bug is GameHouse's, the crash is ours. The same real Person joining one lobby twice (client-side retry) fills both of Diaspora's seats with one identical `player_id`: `close_lobby` (`gamehouse_mcp/lobby.py`) builds `players` straight from `lobby_member` rows with no dedup. Here, `start_session` derives `email = f"gamehouse-{gh_id}@handoff"` per seat with no disambiguation, so the two seats collide on `players.email`'s `UNIQUE` constraint and the push crashes outright — `db/bootstrap.py` does a plain `INSERT` with no upsert and nothing in the call chain handles the exception. The real fix belongs in GameHouse (reject a second `join_lobby` from a `person_id` already in that lobby); ours is to fail a handoff legibly instead of raising out of the bootstrap.
