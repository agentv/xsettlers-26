"""
What an org can be ordered to do, named once.

The same four actions are bound twice, deliberately, because they are invoked
from two places that cannot share an implementation:

* **queued** (`engine/ship_log.py`'s ACTIONS) runs inside engine/turn.py's
  already-open transaction, so it dispatches into the engine-layer `apply_*`
  helpers. A self-connecting call there fails outright -- db/connection.py sets
  no busy_timeout, so a second writer errors rather than waiting.
* **immediate** (`npc/strategy.py`'s IMMEDIATE) runs before the turn
  transaction opens, so it goes through the ordinary @player_tool wrappers --
  the same path a human player's MCP call takes, ownership checks and all.

This module holds no handlers, only the vocabulary, which is what lets both
bindings and queue_command's whitelist agree without any of them importing the
others (the tool layer imports the ship's log, so a registry holding tool
functions would close a cycle). tests/test_actions.py asserts both bindings
cover exactly these names, so a fifth action cannot be half-added.
"""

ACTION_NAMES = frozenset({"move", "set_pod_task", "colonize", "aim_scan"})

# Entering transit locks an org's own mission entirely, but not its pods'
# tasks -- which is the whole reason the upon_departure phase exists, and why
# it is the one phase with a narrower whitelist.
UPON_DEPARTURE_ACTIONS = frozenset({"set_pod_task"})

# Three moments, and they are the three a player actually names: getting
# under way, landing, or a turn they pick themselves. An "one turn after
# landing" phase was removed -- a caller who wants that knows the arrival
# turn and can say at_turn, and every extra primitive is another word the
# grammar has to teach.
TRIGGER_PHASES = frozenset({"upon_departure", "upon_arrival", "at_turn"})
