# Restart notes

A compact "pick this up cold" checkpoint — not a backlog (`docs/TODO.md`) or a
narrative history (`docs/dev_history.md`), just what a fresh session needs to
reorient fast: what just happened, what's actually running right now (which
git doesn't capture), and what's mid-thought. Update this in place rather than
appending — it's a snapshot, not a log. Safe to delete/ignore once its
contents are stale and captured elsewhere; nothing here is authoritative that
isn't also in TODO.md/dev_history.md or the code itself.

## Where things stand (2026-08-05)

`main` is at `c0c3a89` ("Add ship's log: queued commands tied to the clock"),
pushed, working tree clean.

**Ship's log is built and merged** — see `docs/TODO.md`'s "Design (Data Model
canvas)" section for the full technical rundown (schema, four trigger
primitives, the `engine/movement.py`/`engine/pod_tasking.py` extraction and
why). Short version: `queue_command` lets a player/NPC defer an action
(`move` or `set_pod_task`) against an org to fire `during_transit` (on
departure), `before_arrival`/`after_arrival` (relative to a move's
`arrival_turn`), or `at_turn` (an explicit absolute turn). `fan_out_consolidate`
migrated onto it, deleting its old hand-rolled polling. Live-verified on the
local server as well as covered by `tests/test_ship_log.py` (212 tests total,
all green as of this commit).

**Process state — not captured by git, check before assuming:**
- Local server: running (`python -m xsettlers_mcp.server`, port 8080), DB is
  whatever was last left from live ship's-log verification (Solo scenario,
  a few ships mid-chain). Treat as scratch state, not a game in progress —
  fine to `rm xsettlers.db` and restart clean.
- Fly deployment (`xsettlers.fly.dev`): machine is up, but running the image
  deployed 2026-08-01 — **predates ship's log entirely.** `queue_command`
  and the `org_command_queue` table do not exist there yet. Needs `fly
  deploy` before ship's log is usable through the public URL. See
  `project_fly_deployment` memory for the redeploy command if the app/machine
  state has changed since.

## Open thread: fleet-strategy taxonomy (named, not built)

Mid-conversation, not yet a plan-mode design pass. Four NPC/fleet behavioral
styles got named and loosely characterized — **turtle**, **fan_out**,
**burst-and-colonize**, **frontier-map-stay-frosty** — full descriptions in
`docs/TODO.md`'s new "Fleet-strategy taxonomy" subsection under "NPC strategy
profiles." The one architecturally load-bearing point, easy to lose in a
context reset: **these are meant to be fleet-scoped, not player-scoped** — a
player runs multiple fleets, each on its own strategy — which `npc_profiles`
(currently `player_id`-keyed) doesn't support, and fleets don't exist as a
data-model concept at all yet. Don't start implementing any of the four
styles without first resolving that schema question, or the work will need
redoing once fleets exist.

Two of the four already have real comparative data behind them, from an
earlier mock 2-player Diaspora run this session (not yet written up in
`docs/dev_history.md`): a burst-and-colonize-shaped strategy (fan out 6
ships, colonize 2 at home turn 1) scored 2574 vs. a pure-turtle opponent's
2240 — 14.9% ahead, over 20 turns.

## If picking this up fresh, in order

1. Check `fly apps list` / `fly status --app xsettlers` before trusting
   anything above about deployment state — it's changed hands (destroyed,
   redeployed, stopped, started) multiple times this project already.
2. Read `docs/TODO.md`'s two updated sections (ship's log entry, NPC
   strategy profiles) for the technical detail this file deliberately
   doesn't repeat.
3. If resuming the fleet-strategy work: the fleet-vs-player schema question
   is the actual next decision, not which style to code first.
