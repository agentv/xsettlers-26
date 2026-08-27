from db.connection import get_connection


def _drop_spatialite_triggers(cur):
    """
    Remove the triggers SpatiaLite's AddGeometryColumn left on `sectors`.

    Only a database created before SpatiaLite was dropped has them, which
    means a deployed volume rather than a fresh file. Two of them call
    GeometryConstraints(), a function that no longer exists, so they fail
    every INSERT into sectors -- including the sentinel row this function
    inserts, which makes the failure a crash on boot rather than a bug that
    waits for a reveal. The sectors.location column and spatial_ref_sys and
    friends are inert once these are gone; VACUUM reclaims their ~7 MB.
    """
    cur.execute("""SELECT name FROM sqlite_master
                   WHERE type='trigger' AND tbl_name='sectors'""")
    for (name,) in cur.fetchall():
        cur.execute(f'DROP TRIGGER IF EXISTS "{name}"')


def init_schema():
    """Create all tables. Safe to run on an existing DB."""
    conn = get_connection()
    cur = conn.cursor()
    _drop_spatialite_triggers(cur)
    # Otherwise no migration step: the CREATE TABLE statements below are the
    # whole schema. See docs/dev_history.md if a database older than the
    # current shape ever turns up.
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            email             TEXT NOT NULL UNIQUE,
            display_name      TEXT NOT NULL,
            player_token      TEXT UNIQUE,
            end_turn_declared INTEGER DEFAULT 0,
            is_npc            INTEGER DEFAULT 0,
            -- GameHouse's own person.id, stamped by start_session() for
            -- person-kind seats ONLY (see xsettlers_mcp/gamehouse.py). NULL
            -- for NPCs -- GameHouse mints them ephemeral labels with no
            -- Person behind them -- and for the static-roster path, which
            -- has no GameHouse identity at all.
            --
            -- So `gamehouse_person_id IS NOT NULL` is exactly "this result
            -- goes back to GameHouse", which is why the results hand-back
            -- filters on it rather than on is_npc: the omission rule
            -- enforces itself instead of being a filter each caller has to
            -- remember. It also replaces recovering the id by parsing the
            -- synthesized gamehouse-N@handoff email.
            gamehouse_person_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS sectors (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            coord_x         INTEGER NOT NULL,
            coord_y         INTEGER NOT NULL,
            coord_z         INTEGER NOT NULL DEFAULT 0,
            -- Energy is the ONLY resource drawn from the map; food and goods
            -- are manufactured from resources already held, so a sector has
            -- no food/goods pool to deplete (see engine/production.py's
            -- RESOURCE_CAPACITY_COLUMN).
            energy_capacity REAL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sector_coords ON sectors(coord_x, coord_y, coord_z);
        -- Where the map is richer than the ordinary discovery roll, and by how
        -- much. Seeded once by db/bootstrap.py from the scenario's `map:` block
        -- and read at every reveal (db/sectors.py's richness_multiplier).
        --
        -- Lives in the DB rather than being re-read from the scenario file at
        -- reveal time for two reasons: a scenario's `scatter:` rule is expanded
        -- with the map seed at bootstrap, so the concrete placement exists only
        -- once and must survive a server restart; and db/ has no scenario handle
        -- at reveal time, only a cursor.
        --
        -- Nothing player-facing reads this table. A player learns a hotspot
        -- exists by scanning into it and seeing what the sector rolled -- the
        -- layout itself is not discoverable through any tool, which is what
        -- lets a scenario keep its map secret.
        CREATE TABLE IF NOT EXISTS map_hotspots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            center_x   INTEGER NOT NULL,
            center_y   INTEGER NOT NULL,
            center_z   INTEGER NOT NULL DEFAULT 0,
            -- Euclidean, inclusive, in sectors. 0 covers the centre alone.
            radius     REAL NOT NULL DEFAULT 0,
            multiplier REAL NOT NULL DEFAULT 1.0
        );
        CREATE TABLE IF NOT EXISTS game_state (
            id            INTEGER PRIMARY KEY DEFAULT 1,
            current_turn  INTEGER DEFAULT 0,
            -- When the clock will next tick, stamped by engine/clock.py's
            -- run_clock(). Persisted so a status report running as its own
            -- process (scripts/status.py) can show a countdown without asking
            -- the live server. Stale whenever the server isn't running, which
            -- a reader cannot tell from the value alone -- see
            -- engine.turn.get_next_tick_at's docstring.
            next_tick_at  TEXT,
            -- Developer hold: engine/clock.py polls this once a second and
            -- skips the tick while set, so end_of_turn() can be stopped from
            -- firing (via scripts/clock.py) without killing the server -- e.g.
            -- while inspecting state that a tick would change underneath you.
            frozen        INTEGER DEFAULT 0,
            CHECK (id = 1)
        );
        INSERT OR IGNORE INTO game_state (id, current_turn) VALUES (1, 0);
        CREATE TABLE IF NOT EXISTS games (
            id              INTEGER PRIMARY KEY DEFAULT 1,
            scenario_name   TEXT NOT NULL,
            scenario_file   TEXT NOT NULL,
            selected_by     TEXT,
            bootstrapped_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            CHECK (id = 1)
        );
        -- GameHouse handoff: the session_token GameHouse pushes via
        -- start_session() (see xsettlers_mcp/gamehouse.py), stored so it
        -- exists locally ("the game verifies every request against its
        -- own local copy" per GameHouse's docs/data_model.md) -- not yet
        -- wired into any request-gating logic. The existing static-roster
        -- auth (xsettlers_mcp/auth.py) is untouched and remains the only
        -- thing that actually gates access today; this table is preparation,
        -- not a replacement.
        CREATE TABLE IF NOT EXISTS game_session (
            id            INTEGER PRIMARY KEY DEFAULT 1,
            session_token TEXT NOT NULL,
            CHECK (id = 1)
        );
        -- A player-named, explicitly managed roster of that player's own
        -- ships (never colonies -- see organizations.task_force_id). Carries
        -- no co-location or state requirement of its own; membership changes
        -- only by direct action (create/add/remove), with one exception --
        -- a member flips out the moment it colonizes (engine/turn.py's
        -- _handle_colonize), since a task force cannot hold anything but a
        -- ship. An order against one is a fan-out over the ordinary
        -- single-org tools, not a new turn-resolution step -- see
        -- xsettlers_mcp/tools/task_force_tools.py.
        CREATE TABLE IF NOT EXISTS task_forces (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL REFERENCES players(id),
            name      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS organizations (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            org_type       TEXT CHECK(org_type IN ('ship','colony')),
            name           TEXT,
            player_id      INTEGER REFERENCES players(id),
            sector_id      INTEGER REFERENCES sectors(id),
            is_mobile      INTEGER DEFAULT 1,
            mission        TEXT DEFAULT 'idle'
                           CHECK(mission IN ('idle','move','colonize','defend','attack')),
            mission_params TEXT,
            -- NULL for a ship not currently in any task force, and always
            -- NULL for a colony -- enforced at the tool layer (see
            -- task_force_tools.py), not by a CHECK, since org_type and this
            -- column are set by separate statements at separate times.
            task_force_id INTEGER REFERENCES task_forces(id),
            -- Innate scan: every organization can scan one sector per turn on
            -- its own account -- a ship's bridge, a colony's headquarters --
            -- without spending a pod on it. Aimed by an OFFSET from the
            -- org's own sector, not absolute coordinates, so the aim
            -- survives a move (see bearings.SCAN_BEARINGS).
            -- Persistent across turns until changed or cleared. Costs the same
            -- food as a scan pod and is suppressed in transit, exactly as if
            -- the org carried one scan pod already.
            scan_offset_x  INTEGER,
            scan_offset_y  INTEGER,
            scan_offset_z  INTEGER
        );
        CREATE TABLE IF NOT EXISTS pods (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id           INTEGER REFERENCES organizations(id),
            -- `task` is what this pod's crew does; the parent organization's
            -- `mission` is what the vehicle does. Deliberately different
            -- words for deliberately different concepts.
            task             TEXT DEFAULT 'idle'
                             CHECK(task IN ('idle','produce_energy','produce_food','produce_goods','scan')),
            task_params      TEXT,
            storage_capacity REAL DEFAULT 0,
            energy_stored    REAL DEFAULT 0,
            food_stored      REAL DEFAULT 0,
            goods_stored     REAL DEFAULT 0
        );
        -- What a player's scans have seen of other players' organizations.
        -- Distinct from player_sectors, which records knowledge of the GROUND:
        -- a sector's richness is a fact that stays true once learned, while an
        -- organization moves, so a sighting is a dated observation rather than
        -- current intel and every reader must present it with its turn.
        --
        -- One row per (observer, org): re-sighting the same org updates where
        -- and when it was last seen rather than accumulating history. The
        -- engine has no use for the full track, and a player asking "where is
        -- it now" is better served by "last seen at X on turn N" than by a
        -- list they have to read the end of.
        --
        -- owner_id and org_type are denormalized deliberately. They are part
        -- of what was observed, and joining organizations to recover them
        -- would report today's truth about a ship the observer last saw ten
        -- turns ago -- including, once combat exists, one that no longer
        -- exists at all.
        CREATE TABLE IF NOT EXISTS org_sightings (
            observer_id  INTEGER NOT NULL REFERENCES players(id),
            org_id       INTEGER NOT NULL REFERENCES organizations(id),
            owner_id     INTEGER NOT NULL REFERENCES players(id),
            sector_id    INTEGER NOT NULL REFERENCES sectors(id),
            org_type     TEXT    NOT NULL,
            seen_at_turn INTEGER NOT NULL,
            PRIMARY KEY (observer_id, org_id)
        );
        CREATE TABLE IF NOT EXISTS player_sectors (
            player_id  INTEGER REFERENCES players(id),
            sector_id  INTEGER REFERENCES sectors(id),
            confidence INTEGER NOT NULL DEFAULT 100,
            PRIMARY KEY (player_id, sector_id)
        );
        CREATE TABLE IF NOT EXISTS npc_profiles (
            player_id     INTEGER PRIMARY KEY REFERENCES players(id),
            strategy_name TEXT NOT NULL,
            config        TEXT NOT NULL DEFAULT '{}',
            memory        TEXT NOT NULL DEFAULT '{}'
        );
        INSERT OR IGNORE INTO sectors (id, coord_x, coord_y, coord_z, energy_capacity)
        VALUES (-1, -1, -1, -1, 0);
        CREATE TABLE IF NOT EXISTS arrival_queue (
            arrival_turn     INTEGER NOT NULL,
            org_id           INTEGER REFERENCES organizations(id),
            -- Raw coordinates, deliberately NOT a sector FK: sectors are
            -- lazily instantiated (see db/sectors.py's reveal_sector), so a
            -- move's destination usually has no sectors row at the moment the
            -- move is confirmed -- it only gets one on arrival.
            dest_x           INTEGER NOT NULL,
            dest_y           INTEGER NOT NULL,
            dest_z           INTEGER NOT NULL,
            origin_sector_id INTEGER REFERENCES sectors(id),
            PRIMARY KEY (arrival_turn, org_id)
        );
        -- Ship's log: one-shot deferred commands attached to an org,
        -- resolved by engine/turn.py's dispatch_due_commands() at the same
        -- point arrivals resolve. Four fixed trigger primitives
        -- upon_departure fires the instant the
        -- in-transit flag is applied (event-triggered, dispatched instead
        -- from engine/movement.apply_confirm_move, not the resolve_turn
        -- sweep); upon_arrival fires the same tick the in-transit flag is
        -- at_turn fires at a caller-specified absolute turn, independent of
        -- any move. resolve_turn is NULL for upon_departure (event-hooked,
        -- removed; resolve_turn is NULL for upon_departure (event-hooked,
        -- not turn-hooked); for upon_arrival it's computed
        -- once at queue time from the org's current arrival_turn; for
        -- at_turn it's exactly the caller-supplied turn number.
        CREATE TABLE IF NOT EXISTS org_command_queue (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id        INTEGER REFERENCES organizations(id),
            trigger_phase TEXT NOT NULL
                          CHECK(trigger_phase IN ('upon_departure','upon_arrival','at_turn')),
            resolve_turn  INTEGER,
            action        TEXT NOT NULL,
            params        TEXT NOT NULL DEFAULT '{}',
            created_turn  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_org_command_queue_resolve ON org_command_queue(resolve_turn);
        CREATE TABLE IF NOT EXISTS events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id         INTEGER,
            turn            INTEGER NOT NULL,
            seq             INTEGER NOT NULL,
            ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            event_type      TEXT NOT NULL,
            actor_id        INTEGER REFERENCES players(id),
            subject_id      INTEGER,
            subject_type    TEXT,
            resolve_at_turn INTEGER,
            payload         TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_events_turn    ON events(turn, seq);
        CREATE INDEX IF NOT EXISTS idx_events_actor   ON events(actor_id);
        CREATE INDEX IF NOT EXISTS idx_events_subject ON events(subject_type, subject_id);
        CREATE INDEX IF NOT EXISTS idx_events_type    ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_events_resolve ON events(event_type, resolve_at_turn);
    """)
    _add_missing_columns(cur)
    conn.commit()
    conn.close()


# Columns added to a table that already exists on a deployed volume. CREATE
# TABLE IF NOT EXISTS above does nothing for those -- it sees the table and
# stops -- so a column added later never reaches an existing database.
#
# This is NOT the general migration mechanism that was deliberately removed
# (see docs/dev_history.md): there is no version table, no ordering, and no
# data rewriting. It adds a nullable column if it is absent, which is
# idempotent and safe to run on every boot.
#
# It exists because the alternative is worse than the machinery: the deployed
# volume holds a FINISHED GameHouse game, which has both a session token and
# recorded final scores, so the results reporter reaches straight past its
# guards and into a query for a column that isn't there -- logging a failure
# every poll, forever.
ADDED_COLUMNS = {
    "players": {"gamehouse_person_id": "INTEGER"},
    "organizations": {"task_force_id": "INTEGER REFERENCES task_forces(id)"},
}


def _add_missing_columns(cur):
    for table, columns in ADDED_COLUMNS.items():
        existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in columns.items():
            if name not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                print(f"  Added column {table}.{name}")
