import os
from db.connection import get_connection
from db.archive import archive_active_database

def test_archive_moves_the_file_and_leaves_a_working_schema_behind():
    db_path = os.environ["DB_PATH"]
    conn = get_connection()
    conn.execute("INSERT INTO players (email,display_name,player_token) "
                 "VALUES ('a@test.com','A','tok-a')")
    conn.commit(); conn.close()

    archived = archive_active_database()

    assert archived is not None
    assert archived.startswith(db_path + ".finished-")
    assert os.path.exists(archived)
    assert os.path.exists(db_path), "a fresh DB must exist at the live path immediately"

    # The archived copy kept the data; the live path is a clean slate.
    old_conn = get_connection()  # DB_PATH now points at the fresh file
    assert old_conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"] == 0
    old_conn.close()

    import sqlite3
    archived_conn = sqlite3.connect(archived)
    archived_conn.row_factory = sqlite3.Row
    assert archived_conn.execute(
        "SELECT COUNT(*) AS n FROM players").fetchone()["n"] == 1
    archived_conn.close()

def test_archive_is_a_noop_when_there_is_nothing_to_archive():
    db_path = os.environ["DB_PATH"]
    os.remove(db_path)
    assert archive_active_database() is None

def test_archived_live_path_accepts_a_new_bootstrap_without_a_restart():
    """The point of reinitializing in place: a running process can hand off
    a new game right after archiving, with no process restart in between."""
    from db.bootstrap import bootstrap_game
    archive_active_database()
    # bootstrap_game() assumes init_schema() already ran -- exactly what
    # archive_active_database() just did against the fresh DB_PATH.
    bootstrap_game(scenario_file="config/game_solo.yaml", scenario_name="game_solo")
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
    conn.close()
    assert n >= 1
