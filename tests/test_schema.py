from db.connection import connection, get_connection
from db.schema import init_schema

def test_tables_created():
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {r[0] for r in cur.fetchall()}; conn.close()
    for t in ("players","sectors","organizations","pods","player_sectors","arrival_queue","events",
              "npc_profiles"):
        assert t in tables

def test_sentinel_sector_exists():
    """The sentinel sector (id=-1, representing "in transit") is created by
    init_schema() itself, not by bootstrap_game() -- which is why it is
    tested here and not in test_bootstrap.py."""
    conn = get_connection()
    sentinel = conn.execute("SELECT id FROM sectors WHERE id=-1").fetchone()
    assert sentinel is not None
    conn.close()


def test_init_schema_drops_spatialite_triggers_left_on_a_deployed_volume():
    """
    A volume created before SpatiaLite was dropped still carries the triggers
    AddGeometryColumn installed. Two of them call GeometryConstraints(), which
    no longer exists, so they fail every INSERT into sectors -- including the
    sentinel row init_schema() itself writes, which makes it a crash on boot.
    """
    with connection() as conn:
        conn.execute("""CREATE TRIGGER ggi_sectors_location BEFORE INSERT ON sectors
                        FOR EACH ROW BEGIN
                          SELECT RAISE(ROLLBACK, 'geometry constraint');
                        END""")
        assert conn.execute("""SELECT count(*) FROM sqlite_master
            WHERE type='trigger' AND tbl_name='sectors'""").fetchone()[0] == 1

    init_schema()

    with connection() as conn:
        assert conn.execute("""SELECT count(*) FROM sqlite_master
            WHERE type='trigger' AND tbl_name='sectors'""").fetchone()[0] == 0
        conn.execute("""INSERT INTO sectors (coord_x,coord_y,coord_z,energy_capacity)
                        VALUES (9,9,9,500.0)""")
