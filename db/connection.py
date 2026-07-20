import sqlite3, os
from dotenv import load_dotenv
load_dotenv()

def get_connection() -> sqlite3.Connection:
    # Read DB_PATH fresh on every call, not once at import time -- tests rely
    # on monkeypatch.setenv("DB_PATH", ...) taking effect per-test, which a
    # module-level constant bound at first import would silently defeat.
    db_path = os.getenv("DB_PATH", "xsettlers.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    conn.load_extension("mod_spatialite")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
