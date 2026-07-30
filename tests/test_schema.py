from db.connection import get_connection

def test_tables_created():
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {r[0] for r in cur.fetchall()}; conn.close()
    for t in ("players","sectors","organizations","pods","player_sectors","arrival_queue","events",
              "npc_profiles"):
        assert t in tables

def test_sentinel_sector_exists():
    """The sentinel sector (id=-1, representing "in transit") is created by
    init_schema() itself, not by bootstrap_game() -- relocated here from
    test_bootstrap.py per the 2026-07-29 test-suite audit, since it doesn't
    call _bootstrap() and was testing db/schema.py, not db/bootstrap.py."""
    conn = get_connection()
    sentinel = conn.execute("SELECT id FROM sectors WHERE id=-1").fetchone()
    assert sentinel is not None
    conn.close()
