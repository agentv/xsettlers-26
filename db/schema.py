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
            id           INTEGER PRIMARY KEY DEFAULT 1,
            current_turn INTEGER DEFAULT 0,
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
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id             INTEGER REFERENCES organizations(id),
            mission            TEXT DEFAULT 'idle'
                               CHECK(mission IN ('idle','produce_energy','produce_food','produce_goods','scan')),
            mission_params     TEXT,
            energy_consumption REAL DEFAULT 0,
            food_consumption   REAL DEFAULT 0,
            storage_capacity   REAL DEFAULT 0,
            storage_current    REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS player_sectors (
            player_id  INTEGER REFERENCES players(id),
            sector_id  INTEGER REFERENCES sectors(id),
            confidence INTEGER NOT NULL DEFAULT 100,
            PRIMARY KEY (player_id, sector_id)
        );
        INSERT OR IGNORE INTO sectors (id, coord_x, coord_y, coord_z,
                                       energy_capacity, food_capacity, goods_capacity)
        VALUES (-1, -1, -1, -1, 0, 0, 0);
        CREATE TABLE IF NOT EXISTS arrival_queue (
            arrival_turn     INTEGER NOT NULL,
            org_id           INTEGER REFERENCES organizations(id),
            dest_sector_id   INTEGER REFERENCES sectors(id),
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
