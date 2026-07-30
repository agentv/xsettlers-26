DEFAULT_SECTOR_RESOURCE_UNITS = 1000.0  # flat for now; TODO: randomize per-sector later


def reveal_sector(cur, player_id: int, coord_x: int, coord_y: int, coord_z: int) -> int:
    """
    Get-or-create the sector at (coord_x,coord_y,coord_z) and mark it visible
    to player_id at confidence=100. The single entry point for the lazy-reveal
    model (docs/data_model_and_storage_design.md): a sector row only exists
    once bootstrap placement, ship arrival, or a resolved scan reveals it.

    cur is an open cursor on the caller's connection/transaction -- this
    function does not commit; callers (bootstrap_game(), end_of_turn())
    commit as part of their own transaction.

    An already-revealed sector's resource capacities are left untouched --
    only the first reveal sets them. Returns the sector's id either way.
    """
    cur.execute("SELECT id FROM sectors WHERE coord_x=? AND coord_y=? AND coord_z=?",
                (coord_x, coord_y, coord_z))
    row = cur.fetchone()
    if row:
        sector_id = row["id"]
    else:
        cur.execute("""INSERT INTO sectors
            (coord_x,coord_y,coord_z,energy_capacity,food_capacity,goods_capacity)
            VALUES (?,?,?,?,?,?)""",
            (coord_x, coord_y, coord_z, DEFAULT_SECTOR_RESOURCE_UNITS,
             DEFAULT_SECTOR_RESOURCE_UNITS, DEFAULT_SECTOR_RESOURCE_UNITS))
        sector_id = cur.lastrowid
        cur.execute("UPDATE sectors SET location=MakePointZ(?,?,?,-1) WHERE id=?",
                    (coord_x, coord_y, coord_z, sector_id))
    cur.execute("""INSERT INTO player_sectors (player_id,sector_id,confidence) VALUES (?,?,100)
        ON CONFLICT(player_id,sector_id) DO UPDATE SET confidence=100""",
        (player_id, sector_id))
    return sector_id
