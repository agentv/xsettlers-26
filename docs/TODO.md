# XSettlers — Known TODOs

Open work **this phase intends to do**. An item that stops being something we
mean to build does not linger here with a "someday" caveat — it moves to
`docs/dev_history.md` as a settled decision. Combat, multi-game routing, a
`models/` package and an NPC strategy builder all left this file that way; look
for them there before proposing them as new.

## DB & Engine

* [ ] `engine/turn.py` — the `pod.scanned`/`org.scanned` events are still unbuilt, and no NPC strategy scans toward an opponent, so nothing in the library produces contact on its own. Rival detection itself shipped 2026-08-18 (`db/sightings.py`); see `docs/dev_history.md`.

## MCP Tools

* [ ] **Move-tasking response template — canonical.** This is the report a player wants back after ordering a ship to move, and it should be what `confirm_move` (and `set_mission(mission='move')`, which delegates to it) returns. Today they return a bare dict of raw fields and the client improvises the rest. Four parts, in order:

  1. **What was ordered, previewed then committed** — `turns_needed` and `arrival_turn` per ship, then the confirmation. Preview before commit is the intended flow (see the `set_mission` tool description); showing both makes the cost of the order visible before its effect.
  2. **The whole fleet, not just the ships that moved** — `id`, `name`, `where`, `mission`, one row per org. In-transit rows must name the destination (`in transit → (10,12,0)`), because `show_civilization_status`'s `status` string is deliberately terse and a player who just issued a move needs to see it reflected. Showing unmoved orgs is the point: the question after tasking two ships is "what do I still have available," not "did those two ships accept the order."
  3. **What it cost in aggregate** — how many orgs are now off the board, what fraction of total holdings went with them, what remains at home.
  4. **One forward-looking consequence** — the thing that will matter on arrival but is not visible in the table. Example: two destinations 2 sectors out against a scan range of 1, so the two arrival footprints overlap neither each other nor home — two isolated islands of vision. This part is judgment, not a computed field.

  Implementation shape: parts 1–3 are mechanical and belong in a `display` block on the move response, following the same hints convention as `show_organization` and `render_map` (see `docs/ui_and_rendering_design.md`) so any client renders them identically. Part 4 is not mechanizable and stays with whatever agent is narrating.

* [ ] **`set_pod_task` fan-out for a task force.** `order_task_force` fans a *mission* to every member (built — see `docs/dev_history.md`); pod tasking is deliberately not fanned, because unlike a destination or a mission, **a pod id does not generalize across members with different pod loadouts**. The direction's "same pod retask" language never said what the selector is — by index? by matching current task? — and that is a real design decision, not a detail to default silently. Pick one, then add it as its own tool rather than overloading `order_task_force`.

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
