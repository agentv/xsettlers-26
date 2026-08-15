import sqlite3, os
from contextlib import contextmanager
from dotenv import load_dotenv
load_dotenv()

@contextmanager
def connection():
    """
    An open connection that commits on a clean exit and always closes.

    For the ordinary case: open, do a few statements, done. Code that needs
    explicit control of when it commits -- engine/turn.py's end_of_turn(),
    which flushes mid-pass so the holdings snapshot sees every mutation --
    keeps managing its own connection, since the commit point is part of what
    that code is saying.
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def read_one(sql: str, params=()):
    """One row from a query, or None. The read-only half of `connection()`,
    for the many callers whose whole body is a single SELECT."""
    with connection() as conn:
        return conn.execute(sql, params).fetchone()

def read_all(sql: str, params=()) -> list:
    """Every row from a query."""
    with connection() as conn:
        return conn.execute(sql, params).fetchall()

def read_value(sql: str, params=(), default=None):
    """The first column of the first row, or `default` if there is no row."""
    row = read_one(sql, params)
    return row[0] if row else default

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
