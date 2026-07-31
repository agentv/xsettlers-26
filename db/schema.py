from db.connection import get_connection

def init_schema():
    """Create all tables and spatial metadata. Safe to run on existing DB."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='spatial_ref_sys'")
    if cur.fetchone()[0] == 0:
        cur.execute("SELECT InitSpatialMetaData(1)")
    # One-time migration: players.slack_user_id -> player_token (2026-07-22,
    # client-agnostic auth generalization). CREATE TABLE IF NOT EXISTS below
    # is a no-op against an existing table regardless of column names, so a
    # deployed DB that already has the old column needs an explicit rename.
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='players'")
    if cur.fetchone()[0] > 0:
        cols = {row[1] for row in cur.execute("PRAGMA table_info(players)").fetchall()}
        if "slack_user_id" in cols and "player_token" not in cols:
            cur.execute("ALTER TABLE players RENAME COLUMN slack_user_id TO player_token")
    # One-time migration: arrival_queue.dest_sector_id -> dest_x/dest_y/dest_z
    # (lazy sector reveal -- a move's destination may not exist as a sectors
    # row yet when confirmed, only once revealed on arrival, so it can no
    # longer be an FK). CREATE TABLE IF NOT EXISTS below is a no-op against
    # an existing table regardless of column definitions, so a deployed DB
    # that already created arrival_queue with the old column needs explicit
    # handling. arrival_queue is only ever populated by confirm_move(), which
    # requires an already-bootstrapped game; any deployment still on the old
    # schema has never bootstrapped one, so the table is guaranteed empty --
    # drop-and-recreate is safe, no data to migrate.
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='arrival_queue'")
    if cur.fetchone()[0] > 0:
        cols = {row[1] for row in cur.execute("PRAGMA table_info(arrival_queue)").fetchall()}
        if "dest_sector_id" in cols and "dest_x" not in cols:
            cur.execute("DROP TABLE arrival_queue")
    # One-time migration: pods.storage_current (implicitly typed by the pod's
    # current mission) -> energy_stored/food_stored/goods_stored (explicit,
    # independent of current mission -- retasking a pod no longer hides
    # whatever it already has stored). Also drops the long-unused
    # energy_consumption/food_consumption columns, superseded by
    # engine/production.py's POD_CONSUMPTION_RECIPE. pods is only ever
    # populated by bootstrap_game(), which requires scenario selection; any
    # deployment still on the old schema has never bootstrapped a game, so
    # the table is guaranteed empty -- drop-and-recreate is safe.
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='pods'")
    if cur.fetchone()[0] > 0:
        cols = {row[1] for row in cur.execute("PRAGMA table_info(pods)").fetchall()}
        if "storage_current" in cols and "energy_stored" not in cols:
            cur.execute("DROP TABLE pods")
    # One-time migration: pods.mission -> pods.task (2026-07-31). `mission`
    # meant two different things depending on which table you were reading:
    # on organizations it is what the vehicle is doing (idle/move/colonize/
    # defend/attack), on pods what the worker is doing (produce_*/scan/idle).
    # One word, two concepts, and the renderer already called the pod one
    # "task" -- so a player reading a ship report saw "mission: idle" above a
    # column headed "task" and reasonably concluded the pods were idle too.
    # Unlike the drops above this preserves data: a live game has pods with
    # real task values, and RENAME COLUMN also rewrites the CHECK constraint
    # that references the column.
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='pods'")
    if cur.fetchone()[0] > 0:
        cols = {row[1] for row in cur.execute("PRAGMA table_info(pods)").fetchall()}
        if "mission" in cols and "task" not in cols:
            cur.execute("ALTER TABLE pods RENAME COLUMN mission TO task")
        if "mission_params" in cols and "task_params" not in cols:
            cur.execute("ALTER TABLE pods RENAME COLUMN mission_params TO task_params")
    # One-time migration: game_state gains next_tick_at (2026-07-30) -- lets
    # a status report show "time until next tick" without needing to ask the
    # live server process directly. Nullable, ADD COLUMN is safe on an
    # existing single-row table (no data loss) -- unlike the table drops
    # above, game_state.current_turn must never be lost.
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='game_state'")
    if cur.fetchone()[0] > 0:
        cols = {row[1] for row in cur.execute("PRAGMA table_info(game_state)").fetchall()}
        if "next_tick_at" not in cols:
            cur.execute("ALTER TABLE game_state ADD COLUMN next_tick_at TEXT")
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
            energy_capacity REAL DEFAULT 0,
            food_capacity   REAL DEFAULT 0,
            goods_capacity  REAL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sector_coords ON sectors(coord_x, coord_y, coord_z);
        CREATE TABLE IF NOT EXISTS game_state (
            id            INTEGER PRIMARY KEY DEFAULT 1,
            current_turn  INTEGER DEFAULT 0,
            next_tick_at  TEXT,
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
        CREATE TABLE IF NOT EXISTS organizations (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            org_type       TEXT CHECK(org_type IN ('ship','colony')),
            name           TEXT,
            player_id      INTEGER REFERENCES players(id),
            sector_id      INTEGER REFERENCES sectors(id),
            is_mobile      INTEGER DEFAULT 1,
            mission        TEXT DEFAULT 'idle'
                           CHECK(mission IN ('idle','move','colonize','defend','attack')),
            mission_params TEXT
        );
        CREATE TABLE IF NOT EXISTS pods (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id           INTEGER REFERENCES organizations(id),
            -- `task` is what this pod's crew does; the parent organization's
            -- `mission` is what the vehicle does. Deliberately different
            -- words for deliberately different concepts (renamed 2026-07-31).
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
        INSERT OR IGNORE INTO sectors (id, coord_x, coord_y, coord_z,
                                       energy_capacity, food_capacity, goods_capacity)
        VALUES (-1, -1, -1, -1, 0, 0, 0);
        CREATE TABLE IF NOT EXISTS arrival_queue (
            arrival_turn     INTEGER NOT NULL,
            org_id           INTEGER REFERENCES organizations(id),
            dest_x           INTEGER NOT NULL,
            dest_y           INTEGER NOT NULL,
            dest_z           INTEGER NOT NULL,
            origin_sector_id INTEGER REFERENCES sectors(id),
            PRIMARY KEY (arrival_turn, org_id)
        );
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
