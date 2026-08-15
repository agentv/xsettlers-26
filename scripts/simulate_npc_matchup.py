#!/usr/bin/env python
"""
Fast-forwarded local NPC strategy matchup. Bootstraps a scratch DB (never
xsettlers.db / the production volume), assigns each of a scenario's
participants a named strategy from npc/strategies.py's STRATEGIES registry, then
calls end_of_turn() in a loop -- no real clock, no MCP server, no waiting on
GAME_TICK_SECONDS -- so a full game's worth of turns resolves in seconds.
Reporting is whatever end_of_turn() already prints (per-turn holdings, final
standings) -- this script adds only bootstrap/assignment and a header naming
which player got which strategy, not a second reporting path.

Usage:
    .venv/bin/python scripts/simulate_npc_matchup.py \\
        --scenario config/game0.yaml --strategy turtle burst_and_colonize

    .venv/bin/python scripts/simulate_npc_matchup.py \\
        --scenario config/game0.yaml --strategy fan_out fan_out \\
        --turns 30 --db /tmp/sim2.db
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="config/game0.yaml",
                        help="Scenario file to bootstrap (default: config/game0.yaml, Diaspora).")
    parser.add_argument("--strategy", nargs="+", required=True, metavar="STRATEGY_NAME",
                        help="One strategy name per scenario participant, in participant order "
                             "(see npc/strategies.py's STRATEGIES). Diaspora has 2 participants.")
    parser.add_argument("--turns", type=int, default=None,
                        help="Overrides TURN_LIMIT (default: env TURN_LIMIT or 20).")
    parser.add_argument("--db", default="/tmp/xsettlers_sim.db",
                        help="Scratch DB path -- wiped and recreated on every run.")
    parser.add_argument("--config", nargs="*", action="append", default=[],
                        metavar="KEY=VALUE",
                        help="Per-strategy config override, one --config group per strategy "
                             "(e.g. --config colonize_fraction=0.5 --config leg_distance=4). "
                             "Empty group ({}) if omitted for a given player.")
    parser.add_argument("--json-out", default=None,
                        help="Dump turn-by-turn holdings + final standings as JSON to this path, "
                             "read from the same live connection this run already holds -- avoids "
                             "a second process re-loading the SpatiaLite extension against this DB "
                             "(observed to segfault when done repeatedly across a batch of runs).")
    args = parser.parse_args()

    if os.path.exists(args.db):
        os.remove(args.db)
    os.environ["DB_PATH"] = args.db
    if args.turns is not None:
        os.environ["TURN_LIMIT"] = str(args.turns)

    from db.schema import init_schema
    from db.bootstrap import bootstrap_game
    from npc.profiles import assign_npc_profile
    from db.connection import get_connection
    from engine.turn import end_of_turn, is_game_over, TURN_LIMIT

    scenario_name = os.path.splitext(os.path.basename(args.scenario))[0]
    init_schema()
    bootstrap_game(scenario_file=args.scenario, scenario_name=scenario_name)

    conn = get_connection()
    players = conn.execute("SELECT id, display_name FROM players ORDER BY id").fetchall()
    conn.close()

    if len(players) != len(args.strategy):
        sys.exit(f"Scenario '{scenario_name}' has {len(players)} participant(s) but "
                 f"{len(args.strategy)} --strategy value(s) were given.")

    configs = args.config or [[] for _ in players]
    if len(configs) < len(players):
        configs += [[] for _ in range(len(players) - len(configs))]

    print(f"=== {scenario_name} matchup, {TURN_LIMIT} turns, db={args.db} ===")
    for player, strategy_name, config_pairs in zip(players, args.strategy, configs):
        config = {}
        for pair in config_pairs:
            key, _, value = pair.partition("=")
            try:
                value = float(value) if "." in value else int(value)
            except ValueError:
                pass
            config[key] = value
        assign_npc_profile(player["id"], strategy_name, config=config)
        print(f"  Player {player['id']} ({player['display_name']}): {strategy_name} {config or ''}")
    print()

    while not is_game_over():
        end_of_turn()

    print()
    print("=== Final standings ===")
    for player, strategy_name in zip(players, args.strategy):
        print(f"  Player {player['id']} ({player['display_name']}) ran: {strategy_name}")

    if args.json_out:
        import json
        strategy_by_player = {p["id"]: s for p, s in zip(players, args.strategy)}
        conn = get_connection()
        snapshots = conn.execute("""
            SELECT turn, subject_id AS player_id, payload FROM events
            WHERE event_type='turn.snapshot' ORDER BY turn, subject_id
        """).fetchall()
        turn_data = [{
            "turn": row["turn"], "player_id": row["player_id"],
            "strategy": strategy_by_player[row["player_id"]],
            **{k: json.loads(row["payload"])[k] for k in ("energy", "food", "goods", "total", "score")},
        } for row in snapshots]
        final = conn.execute("""
            SELECT payload FROM events WHERE event_type='game.final_scores' ORDER BY id LIMIT 1
        """).fetchone()
        conn.close()
        standings = json.loads(final["payload"])["standings"] if final else []
        for s in standings:
            s["strategy"] = strategy_by_player[s["player_id"]]

        with open(args.json_out, "w") as f:
            json.dump({
                "scenario": scenario_name,
                "players": [{"id": p["id"], "display_name": p["display_name"], "strategy": s}
                            for p, s in zip(players, args.strategy)],
                "turn_data": turn_data, "standings": standings,
            }, f, indent=2)
        print(f"\nWrote {args.json_out}")

if __name__ == "__main__":
    main()
