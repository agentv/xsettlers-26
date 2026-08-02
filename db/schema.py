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
    # One-time migration: organizations gain innate scan aiming (2026-07-31).
    # Nullable ADD COLUMNs -- organizations holds live game state that must
    # never be dropped, unlike the empty-table drops above.
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='organizations'")
    if cur.fetchone()[0] > 0:
        cols = {row[1] for row in cur.execute("PRAGMA table_info(organizations)").fetchall()}
        for col in ("scan_offset_x", "scan_offset_y", "scan_offset_z"):
            if col not in cols:
                cur.execute(f"ALTER TABLE organizations ADD COLUMN {col} INTEGER")
        # Absolute scan_target_* -> relative scan_offset_* (same day, 2026-07-31).
        # Converted rather than renamed: the numbers mean something different
        # now. offset = target - the org's current position, which is exactly
        # what the old absolute target was aiming at from where the org stands.
        # An org in transit has no position to subtract from, so its aim is
        # cleared rather than guessed at.
        if "scan_target_x" in cols:
            cur.execute("""
                UPDATE organizations SET
                    scan_offset_x = scan_target_x - (SELECT s.coord_x FROM sectors s WHERE s.id = organizations.sector_id),
                    scan_offset_y = scan_target_y - (SELECT s.coord_y FROM sectors s WHERE s.id = organizations.sector_id),
                    scan_offset_z = scan_target_z - (SELECT s.coord_z FROM sectors s WHERE s.id = organizations.sector_id)
                WHERE scan_target_x IS NOT NULL AND sector_id != -1""")
            for col in ("scan_target_x", "scan_target_y", "scan_target_z"):
                cur.execute(f"ALTER TABLE organizations DROP COLUMN {col}")
    # One-time migration: pod scan targets were absolute coordinates in
    # task_params JSON; they are offsets now (2026-07-31). Same conversion,
    # done in Python because it is JSON rather than columns.
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='pods'")
    if cur.fetchone()[0] > 0:
        import json as _json
        rows = cur.execute("""SELECT p.id, p.task_params, s.coord_x, s.coord_y, s.coord_z
            FROM pods p JOIN organizations o ON o.id = p.org_id
            LEFT JOIN sectors s ON s.id = o.sector_id
            WHERE p.task_params IS NOT NULL""").fetchall()
        for row in rows:
            try:
                params = _json.loads(row["task_params"] or "{}")
            except ValueError:
                continue
            if "target_x" not in params:
                continue
            if row["coord_x"] is None:
                cur.execute("UPDATE pods SET task_params=NULL WHERE id=?", (row["id"],))
                continue
            cur.execute("UPDATE pods SET task_params=? WHERE id=?", (_json.dumps({
                "offset_x": params["target_x"] - row["coord_x"],
                "offset_y": params["target_y"] - row["coord_y"],
                "offset_z": params["target_z"] - row["coord_z"],
            }), row["id"]))
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
    # One-time migration: drop sectors.food_capacity/goods_capacity (2026-08-02).
    # Only energy is drawn from the map -- food and goods are manufactured out
    # of resources already held (see engine/production.py's
    # RESOURCE_CAPACITY_COLUMN, which has only ever mapped energy). These two
    # columns were seeded to the same 1000 as energy on every reveal and then
    # never read or decremented by anything, so they were not merely unused:
    # show_sector_neighborhood displayed them, telling players a sector held
    # 1000 food and 1000 goods that could not be harvested from it. Dropping
    # them removes the lie at the source rather than hiding it in the view.
    # No data is lost that anything ever consulted.
    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sectors'")
    if cur.fetchone()[0] > 0:
        cols = {row[1] for row in cur.execute("PRAGMA table_info(sectors)").fetchall()}
        for col in ("food_capacity", "goods_capacity"):
            if col in cols:
                cur.execute(f"ALTER TABLE sectors DROP COLUMN {col}")
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
            -- RESOURCE_CAPACITY_COLUMN). food_capacity/goods_capacity were
            -- dropped 2026-08-02.
            energy_capacity REAL DEFAULT 0
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
            mission_params TEXT,
            -- Innate scan: every organization can scan one sector per turn on
            -- its own account -- a ship's bridge, a colony's headquarters --
            -- without spending a pod on it (added 2026-07-31). Aimed by an
            -- OFFSET from the org's own sector, not absolute coordinates, so
            -- the aim survives a move (see sector_tools.SCAN_BEARINGS).
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
        INSERT OR IGNORE INTO sectors (id, coord_x, coord_y, coord_z, energy_capacity)
        VALUES (-1, -1, -1, -1, 0);
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
