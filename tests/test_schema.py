from db.connection import get_connection

def test_tables_created():
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {r[0] for r in cur.fetchall()}; conn.close()
    for t in ("players","sectors","organizations","pods","player_sectors","arrival_queue","events"):
        assert t in tables
