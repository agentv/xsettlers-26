#!/usr/bin/env python
"""
Round-robin tournament across every registered NPC strategy (engine/npc.py's
STRATEGIES), each pair playing once via scripts/simulate_npc_matchup.py in its
own scratch DB (tourney/dbs/) and its own subprocess. This orchestrator never
opens a DB connection itself -- each matchup's subprocess dumps its own
turn-by-turn + standings JSON (tourney/data/) via that script's --json-out,
since repeatedly loading the SpatiaLite extension across separate
connections in one long-running process was observed to segfault on the
second matchup. Charting/analysis happens downstream of this data, not here.

Usage:
    .venv/bin/python scripts/run_tournament.py --scenario config/game0.yaml
"""
import argparse
import itertools
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOURNEY_DIR = f"{REPO_ROOT}/tourney"

def _run_matchup(scenario: str, s1: str, s2: str, turns: int | None) -> dict:
    pair_name = f"{s1}_vs_{s2}"
    db_path = f"{TOURNEY_DIR}/dbs/{pair_name}.db"
    log_path = f"{TOURNEY_DIR}/logs/{pair_name}.log"
    json_path = f"{TOURNEY_DIR}/data/{pair_name}.json"

    cmd = [sys.executable, "scripts/simulate_npc_matchup.py",
           "--scenario", scenario, "--strategy", s1, s2,
           "--db", db_path, "--json-out", json_path]
    if turns:
        cmd += ["--turns", str(turns)]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    with open(log_path, "w") as f:
        f.write(result.stdout)
        if result.returncode != 0:
            f.write("\n--- STDERR ---\n")
            f.write(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"{pair_name} failed (see {log_path}):\n{result.stderr[-2000:]}")

    with open(json_path) as f:
        return json.load(f)

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="config/game0.yaml")
    parser.add_argument("--turns", type=int, default=None)
    args = parser.parse_args()

    sys.path.insert(0, REPO_ROOT)
    from engine.npc import STRATEGIES
    strategies = sorted(STRATEGIES)
    pairs = list(itertools.combinations(strategies, 2))
    print(f"Round robin: {len(strategies)} strategies, {len(pairs)} matchups.")

    for d in ("dbs", "logs", "data"):
        os.makedirs(f"{TOURNEY_DIR}/{d}", exist_ok=True)

    summary = {"scenario": args.scenario, "strategies": strategies, "matchups": []}
    for s1, s2 in pairs:
        print(f"  Running {s1} vs {s2} ...")
        game = _run_matchup(args.scenario, s1, s2, args.turns)
        winner = max(game["standings"], key=lambda s: s["score"]) if game["standings"] else None
        summary["matchups"].append({
            "matchup": f"{s1}_vs_{s2}", "strategy_1": s1, "strategy_2": s2,
            "standings": game["standings"],
            "winner": winner["strategy"] if winner else None,
        })
        print(f"    winner: {winner['strategy'] if winner else 'n/a'} "
              f"({winner['score'] if winner else '?'} pts)")

    with open(f"{TOURNEY_DIR}/summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nWrote {len(pairs)} matchup files to {TOURNEY_DIR}/data/, summary to {TOURNEY_DIR}/summary.json")

if __name__ == "__main__":
    main()
