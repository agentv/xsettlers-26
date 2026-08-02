POD_PRODUCTION = {
    "produce_energy": {"energy": 6.0},
    "produce_food":   {"food":   5.0},
    "produce_goods":  {"goods":  3.0},
    # Non-producing tasks
    "idle": {}, "scan": {},
}

# Only energy is harvested from a sector -- produce_energy is capped by
# whatever the sector has left (see engine/turn.py's production step), not
# just the pod's own storage_capacity. Food/goods are no longer sector-
# sourced at all (see POD_CONSUMPTION_RECIPE below): they're manufactured
# from other already-stored resources instead.
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
    "produce_goods":  {"energy": 2.0, "food": 1.0},
    "produce_food":   {"energy": 1.0, "goods": 1.0},
    "scan":           {"food": 1.0, "energy": 2.0},
    # idle costs nothing -- a pod not doing anything has no upkeep of its own.
}

# Per-organization upkeep, once per turn (not per pod) -- every ship/colony
# costs this to keep running at all, on top of whatever its individual pods
# cost. Applies regardless of transit state, same pooled/prorated draw as
# pod recipes (see engine/turn.py's _apply_org_upkeep).
ORG_UPKEEP_COST = {"food": 5.0, "energy": 1.0}

# Which pod column holds a resource's stock. Storage is per-pod but generic
# -- a pod can hold a mix of resource types regardless of its current
# task, since retasking never clears or relabels existing cargo (see
# engine/turn.py's _store_org_resource/_available_org_resource).
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

def get_consumption_recipe(task: str) -> dict:
    return POD_CONSUMPTION_RECIPE.get(task, {})
