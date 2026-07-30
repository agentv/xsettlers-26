# XSettlers — MCP Server Layer Design

# Overview

This document captures the design and hosting decisions for the XSettlers MCP server layer — the component that sits between Slack (Slackbot) and the SpatiaLite game model, translating player queries into scoped database operations and returning structured results.

---

# Role of the MCP Server

The MCP server is the integration linchpin. Its responsibilities:

* Receive tool calls from Slackbot with the player's Slack identity attached
* Resolve that Slack identity to a player record in the database
* Execute queries scoped to that player's **partial view** of the game world
* Return structured results that Slackbot renders as natural language responses
* Accept **write** operations (moves, conversions) initiated by players via Slack

**Game state lives entirely on the server.** The clock ticks server-side on a fixed interval; end-of-turn calculations run independently of player activity. Players never push state into Slack — they only pull it. All Slack interactions are read-initiated: a player asks a question, Slackbot calls the MCP server, the server returns the current state.

---

# MCP Tools (POC Surface)

These are the minimum tools the server needs to expose for a functional POC:

Tools are organized by concern — focused, composable, and named after what a player would naturally ask.

**Player tools**

| Tool | Description |
|---|---|
| `get_player_state` | Dashboard summary — player record, all organizations, all pods. Use for broad "catch me up" queries |
| `declare_end_turn` | Player signals no further moves this tick; triggers clock acceleration if all players agree |
| `rescind_end_turn` | Withdraws end turn declaration, provided consensus hasn't fired yet |

**Sector tools**

| Tool | Description |
|---|---|
| `get_sector` | Details of a specific sector — scoped to player visibility (confidence > 0) |
| `get_sector_map` | All sectors visible to this player, ordered by confidence |

**Navigation tools**

| Tool | Description |
|---|---|
| `preview_move` | Preview travel distance/turns to a destination without committing |
| `confirm_move` | Commit a ship to travel to a destination; ship enters transit (parked at sentinel sector) until arrival turn |
| `cancel_move` | Cancel an in-progress move, rubber-banding the ship back to its origin sector |

**Organization & Pod command tools**

| Tool | Description |
|---|---|
| `set_mission` | Set an organization's strategic mission: `idle`, `move`, `colonize`, `defend`, `attack` |
| `set_pod_task` | Set a pod's productive task: `idle`, `produce`, `mine` — pod-level work is independent of org mission |

**Mission vs Task — the key split:**

* **Organization missions** govern strategic intent: where to go, whether to colonize, combat posture. Missions are: `idle`, `move`, `colonize`, `defend`, `attack`.
* **Pod tasks** govern productive work: what each pod does each tick. Tasks are: `idle`, `produce`, `mine`.

> **Note:** the Product Requirements and Data Model & Storage Design canvases supersede this with a `mission`-based vocabulary for pods (`set_pod_mission`, missions `idle`/`produce_energy`/`produce_food`/`produce_goods`/`scan`) rather than the `task`-based vocabulary (`set_pod_task`, tasks `idle`/`produce`/`mine`) described in this section. This canvas is retained verbatim for its hosting/gateway design; treat `set_pod_mission` as authoritative for pod command implementation.

A ship on a `move` mission still has farm pods ticking away producing food during the journey. The two concerns are genuinely independent.

`confirm_move` is the player-facing tool that actually commits movement — it sets the org mission to `move`, parks the ship at the sentinel sector (-1,-1,-1), and inserts a row into the `arrival_queue`. `preview_move` runs the same distance/turn-count calculation without committing, and `cancel_move` reverses an in-progress move. End-of-turn resolves arrivals and reveals the destination sector at confidence 100.

The server-side clock fires on a fixed interval as the fallback. If all active players call `declare_end_turn`, the clock accelerates immediately.

---

# Player Identity & Partial View Scoping

> **Note:** this section's `slack_user_id` naming is superseded (2026-07-22) by the client-agnostic `player_token` — same role (identity argument on every tool call), renamed so any MCP client, not just Slack, can authenticate. See `CLAUDE.md` and `docs/TODO.md` for what changed; this canvas is retained verbatim otherwise.

Every tool call carries the calling player's `slack_user_id`. The MCP server:

1. Resolves `slack_user_id` → `player_id` via the Players table
2. Applies that `player_id` as a filter on every query
3. Returns only data the player is entitled to see

