#!/usr/bin/env python
"""
Fixed-format status CLI. No discretionary formatting -- same code path,
same output shape, every time. A thin wrapper around the existing tool
functions (xsettlers_mcp/tools/organization_reports.py) and the generic
hint-driven renderer (views/render.py's render_status()); adds nothing of
its own beyond argument parsing and a turn-context header line.

Usage:
    .venv/bin/python scripts/status.py game
    .venv/bin/python scripts/status.py fleet
    .venv/bin/python scripts/status.py fleet --token <player_token>

Defaults --token to $XSETTLERS_PLAYER_TOKEN, falling back to Vincent's
dev-roster token in config/game_config.yaml.
"""
import argparse
import os
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.clock import is_frozen
from views.render import render_status
from xsettlers_mcp.tools.organization_reports import show_game_status, show_civilization_status

DEFAULT_TOKEN = os.environ.get("XSETTLERS_PLAYER_TOKEN", "REPLACE_WITH_GENERATED_TOKEN_1")
HEALTH_URL = f"http://localhost:{os.environ.get('PORT', 8080)}/health"

REPORTS = {
    "game": show_game_status,
    "fleet": show_civilization_status,
}

def _server_is_up() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False

def _clock_status(next_tick_at: str | None) -> str:
    """
    "clock paused" whenever the server process isn't actually reachable --
    a paused clock (server killed) leaves next_tick_at stale with nothing
    refreshing it, so trusting the timestamp alone would misreport an old
    countdown as if it were live (see engine.turn.get_next_tick_at's
    docstring). Only compute a real countdown once the server's confirmed up.

    Distinct from "clock frozen": that's a deliberate hold set via
    scripts/clock.py while the server keeps running (see engine/clock.py's
    game_state.frozen flag) -- checked first since a frozen clock is still a
    live, reachable server, just intentionally not ticking.
    """
    if is_frozen():
        return "clock frozen (testing)"
    if not _server_is_up() or not next_tick_at:
        return "clock paused"
    next_dt = datetime.strptime(next_tick_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    remaining = max(0, round((next_dt - datetime.now(timezone.utc)).total_seconds()))
    minutes, seconds = divmod(remaining, 60)
    return f"{minutes}m {seconds}s until next tick"

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", choices=sorted(REPORTS), help="Which report to run.")
    parser.add_argument("--token", default=DEFAULT_TOKEN,
                        help="player_token (default: $XSETTLERS_PLAYER_TOKEN or Vincent's dev token)")
    args = parser.parse_args()

    data = REPORTS[args.report](args.token)
    if "error" in data:
        print(f"Error: {data['error']}", file=sys.stderr)
        sys.exit(1)

    clock = _clock_status(data.get("next_tick_at"))
    print(f"Turn {data['turn']} of {data['turn_limit']} — {clock}")
    print()
    print(render_status(data))

if __name__ == "__main__":
    main()
