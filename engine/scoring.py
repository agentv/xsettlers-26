"""
What a player's score is, and what the standings are -- defined once.

Three call sites need this and must agree: the live scoreboard
(xsettlers_mcp/tools/organization_reports.show_game_status), the per-turn
ledger (engine/turn._snapshot_holdings), and the game-over result
(engine/turn._calculate_final_scores). Sharing one implementation is what
makes that true -- with a copy each, a weights change applied to two of the
three would show a player one standing mid-game and crown a different winner
at the whistle.

Deliberately a leaf module: it imports nothing from engine/, db/, or
xsettlers_mcp/, and takes an open cursor plus an already-loaded weights dict
rather than reaching for a connection or config itself. That keeps it
importable from both the engine and the tool layer with no risk of joining
the circular-import tangle those two already navigate carefully (see
db/events.py and engine/turn.py's lazy imports).
"""

# The resources that carry score. Energy is currently weighted 0 (it's a means
# of production, not a scored asset -- see config/game_config.yaml), but it
# stays in the tuple rather than being dropped: a weight of 0 is a tuning
# decision, and hardcoding that decision into the shape of the calculation is
# how you get a scoring bug the day someone raises it above 0.
SCORED_RESOURCES = ("energy", "food", "goods")


def score_for(holdings: dict, weights: dict) -> float:
    """
    A player's weighted score from what they hold: each resource's stored
    amount times its configured weight, summed.

    `holdings` may carry extra keys (the ledger passes a dict that also holds
    `total`); only SCORED_RESOURCES are read. A resource missing from either
    `holdings` or `weights` contributes 0 rather than raising -- an unweighted
    resource is worth nothing, which is exactly what a missing weight means.
    """
    return sum(holdings.get(resource, 0) * weights.get(resource, 0)
               for resource in SCORED_RESOURCES)


def player_standings(cur, weights: dict) -> list:
    """
    Every player's aggregate holdings, scored and ranked highest-first.

    One row per player -- including players holding nothing at all, via the
    LEFT JOINs, so a wiped-out player still appears in the standings rather
    than vanishing from the scoreboard. Sums run across all of a player's
    pods regardless of which org carries them or what task each pod is on
    (storage is generic per pod -- see engine/production.RESOURCE_STORAGE_COLUMN).

    Values are returned unrounded. Presentation is the caller's business:
    show_game_status rounds to 2dp and adds a utilization percentage,
    _calculate_final_scores persists the raw figures. Rounding here would
    quietly change what gets written into the permanent game.final_scores
    event.
    """
    cur.execute("""
        SELECT p.id AS player_id, p.display_name,
               SUM(pods.energy_stored) AS energy,
               SUM(pods.food_stored) AS food,
               SUM(pods.goods_stored) AS goods,
               SUM(pods.storage_capacity) AS capacity
        FROM players p
        LEFT JOIN organizations o ON o.player_id = p.id
        LEFT JOIN pods ON pods.org_id = o.id
        GROUP BY p.id""")
    standings = []
    for row in cur.fetchall():
        holdings = {resource: row[resource] or 0 for resource in SCORED_RESOURCES}
        standings.append({
            "player_id": row["player_id"],
            "display_name": row["display_name"],
            "score": score_for(holdings, weights),
            **holdings,
            "total": sum(holdings.values()),
            "capacity": row["capacity"] or 0,
        })
    standings.sort(key=lambda s: s["score"], reverse=True)
    for rank, entry in enumerate(standings, start=1):
        entry["rank"] = rank
    return standings