This keeps partial-view logic centralized in the MCP server, not scattered across clients.

---

# Gateway Layer (Auth, Game Selection & Bootstrap)

The gateway is the **outer shell of the MCP server** — it runs as the first step on every tool call, before any game logic is reached. It is not a separate network service; it lives inside `mcp/` and is invoked by `server.py`.

## Responsibilities

1. **Authenticate the player** — resolve and verify the caller's `slack_user_id` against the Players table. For now this trusts the Slack-provided identity; hardening is a known TODO.
2. **Select the game** — look up which game instance this player belongs to. Currently there is only one game, but the slot exists for future multi-game support.
3. **Bootstrap if needed** — if the game has not yet been initialized, call `bootstrap_game()` from `db/bootstrap.py` before proceeding.

Only after all three steps pass is control handed off to the tool dispatcher.

## Flow

```
Slack → Slackbot AI → MCP tool call
               │
          mcp/server.py
               │
          mcp/gateway.py
          ├── authenticate(slack_user_id)   → Player
          ├── select_game(player)           → game_id
          └── ensure_bootstrapped(game_id)  → (runs bootstrap_game() if needed)
               │
          tools/ dispatcher
          (player_tools, sector_tools, navigation_tools, organization_tools)
```

## Files

| File | Purpose |
|---|---|
| `mcp/gateway.py` | Entry pre-flight — orchestrates auth → game select → bootstrap |
| `mcp/auth.py` | `authenticate(slack_user_id)` → Player record (trusts Slack identity for now) |
| `mcp/game_select.py` | `select_game(player)` → game_id (stub: one game; extensible to a `games` table) |

---

# Python Implementation

The standard library is the `mcp` Python SDK. A minimal server skeleton:

```python
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

app = Server("xsettlers")

@app.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="get_player_state",
            description="Get the full state of the calling player's organizations and pods",
            inputSchema={"type": "object", "properties": {
                "slack_user_id": {"type": "string"}
            }}
        ),
        # ... additional tools
    ]

@app.call_tool()
async def call_tool(name, arguments):
    if name == "get_player_state":
        return query_player_state(arguments["slack_user_id"])
```

Each tool handler queries SpatiaLite and returns structured data. No framework required beyond the `mcp` SDK and Python's built-in `sqlite3` module.

---

# Hosting Options Considered

| Option | Pros | Cons | Cost |
|---|---|---|---|
| **Fly.io** ✅ | Persistent, warm, easy Python deploy, persistent volumes for `.db` file | Slight learning curve | Free tier |
| Local + ngrok | Zero setup friction | Not persistent, requires machine to be running | Free |
| Render | Easy deploy | Free tier spins down on inactivity (cold start delay) | Free tier |
| Railway | Easy deploy, good DX | Free tier limits | Free tier |
| VPS (Hetzner / DO) | Full control | Requires more ops work | ~$4–6/mo |

---

# Hosting Selection: Fly.io

**Fly.io** is the recommended host for the POC because:

* Free tier supports small persistent Python apps
* Persistent volume storage keeps the SpatiaLite `.db` file alive between deploys
* Apps stay warm — no cold start penalty during play sessions
* Single CLI deploy: `fly launch` + `fly deploy`
* Clear upgrade path when XSettlers moves to PostGIS on a proper server

### Deployment sketch

```
# Install Fly CLI
brew install flyctl

# Authenticate
fly auth login

# Launch app (run from project root)
fly launch --name xsettlers-mcp --region sjc

# Attach a persistent volume for the database
fly volumes create xsettlers_data --size 1

# Deploy
fly deploy
```

The SpatiaLite `.db` file is mounted at `/data/xsettlers.db` on the persistent volume.

---

# Next Steps

* [x] Scaffold the Python MCP server project structure
* [ ] Implement `get_player_state` as the first tool end-to-end
* [ ] Write `organization_tools.py` — `set_mission` and `set_pod_task`
* [ ] Wire up Slack identity resolution (Slack user ID → player record)
* [ ] Deploy skeleton to Fly.io and verify Slackbot can reach it
* [ ] Implement remaining POC tools
* [ ] Implement `_handle_defend` and `_handle_attack` in `engine/turn.py`
* [ ] Define org-level resource pool so consumption can be deducted at end-of-turn
* [ ] Review setup steps document and incorporate into this design
