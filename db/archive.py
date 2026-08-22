# Moves a finished game's live database file out of the way and
# reinitializes an empty one at DB_PATH in its place, so a running server can
# accept a new game (select_scenario/start_session) without a process
# restart. Deciding WHEN it's safe to do this is not this module's job -- see
# xsettlers_mcp/gamehouse.py's _game_settled(), which is the only caller.

import os
from datetime import datetime, timezone
from db.schema import init_schema

ARCHIVE_TIMESTAMP_FMT = "%Y%m%d-%H%M%S"

def archive_active_database() -> str | None:
    """
    Renames DB_PATH to '<DB_PATH>.finished-<UTC timestamp>' and reinitializes
    an empty schema at DB_PATH. Returns the archive path, or None if DB_PATH
    doesn't currently exist (nothing to archive -- also makes this safe to
    call more than once).
    """
    db_path = os.getenv("DB_PATH", "xsettlers.db")
    if not os.path.exists(db_path):
        return None
    archive_path = f"{db_path}.finished-{datetime.now(timezone.utc).strftime(ARCHIVE_TIMESTAMP_FMT)}"
    os.rename(db_path, archive_path)
    init_schema()
    return archive_path
