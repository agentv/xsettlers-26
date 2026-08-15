"""
The compass: how far a scanner reaches and which offsets have names.

A leaf module with no imports of its own. Both the engine (turn resolution,
pod tasking, NPC policies) and the tool layer need this vocabulary, so it sits
below both -- the tool layer may import the engine, never the reverse.
"""

# Euclidean radius a scan can reach from the scanning org's sector. 2 is the
# smallest radius where a scan behaves the way people expect: at range 1 a
# Euclidean radius reaches only the four orthogonal neighbours, since a
# diagonal is sqrt(2) ~= 1.41 > 1, which reads as broken to a player refused
# the sector "just there". Range 2 reaches 12 sectors -- the 4 orthogonal, the
# 4 diagonals (sqrt(2)), and the 4 two-out orthogonals.
SCAN_RANGE = 2


# A scan is aimed by an OFFSET from the scanner's own sector, not by absolute
# coordinates. Sensors are mounted on the thing that carries them: they look a
# fixed direction and distance from wherever it currently is, and a ship flying
# away from a sector does not keep seeing it. Two consequences fall out: a scan
# pattern survives a move with no re-aiming, and "in range" is a permanent
# property of the offset rather than something that can silently stop being
# true when the scanner moves -- so it is validated once, at set time, instead
# of failing at resolution.
#
# NORTH IS -Y. Chosen to match how the neighborhood map renders (y ascending
# downward, so north is up on screen). Arbitrary but fixed; everything
# player-facing depends on it.
#
# The 12 sectors reachable at SCAN_RANGE 2 map exactly onto these 12 names:
# 8 compass points plus 4 doubled cardinals. Every legal scan has a name and
# every name is a legal scan -- which is only true while SCAN_RANGE == 2, so
# treat this table as a convenience over `offset_x/y/z`, not as the definition
# of what is reachable.
SCAN_BEARINGS = {
    "N":  (0, -1, 0), "NE": (1, -1, 0), "E":  (1, 0, 0),  "SE": (1, 1, 0),
    "S":  (0, 1, 0),  "SW": (-1, 1, 0), "W":  (-1, 0, 0), "NW": (-1, -1, 0),
    "N2": (0, -2, 0), "E2": (2, 0, 0),  "S2": (0, 2, 0),  "W2": (-2, 0, 0),
}


def resolve_bearing(bearing: str):
    """Compass name -> (dx, dy, dz). Case- and whitespace-insensitive."""
    return SCAN_BEARINGS.get((bearing or "").strip().upper())


def bearing_name(dx: int, dy: int, dz: int):
    """(dx, dy, dz) -> compass name, or None if the offset has no short name."""
    for name, offset in SCAN_BEARINGS.items():
        if offset == (dx, dy, dz):
            return name
    return None


def get_scan_range(org_id: int) -> int:
    """
    Returns the scan range for an org.
    POC: always returns SCAN_RANGE, ignoring the org.
    Future: sum range contributions from pods on the scan task.
    """
    return SCAN_RANGE
