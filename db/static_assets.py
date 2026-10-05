"""
Memoized renderer output, keyed by the data it was drawn from.

The key is a digest of the input dict, not a turn number or a revision
counter. A renderer's output is a pure function of its input, so an identical
input can reuse the stored body and a changed input cannot match. That makes
invalidation exact without anything having to enumerate the writes that
change an organization or a neighborhood: a mission set mid-turn changes the
org card's input, so the next request misses and renders fresh.

Entries are cleared at the tick (engine/turn.py), because the sightings and
fog-of-war inputs only change there and an entry from a prior turn is dead
weight.

The table is a cache of the renderers, never a second source of truth. Any
failure to read or write it falls back to rendering live.
"""
import hashlib
import json
import sqlite3

from db.connection import connection, read_one


def _digest(asset: str, data) -> str:
    # default=str matches the server's JSON serialization, so anything that
    # would appear in the JSON half of a response is keyed the same way.
    canonical = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(f"{asset}\0{canonical}".encode()).hexdigest()


def get_or_render(asset: str, data, render) -> str:
    """
    The stored body for `data` if one exists, otherwise `render(data)`,
    stored for next time.

    The render runs outside any connection so a slow layout never holds a
    write lock. The store is written in its own short connection, and a
    locked or missing table costs only the memo, not the response.
    """
    digest = _digest(asset, data)
    try:
        row = read_one("SELECT body FROM static_assets WHERE digest=?", (digest,))
    except sqlite3.OperationalError:
        return render(data)
    if row:
        return row[0]

    body = render(data)
    try:
        with connection() as conn:
            conn.execute("INSERT OR REPLACE INTO static_assets (digest, asset, body) "
                         "VALUES (?, ?, ?)", (digest, asset, body))
    except sqlite3.OperationalError:
        pass
    return body
