# Restart notes

A compact "pick this up cold" checkpoint — not a backlog (`docs/TODO.md`) or a
narrative history (`docs/dev_history.md`), just what a fresh session needs to
reorient fast: what just happened, what's actually running right now (which
git doesn't capture), and what's mid-thought. Update this in place rather than
appending — it's a snapshot, not a log. Safe to delete/ignore once its
contents are stale and captured elsewhere; nothing here is authoritative that
isn't also in TODO.md/dev_history.md or the code itself.

## Where things stand (2026-08-07)

Working on branch **`game-inception-authentication`** (`7594000`, pushed),
branched off `main` at `85fe15a`. `main` itself has ship's log and the
default-display steering work merged already — this branch is entirely
GameHouse integration + fleet strategies on top of that.

**Fleet-strategy taxonomy is built, not just named** — all four (`turtle`,
`fan_out`, `burst_and_colonize`, `frontier_map_stay_frosty`) are real,
registered functions in `engine/npc.py`'s `STRATEGIES` dict. The fleet-vs-player
schema question from the earlier version of this note (fleets don't exist as a
data-model concept, `npc_profiles` is still `player_id`-keyed) is **still
open** — nothing here changed that, these four are still assigned per-player,
not per-fleet.

**GameHouse handoff is built and live-verified across two real processes** —
see `docs/TODO.md`'s "GameHouse handoff" subsection for the full technical
rundown. Short version: a sibling repo `../gamehouse` is the identity/lobby
orchestrator; xsettlers registers itself with it at startup
(`register_with_gamehouse()`) and exposes `start_session()` for the actual
handoff. **The existing static-roster auth (`xsettlers_mcp/auth.py`,
`config/game_config.yaml`) is untouched** — this is an additional path, not a
replacement; whether to ever retire the old one is undecided. Verified for
real: `welcome`/`verify_code` login on GameHouse → `join_lobby` filling and
closing a lobby → a genuine HTTP push of `start_session` to xsettlers →
xsettlers bootstrapping real ships/pods → the returned `player_token` working
against `get_player_state`. One real bug found and fixed on the way: the
`start_session` tool's declared JSON Schema rejected `null` for `scenario_key`
even though the Python function handled `None` fine — only the live round
trip caught it, not any unit test, which is why
`tests/test_gamehouse.py::test_start_session_tool_schema_permits_null_scenario_key`
now checks the schema declaration itself.

**Process state — not captured by git, check before assuming:**
- Local xsettlers server: running, port 8080, DB is whatever was last left
  from the live GameHouse handoff test (a few `gamehouse-*@handoff` players
  bootstrapped). Scratch state, fine to `rm xsettlers.db` and restart clean.
- Local GameHouse server (`../gamehouse`, sibling repo): running, port 8090
  (not its default 8080 — collides with xsettlers otherwise), its own
  `gamehouse.db`. Started via `DB_PATH=gamehouse.db PORT=8090 .venv/bin/python3
  -m gamehouse_mcp.server` from that repo's root. Both need
  `GAMEHOUSE_URL=http://localhost:8090/mcp` and
  `XSETTLERS_PUBLIC_URL=http://localhost:8080/mcp` set when starting
  xsettlers, or registration silently no-ops (by design — see
  `register_with_gamehouse`'s docstring).
- **GameHouse's own contract has moved at least twice already** since first
  read this session — `describe_lobby()` was fully removed (superseded by a
  push-based `register_game` model) and `start_session` gained a
  `scenario_key` field, mid-session, without warning. Don't trust anything
  above about GameHouse's wire shape without re-reading `../gamehouse/docs/data_model.md`
  fresh — it's a fast-moving sibling project, not a stable external dependency.
- Fly deployment (`xsettlers.fly.dev`): last known state (2026-08-06) was
  running and healthy at commit `85fe15a` — **predates this entire branch**,
  including ship's log's pod-tasking/at_turn additions were already on it,
  but none of the fleet-strategy or GameHouse work has been deployed.
  Re-verify with `fly status --app xsettlers` before trusting this.

## Open threads

- **Fleet-vs-player schema** (carried over, still unresolved): `npc_profiles`
  is `player_id`-keyed; a real fleet concept doesn't exist. Don't build
  toward multiple fleets per player without resolving this first.
- **GameHouse's own open items that create required xsettlers-side surface**
  (named directly in their docs, not built on either side): a results-object
  hand-back to GameHouse at game completion, and a run-state query GameHouse
  can poll before offering a Person reconnect.
- **Multi-scenario support** — xsettlers registered with GameHouse as
  scenario-less (`scenarios=[]`); `start_session`'s `scenario_key` is accepted
  but not branched on. Outbreak/Solo have no path to GameHouse yet.
- **Old auth code's fate** — explicitly deferred, not decided: keep both
  paths indefinitely, or retire the static roster once GameHouse-driven
  sessions are the only real usage.

## If picking this up fresh, in order

1. Check `fly apps list` / `fly status --app xsettlers` before trusting
   anything above about deployment state — it's changed hands multiple times
   this project already.
2. Re-read `../gamehouse/docs/data_model.md` before touching
   `xsettlers_mcp/gamehouse.py` — that contract has already moved twice
   without warning this session.
3. Read `docs/TODO.md`'s "GameHouse handoff" and "Fleet-strategy taxonomy"
   subsections for the technical detail this file deliberately doesn't repeat.
