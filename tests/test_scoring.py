"""
engine/scoring.py -- the single definition of score and standings, shared by
show_game_status (live scoreboard), _snapshot_holdings (per-turn ledger), and
_calculate_final_scores (game-over result).

The point of these tests is less the arithmetic than the agreement: the last
test here is the one that matters, since three separate copies of this formula
agreeing by coincidence is exactly the situation the module was extracted to
end.
"""
from db.connection import connection, get_connection
from engine.scoring import score_for, player_standings, SCORED_RESOURCES
from tests.conftest import seed_player, seed_sector, seed_ship, seed_pod

WEIGHTS = {"energy": 0, "goods": 2, "food": 1}


# --- score_for --------------------------------------------------------------

def test_score_for_applies_each_resources_weight():
    assert score_for({"energy": 80, "food": 10, "goods": 10}, WEIGHTS) == 30


def test_score_for_ignores_unscored_keys():
    """The ledger passes a holdings dict that also carries `total`; counting
    it would double every score."""
    holdings = {"energy": 0, "food": 10, "goods": 0, "total": 10}
    assert score_for(holdings, WEIGHTS) == 10


def test_score_for_treats_missing_resource_or_weight_as_zero():
    """An unweighted resource is worth nothing -- which is what a missing
    weight means -- so neither side should raise."""
    assert score_for({"food": 5}, WEIGHTS) == 5
    assert score_for({"energy": 5, "food": 5, "goods": 5}, {"food": 1}) == 5
    assert score_for({}, {}) == 0


def test_energy_stays_scorable_if_its_weight_is_raised():
    """Energy is weighted 0 today, but that's tuning, not structure -- raising
    the weight must start scoring it with no code change."""
    assert "energy" in SCORED_RESOURCES
    assert score_for({"energy": 7, "food": 0, "goods": 0}, {"energy": 3}) == 21


# --- player_standings -------------------------------------------------------

def _two_players():
    p1 = seed_player(email="p1@t.com", player_token="U_P1", display_name="One")
    p2 = seed_player(email="p2@t.com", player_token="U_P2", display_name="Two")
    sid = seed_sector()
    o1 = seed_ship(p1, sid, name="P1 Ship")
    o2 = seed_ship(p2, sid, name="P2 Ship")
    seed_pod(o1, task="produce_energy", storage_capacity=100.0, storage_current=80.0)
    seed_pod(o2, task="produce_food", storage_capacity=100.0, storage_current=10.0)
    seed_pod(o2, task="produce_goods", storage_capacity=100.0, storage_current=10.0)
    return p1, p2


def test_player_standings_ranks_by_weighted_score_not_raw_total():
    """P1 holds 80 raw units to P2's 20, but all of it is energy (weight 0),
    so P2 wins. This is the whole reason score and total are separate fields."""
    p1, p2 = _two_players()
    with connection() as conn:
        standings = player_standings(conn.cursor(), WEIGHTS)
    by_player = {s["player_id"]: s for s in standings}
    assert by_player[p1]["score"] == 0
    assert by_player[p2]["score"] == 30      # 10*1 food + 10*2 goods
    assert by_player[p1]["total"] == 80      # higher raw holdings...
    assert standings[0]["player_id"] == p2   # ...but P2 ranks first
    assert [s["rank"] for s in standings] == [1, 2]


def test_tied_scores_share_a_rank_and_skip_the_number_they_consume():
    """Standard competition ranking: two players tied at the top are both
    rank 1, and the next distinct score is rank 3, not rank 2."""
    seed_player(email="a@test.com", player_token="U_A", display_name="A")
    seed_player(email="b@test.com", player_token="U_B", display_name="B")
    seed_player(email="c@test.com", player_token="U_C", display_name="C")
    conn = get_connection()
    for token, goods in (("U_A", 10.0), ("U_B", 10.0), ("U_C", 5.0)):
        pid = conn.execute("SELECT id FROM players WHERE player_token=?",
                           (token,)).fetchone()["id"]
        sid = seed_sector()
        seed_pod(seed_ship(pid, sid), task="produce_goods", storage_current=goods)
    standings = player_standings(conn.cursor(), WEIGHTS)
    conn.close()
    assert [s["display_name"] for s in standings][2] == "C"
    assert [s["rank"] for s in standings] == [1, 1, 3]


def test_player_standings_sums_across_every_org_and_pod():
    """Holdings are per player, not per org -- and storage is generic per pod,
    so a pod's task doesn't decide which column it counts toward."""
    pid = seed_player()
    sid = seed_sector()
    for _ in range(2):
        oid = seed_ship(pid, sid, name=f"Ship{_}")
        seed_pod(oid, task="produce_goods", storage_capacity=50.0, storage_current=10.0)
    with connection() as conn:
        standings = player_standings(conn.cursor(), WEIGHTS)
    assert len(standings) == 1
    assert standings[0]["goods"] == 20       # both orgs, both pods
    assert standings[0]["capacity"] == 100
    assert standings[0]["score"] == 40       # 20 goods * 2


def test_player_standings_includes_a_player_holding_nothing():
    """A player with no orgs at all must still appear -- vanishing from the
    scoreboard is not the same as scoring zero."""
    seed_player(email="empty@t.com", player_token="U_E", display_name="Empty")
    with connection() as conn:
        standings = player_standings(conn.cursor(), WEIGHTS)
    assert len(standings) == 1
    row = standings[0]
    assert row["score"] == 0 and row["total"] == 0 and row["capacity"] == 0
    assert all(row[r] == 0 for r in SCORED_RESOURCES)


def test_player_standings_returns_unrounded_values():
    """Presentation is the caller's job. Rounding here would silently change
    what gets persisted into the permanent game.final_scores event."""
    pid = seed_player()
    sid = seed_sector()
    oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_goods", storage_capacity=100.0, storage_current=1.125)
    with connection() as conn:
        standings = player_standings(conn.cursor(), WEIGHTS)
    assert standings[0]["goods"] == 1.125
    assert standings[0]["score"] == 2.25


# --- the agreement this module exists to guarantee ---------------------------

def test_all_three_consumers_report_the_same_score():
    """show_game_status (live), _snapshot_holdings (ledger) and
    _calculate_final_scores (game over) must never disagree about what a
    player scored. Before engine/scoring.py each computed it independently,
    so this could only ever be true by coincidence."""
    import json
    from engine.turn import end_of_turn, _calculate_final_scores
    from xsettlers_mcp.tools.organization_reports import show_game_status

    pid = seed_player()
    sid = seed_sector(energy=1000.0)
    oid = seed_ship(pid, sid)
    seed_pod(oid, task="produce_goods", storage_capacity=100.0, storage_current=40.0)
    seed_pod(oid, task="produce_food", storage_capacity=100.0, storage_current=40.0)
    end_of_turn()

    live = next(s for s in show_game_status("U_P1")["standings"] if s["player_id"] == pid)
    final = next(s for s in _calculate_final_scores() if s["player_id"] == pid)
    with connection() as conn:
        row = conn.execute(
            "SELECT payload FROM events WHERE event_type='turn.snapshot' AND subject_id=?",
            (pid,)).fetchone()
    ledger = json.loads(row["payload"])

    assert live["score"] == final["score"] == ledger["score"]
