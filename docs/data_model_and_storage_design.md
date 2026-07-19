# XSettlers — Data Model & Storage Design

# Overview

This document captures the data storage design decisions for the XSettlers game engine (formerly Outlanders), as well as the rationale behind them. The goal is a Python-centric, zero-cost POC that can scale to a production backend later.

---

# The Problem Space

The game requires a **master model** — a canonical shared state of the universe — from which each player sees only a **partial view** (their ships, their colonies, sectors they've scouted). There are two directions of interaction:

* **Writes**: the game engine updates the master model on a server-side clock interval (end-of-turn calculations, production, movement, events) — independent of player activity
* **Reads**: players query the current state on demand, in natural language via Slack — all interaction is player-initiated

The model must support:

* A **sparse, infinite 3D grid** (Sectors are only instantiated when a player interacts with them)
* **Spatial queries** — jump range radius, adjacency, nearest neighbors
* **Object tree traversal** — Pods → Ships/Colonies → Sectors → Players
* **Player-scoped partial views** — every query filtered by player identity
* Low object counts for POC; designed to scale later

---

# Storage Choice: SpatiaLite

## Why SpatiaLite

| Option | Spatial Queries | Graph Traversal | Zero Infrastructure | Python Support |
|---|---|---|---|---|
| **SpatiaLite** | ✅ Full | ⚠️ Adequate | ✅ Single file | ✅ Native |
| PostgreSQL + PostGIS | ✅ Full | ⚠️ Adequate | ❌ Server required | ✅ Excellent |
| Redis | ⚠️ 2D only | ❌ Weak | ❌ Server required | ✅ Good |
| Neo4j Community | ✅ Good | ✅ Excellent | ❌ Server required | ✅ Good |
| MongoDB | ⚠️ Limited | ❌ Weak | ❌ Server required | ✅ Good |

**SpatiaLite** was chosen for the POC because:

* It is a single `.db` file — no server process, no config, no ports
* It is SQLite + `mod_spatialite` extension — standard, open source, free
* Full spatial query support including 3D geometry (`POINTZ`)
* The schema migrates cleanly to **PostGIS** when a real server is needed

**Neo4j** remains the long-term candidate for at-scale deployments where traversing large object trees (Pods → Ships/Colonies → Sectors) becomes a performance concern.

---

# Python Setup

```
# macOS
brew install spatialite-tools

# Ubuntu/Debian
sudo apt-get install spatialite-bin libsqlite3-mod-spatialite
```

```python
import sqlite3

conn = sqlite3.connect("outlanders.db")
conn.enable_load_extension(True)
conn.load_extension("mod_spatialite")
conn.execute("SELECT InitSpatialMetaData(1)")  # Run once on new database
```

No additional `pip install` required — uses Python's built-in `sqlite3` module.

---

# Core Schema

## Players

```sql
CREATE TABLE players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    slack_user_id TEXT UNIQUE,  -- resolved at login; links Slack identity to player
    end_turn_declared INTEGER DEFAULT 0  -- 1 = player has declared end of turn this tick
);
```

The `slack_user_id` column is the key integration point: when a player queries via Slack, their Slack identity resolves directly to their player record, and every query is automatically filtered to their partial view.

> **Object Graph Summary:** Pods belong to Organizations (Ships or Colonies). Organizations are subclasses of a master `Organization` class. An Organization is located in a Sector and is owned by a Player. **Sectors are not owned by players** — they are neutral game-board cells that any player's ships or colonies may occupy.

## Sectors

```sql
CREATE TABLE sectors (
    id INTEGER PRIMARY KEY,
    coord_x INTEGER NOT NULL,
    coord_y INTEGER NOT NULL,
    coord_z INTEGER NOT NULL DEFAULT 0,
    energy_capacity REAL,
    food_capacity REAL,
    goods_capacity REAL,
    discovered_by INTEGER  -- player_id
);
SELECT AddGeometryColumn('sectors', 'location', -1, 'POINTZ', 'XYZ');
CREATE UNIQUE INDEX idx_sector_coords ON sectors(coord_x, coord_y, coord_z);
```

Sectors are **lazily instantiated** — only created when a player interacts with them, keeping the sparse grid efficient. The `POINTZ` geometry column enables true 3D spatial queries. Note: sectors have no `player_id` — ownership is not a sector property. A player's presence in a sector is determined by the organizations they have located there.

## Organizations (Ships & Colonies)

```sql
CREATE TABLE organizations (
    id INTEGER PRIMARY KEY,
    org_type TEXT CHECK(org_type IN ('ship','colony')),
    name TEXT,
    player_id INTEGER REFERENCES players(id),
    sector_id INTEGER REFERENCES sectors(id),
    is_mobile INTEGER DEFAULT 1
);
```

Ships and Colonies share a single table (single-table inheritance). `is_mobile` and `org_type` serve **two distinct purposes** — see *Colonization Transition* below.

## Arrival Queue

```sql
CREATE TABLE arrival_queue (
    arrival_turn     INTEGER NOT NULL,
    org_id           INTEGER REFERENCES organizations(id),
    dest_sector_id   INTEGER REFERENCES sectors(id),
    origin_sector_id INTEGER REFERENCES sectors(id),  -- rubber-band target on cancel
    PRIMARY KEY (arrival_turn, org_id)
);
```

## Colonization Transition

Converting a ship to a colony takes **three turns**. The transition is modeled entirely with existing fields — no schema changes required.

**Mechanics:**

1. Player calls `set_mission('colonize', ...)` on a ship
2. `organizations.is_mobile` flips to `0` immediately — the ship is committed and grounded for the duration
3. An event is written to the events table with `event_type = 'colonize_complete'` and `turn = current_turn + 3`
4. At end-of-turn, `engine/turn.py` checks for any `colonize_complete` events where `turn <= current_turn`
5. On match: `org_type` is flipped to `'colony'` and a `ship.colonized` event is written to the log

**State during the 3-turn window:**

| Field | During transition | After completion |
|---|---|---|
| `org_type` | `'ship'` | `'colony'` |
| `is_mobile` | `0` | `0` |
| `mission` | `'colonize'` | `null` |

The `mission` field doubles as the in-progress state indicator — no separate "transitioning" status is needed. Navigation tools already filter on `is_mobile = 1`, so the ship is automatically excluded from movement during the window.

**Event payload (colonize_complete):**

```json
{
  "event_type": "colonize_complete",
  "actor_id": null,
  "subject_type": "organization",
  "subject_id": "<org_id>",
  "turn": "<current_turn + 3>",
  "payload": { "org_id": "<org_id>", "sector_id": "<sector_id>" }
}
```

`origin_sector_id` is captured at the moment `confirm_move` is called (before the ship is parked at the sentinel). If the player cancels mid-flight, the ship snaps back to `origin_sector_id` — no partial credit for distance traveled. This keeps in-transit state completely stateless: the only thing that matters is where you started and where you're going.

`is_mobile` is a **behavioral state flag** (can this org move right now?) that changes during the game. `org_type` is a **semantic label** (what kind of org is this?) used for player-facing queries, display, and logic branching. They are intentionally separate: `is_mobile` can be `0` on a `ship` during a colonization in progress, while `org_type` stays `'ship'` until the transition completes.

## Pods

```sql
CREATE TABLE pods (
    id INTEGER PRIMARY KEY,
    org_id INTEGER REFERENCES organizations(id),
    mission TEXT DEFAULT 'idle',  -- idle | produce_energy | produce_food | produce_goods | scan
    mission_params TEXT,          -- JSON: e.g. {"target_sector_id": 12} for scan mission
    energy_consumption REAL,
    food_consumption REAL,
    storage_capacity REAL DEFAULT 0,  -- innate maximum storage for this pod
    storage_current  REAL DEFAULT 0   -- current fill level; sum across all player pods = inventory
);
```

Pod missions: `idle`, `produce_energy`, `produce_food`, `produce_goods`, `scan`. No `pod_type` field — mission defines the pod's role entirely. The colloquial names *energy*, *farm*, and *factory* correspond to `produce_energy`, `produce_food`, and `produce_goods` respectively.

## Events (Write-Ahead Log)

```sql
CREATE TABLE events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      INTEGER,                              -- future multi-game support
    turn         INTEGER NOT NULL,                      -- game turn number at time of event
    seq          INTEGER NOT NULL,                      -- ordering within a turn
    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    event_type   TEXT NOT NULL,                          -- e.g. 'ship.moved', 'pod.produced', 'turn.snapshot'
    actor_id     INTEGER REFERENCES players(id),        -- who caused it (NULL for engine events)
    subject_id   INTEGER,                                -- org_id, sector_id, pod_id, etc.
    subject_type TEXT,                                   -- 'organization', 'sector', 'pod', 'player'
    payload      TEXT NOT NULL DEFAULT '{}'              -- JSON: delta for player actions, full snapshot for engine events
);

CREATE INDEX idx_events_turn    ON events(turn, seq);
CREATE INDEX idx_events_actor   ON events(actor_id);
CREATE INDEX idx_events_subject ON events(subject_type, subject_id);
CREATE INDEX idx_events_type    ON events(event_type);
```

**Hybrid payload strategy (agreed design):**

* **Player-action events** (`actor_id IS NOT NULL`) — store a **delta**: only what changed, e.g. `{"from_sector": 3, "to_sector": 7}`. Compact and human-readable.
* **End-of-turn engine events** (`event_type = 'turn.snapshot'`, `actor_id IS NULL`) — store a **full snapshot** of all affected state. Self-contained recovery checkpoint.

This means:

* **Point-in-time replay**: load the nearest `turn.snapshot`, then replay player-action deltas forward to any moment within that turn.
* **Disaster recovery**: on server boot, load the last `turn.snapshot` + re-apply any player-action deltas written after it.
* **Service interruption resilience**: player actions are written to `events` first (write-ahead), then applied to game state tables. If the server crashes between steps, the event log is the source of truth — re-apply on restart.

**Starter event type taxonomy:**

| Event type | Actor | Payload style |
|---|---|---|
| `ship.move_previewed` | player | read-only: `{"org_id", "dest_sector_id", "turns_needed", "arrival_turn"}` |
| `ship.move_confirmed` | player | delta: `{"org_id", "from_sector_id", "to_sector_id", "arrival_turn"}` |
| `ship.move_cancelled` | player | delta: `{"org_id", "rubber_banded_to_sector_id"}` |
| `ship.arrived` | engine | delta: `{"org_id", "sector_id", "turn"}` |
| `mission.set` | player | delta: `{"org_id", "mission", "params"}` |
| `pod.mission_set` | player | delta: `{"pod_id", "mission", "params"}` |
| `pod.scan_target_set` | player | delta: `{"pod_id", "target_sector_id"}` |
| `turn.declared` | player | delta: `{"player_id"}` |
| `ship.colonized` | engine | delta: `{"org_id", "sector_id"}` |
| `pod.produced` | engine | delta: `{"pod_id", "resource", "amount", "new_level"}` |
| `pod.scanned` | engine | delta: `{"pod_id", "org_id", "sector_id", "confidence_set_to"}` |
| `alert.rival_detected` | engine | delta: `{"scanning_org_id", "rival_org_id", "sector_id"}` |
| `alert.scan_out_of_range` | engine | delta: `{"pod_id", "org_id", "target_sector_id", "distance", "range"}` |
| `fog.decayed` | engine | delta: `{"player_id", "sector_id", "old_confidence", "new_confidence"}` |
| `turn.snapshot` | engine | full snapshot of all players, orgs, pods, player_sectors |

## Player Sectors (Fog of War)

```sql
CREATE TABLE player_sectors (
    player_id INTEGER REFERENCES players(id),
    sector_id INTEGER REFERENCES sectors(id),
    confidence INTEGER NOT NULL DEFAULT 100,
    PRIMARY KEY (player_id, sector_id)
);
```

This table is **sparse** — rows exist only for sectors a player has discovered. Confidence rules:

* **New discovery:** row inserted with `confidence = 100`
* **Player org present:** `confidence` reset to `100` each tick
* **Unoccupied sector:** `confidence = MAX(0, ROUND(confidence * 0.9))` each tick (~10% decay)
* **Confidence hits 0:** sector shows a degraded "last known" indicator — it does not disappear from the player's map. The row is retained as a ghost memory showing stale information.

Query for a player's current visible sectors:

```sql
SELECT s.*, ps.confidence
FROM sectors s
JOIN player_sectors ps ON ps.sector_id = s.id
WHERE ps.player_id = :player_id
AND ps.confidence > 0
ORDER BY ps.confidence DESC;
```

A player's total inventory of any resource is computed as `SUM(storage_current)` across all pods in all their organizations, filtered by resource type. No separate inventory table is needed.

---

# Spatial Query Example

**"What sectors are within jump range 3 of my ship?"**

```sql
SELECT s2.id, s2.coord_x, s2.coord_y, s2.coord_z
FROM sectors s1
JOIN organizations o ON o.sector_id = s1.id
JOIN sectors s2 ON (
    ((s2.coord_x - s1.coord_x) * (s2.coord_x - s1.coord_x) +
     (s2.coord_y - s1.coord_y) * (s2.coord_y - s1.coord_y) +
     (s2.coord_z - s1.coord_z) * (s2.coord_z - s1.coord_z)) <= 9  -- 3^2
)
WHERE o.id = :ship_id;
```

This uses integer grid math for the POC. When migrating to PostGIS, this becomes a native `ST_3DDistance` call with identical semantics.

---

# Migration Path

When the POC outgrows SpatiaLite:

1. Stand up a PostgreSQL instance with the PostGIS extension
2. Port the schema — `AddGeometryColumn` syntax is nearly identical
3. Replace the Python connection string — `sqlite3` → `psycopg2` or `SQLAlchemy`
4. Spatial queries require minimal changes (`ST_3DDistance` replaces manual distance math)

This is a well-documented, well-traveled migration path.

---

# Next Steps

* [x] Design the MCP server layer — tools and endpoints that expose this model to Slack
* [x] Define player identity / partial view scoping model
* [x] Scaffold the Python project structure for XSettlers
* [x] Implement `bootstrap_game()` shim to seed origin sector, initial players, and starting ships
* [x] Implement end-of-turn loop — reset `end_turn_declared = 0` as step one, then run production calculations, then decay fog-of-war confidence
* [x] Design events table — write-ahead log with hybrid payload strategy (player-action deltas + end-of-turn snapshots) for replay, disaster recovery, and service interruption resilience
* [x] Codify colonization transition — 3-turn event queue mechanic, `is_mobile` vs `org_type` split
* [ ] **Refinement:** Review sector schema to ensure no ownership fields creep in; player presence in a sector is always derived from Organization location, never stored on the Sector itself
* [ ] **Future consideration:** Evaluate storing a denormalized vector of active player IDs on the Sector for fast "who's in this sector?" lookups — only worth pursuing if join-based lookups become a bottleneck at scale
* [ ] **Future:** Evaluate Neo4j Community Edition for at-scale object tree traversal
