#!/usr/bin/env python
"""
Freeze/unfreeze the background game clock without stopping the server
process, so end_of_turn() doesn't fire unexpectedly mid-session (e.g. while
testing interactively and discussing state that a tick would change).

Talks to the same DB the running server reads (DB_PATH) -- run_clock() polls
game_state.frozen once a second (see engine/clock.py), so a freeze/unfreeze
issued here takes effect within ~1s of the live server. No restart needed.

Usage:
    .venv/bin/python scripts/clock.py freeze
    .venv/bin/python scripts/clock.py unfreeze
    .venv/bin/python scripts/clock.py status
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.clock import freeze, unfreeze, is_frozen
from engine.turn import get_current_turn, get_next_tick_at

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["freeze", "unfreeze", "status"])
    args = parser.parse_args()

    if args.action == "freeze":
        freeze()
    elif args.action == "unfreeze":
        unfreeze()

    state = "FROZEN" if is_frozen() else "running"
    print(f"Turn {get_current_turn()} — clock {state} — next_tick_at={get_next_tick_at()}")

if __name__ == "__main__":
    main()
