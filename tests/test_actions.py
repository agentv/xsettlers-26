"""
engine/actions.py names what an org can be ordered to do; two modules bind
those names to handlers. These tests are what stop a fifth action being added
to one binding and forgotten in the other -- the failure mode being an order a
player can queue but the engine cannot fire, or vice versa.
"""
from engine.actions import ACTION_NAMES, UPON_DEPARTURE_ACTIONS, TRIGGER_PHASES
from npc.strategy import IMMEDIATE
from engine.ship_log import ACTIONS as QUEUED
from xsettlers_mcp.tools.organization_tools import VALID_TRIGGER_PHASES


def test_queued_binding_covers_exactly_the_named_actions():
    """engine/ship_log.py: what the turn engine can fire from the queue."""
    assert set(QUEUED) == set(ACTION_NAMES)


def test_immediate_binding_covers_exactly_the_named_actions():
    """npc/strategy.py: what a strategy document can order right now."""
    assert set(IMMEDIATE) == set(ACTION_NAMES)


def test_document_and_tool_whitelists_are_the_same_vocabulary():
    """A strategy author and a queue_command caller get the same answer about
    which actions and phases exist."""
    assert set(VALID_TRIGGER_PHASES) == set(TRIGGER_PHASES)


def test_upon_departure_is_a_subset_of_the_named_actions():
    """The narrow phase can only narrow -- it cannot admit an action the rest
    of the system doesn't have."""
    assert UPON_DEPARTURE_ACTIONS <= ACTION_NAMES
