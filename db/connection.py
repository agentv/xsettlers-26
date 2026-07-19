import sqlite3, os
from dotenv import load_dotenv
load_dotenv()
DB_PATH = os.getenv("DB_PATH", "xsettlers.db")

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    conn.load_extension("mod_spatialite")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
