POD_PRODUCTION = {
    "produce_energy": {"energy": 6.0},
    "produce_food":   {"food":   5.0},
    "produce_goods":  {"goods":  2.0},
    # Non-producing tasks
    "idle": {}, "scan": {},
}

# What a colony gains over a ship: every pod aboard a colony produces this
# multiple of its base rate. Ships get 1.0. This is the entire mechanical
# payoff for colonizing -- without it a colony would be a ship that had
# thrown away its mobility for nothing. Stability pays; that advantage is the
# counterweight to the transit stress a mobile fleet lives with.
#
# Deliberately output-only: POD_CONSUMPTION_RECIPE and ORG_UPKEEP_COST are
# untouched, so a colony pays exactly what a ship pays for the same tasking
# and simply gets more back. The multiplier applies to the sector draw too
# (see engine/turn.py) -- a colony harvesting energy strips its sector 1.5x
# as fast, so the reward carries its own clock.
COLONY_PRODUCTION_MULTIPLIER = 1.5

# One-time energy price to convert a ship into a colony, charged in full at
# the moment the player commits (see organization_tools.set_mission), not
# spread across the 3-turn transition. Unlike every other cost in the
# economy this is an all-or-nothing gate rather than a prorated draw: an org
# cannot half-become a colony, so a partial payment has nothing to buy.
# A ship without this much energy pooled across its pods is refused.
#
# 30 is provisional, not tuned -- it establishes that colonizing has a price
# at all, and should move once there is play data on what a colony is worth.
COLONIZATION_ENERGY_COST = 30.0

# Only energy is harvested from a sector -- produce_energy is capped by
# whatever the sector has left (see engine/turn.py's production step), not
# just the pod's own storage_capacity. Food and goods are not sector-sourced
# at all (see POD_CONSUMPTION_RECIPE below): they are manufactured from other
# already-stored resources.
RESOURCE_CAPACITY_COLUMN = {
    "energy": "energy_capacity",
}

# Input cost to run each producing task, drawn from the org's own pooled
# stock of that resource across ALL its pods (see RESOURCE_STORAGE_COLUMN --
# storage is generic per pod, independent of that pod's current mission, so
# retasking a pod never hides what it already has stored). Output is
# prorated to whatever fraction of the required input is actually available,
# same graceful-degradation pattern as sector depletion: e.g. only half the
# required energy on hand gives half the normal output, rather than an
# all-or-nothing gate.
POD_CONSUMPTION_RECIPE = {
    "produce_energy": {"food": 1.0},
    "produce_goods":  {"energy": 3.0, "food": 1.0},
    "produce_food":   {"energy": 1.0, "goods": 1.0},
    "scan":           {"food": 1.0, "energy": 2.0},
    # idle costs nothing -- a pod not doing anything has no upkeep of its own.
}

# Per-organization upkeep, once per turn (not per pod) -- every ship/colony
# costs this to keep running at all, on top of whatever its individual pods
# cost. The energy figure is what makes territory worth holding: priced too
# low, energy supply outruns demand and a stripped sector costs a player
# nothing. Applies regardless of transit state, same pooled/prorated draw as
# pod recipes (see engine/turn.py's _apply_org_upkeep).
ORG_UPKEEP_COST = {"food": 5.0, "energy": 3.0}

# Which pod column holds a resource's stock. Storage is per-pod but generic
# -- a pod can hold a mix of resource types regardless of its current
# task, since retasking never clears or relabels existing cargo (see
# engine/org_resources.py).
RESOURCE_STORAGE_COLUMN = {
    "energy": "energy_stored",
    "food":   "food_stored",
    "goods":  "goods_stored",
}

# Which task produces a given resource -- used only at bootstrap, to seed
# a freshly created pod's starting cargo into the column matching its
# initial task (see db/bootstrap.py's _starting_cargo_for_task). Not used
# for run-time availability/consumption -- that's keyed by
# RESOURCE_STORAGE_COLUMN alone, independent of current task.
RESOURCE_PRODUCING_TASK = {
    "energy": "produce_energy",
    "food":   "produce_food",
    "goods":  "produce_goods",
}

def get_production(task: str) -> dict:
    return POD_PRODUCTION.get(task, {})

def get_production_multiplier(org_type: str) -> float:
    """Output multiplier for a pod aboard an org of this type -- the single
    place the colony bonus is decided, so the live engine (engine/turn.py)
    and the nameplate figure players see (organization_reports._org_production)
    can never drift apart."""
    return COLONY_PRODUCTION_MULTIPLIER if org_type == "colony" else 1.0

def get_consumption_recipe(task: str) -> dict:
    return POD_CONSUMPTION_RECIPE.get(task, {})
