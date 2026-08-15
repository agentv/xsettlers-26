"""db/orgs.py -- the one place an organization's coordinates are looked up."""
from db.connection import get_connection
from db.orgs import org_position
from tests.conftest import seed_player, seed_sector, seed_ship


def test_org_position_returns_sector_and_coordinates():
    pid = seed_player(); sec = seed_sector(4, 7, 0)
    oid = seed_ship(pid, sec)
    conn = get_connection()
    pos = org_position(conn.cursor(), oid)
    conn.close()
    assert (pos["coord_x"], pos["coord_y"], pos["coord_z"]) == (4, 7, 0)
    assert pos["sector_id"] == sec
    assert pos["org_id"] == oid
    assert pos["player_id"] == pid


def test_org_position_is_none_for_org_in_transit():
    """An in-transit org is parked at the sentinel sector, which is a real row
    with coordinates (-1,-1,-1). Returning those would let a caller offset a
    scan aim or a relative move from a placeholder, so it returns None."""
    pid = seed_player(); sec = seed_sector(4, 7, 0)
    oid = seed_ship(pid, sec)
    conn = get_connection()
    conn.execute("UPDATE organizations SET sector_id=-1 WHERE id=?", (oid,))
    conn.commit()
    pos = org_position(conn.cursor(), oid)
    conn.close()
    assert pos is None


def test_org_position_is_none_for_unknown_org():
    conn = get_connection()
    pos = org_position(conn.cursor(), 999999)
    conn.close()
    assert pos is None
