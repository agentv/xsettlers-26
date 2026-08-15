from db.connection import get_connection

def init_schema():
    """Create all tables and spatial metadata. Safe to run on existing DB."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='spatial_ref_sys'")
    if cur.fetchone()[0] == 0:
        cur.execute("SELECT InitSpatialMetaData(1)")
    # No migration step: the CREATE TABLE statements below are the whole
    # schema. See docs/dev_history.md if a database older than the current
    # shape ever turns up.
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            email             TEXT NOT NULL UNIQUE,
            display_name      TEXT NOT NULL,
            player_token      TEXT UNIQUE,
            end_turn_declared INTEGER DEFAULT 0,
            is_npc            INTEGER DEFAULT 0
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
            -- Innate scan: every organization can scan one sector per turn on
            -- its own account -- a ship's bridge, a colony's headquarters --
            -- without spending a pod on it. Aimed by an OFFSET from the
            -- org's own sector, not absolute coordinates, so the aim
            -- survives a move (see sector_tools.SCAN_BEARINGS).
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
        -- (see docs/TODO.md) -- during_transit fires the instant the
        -- in-transit flag is applied (event-triggered, dispatched instead
        -- from engine/movement.apply_confirm_move, not the resolve_turn
        -- sweep); before_arrival fires the same tick the in-transit flag is
        -- removed; after_arrival fires exactly one end_of_turn() pass later;
        -- at_turn fires at a caller-specified absolute turn, independent of
        -- any move. resolve_turn is NULL for during_transit (event-hooked,
        -- not turn-hooked); for before_arrival/after_arrival it's computed
        -- once at queue time from the org's current arrival_turn; for
        -- at_turn it's exactly the caller-supplied turn number.
        CREATE TABLE IF NOT EXISTS org_command_queue (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id        INTEGER REFERENCES organizations(id),
            trigger_phase TEXT NOT NULL
                          CHECK(trigger_phase IN ('during_transit','before_arrival','after_arrival','at_turn')),
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
    cur.execute("""SELECT COUNT(*) FROM geometry_columns
                   WHERE f_table_name='sectors' AND f_geometry_column='location'""")
    if cur.fetchone()[0] == 0:
        cur.execute("SELECT AddGeometryColumn('sectors','location',-1,'POINTZ','XYZ')")
    conn.commit()
    conn.close()
