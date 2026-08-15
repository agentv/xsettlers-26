"""
engine/actions.py names what an org can be ordered to do; two modules bind
those names to handlers. These tests are what stop a fifth action being added
to one binding and forgotten in the other -- the failure mode being an order a
player can queue but the engine cannot fire, or vice versa.
"""
from engine.actions import ACTION_NAMES, DURING_TRANSIT_ACTIONS, TRIGGER_PHASES
from engine.npc_script import ACTIONS as PROGRAM_ACTIONS, IMMEDIATE
from engine.ship_log import ACTIONS as QUEUED
from xsettlers_mcp.tools.organization_tools import VALID_TRIGGER_PHASES


def test_queued_binding_covers_exactly_the_named_actions():
    """engine/ship_log.py: what the turn engine can fire from the queue."""
    assert set(QUEUED) == set(ACTION_NAMES)


def test_immediate_binding_covers_exactly_the_named_actions():
    """engine/npc_script.py: what a program can order right now."""
    assert set(IMMEDIATE) == set(ACTION_NAMES)


def test_program_and_tool_whitelists_are_the_same_vocabulary():
    """A program author and a queue_command caller get the same answer about
    which actions exist."""
    assert set(PROGRAM_ACTIONS) == set(ACTION_NAMES)
    assert set(VALID_TRIGGER_PHASES) == set(TRIGGER_PHASES)


def test_during_transit_is_a_subset_of_the_named_actions():
    """The narrow phase can only narrow -- it cannot admit an action the rest
    of the system doesn't have."""
    assert DURING_TRANSIT_ACTIONS <= ACTION_NAMES
