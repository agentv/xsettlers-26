POD_PRODUCTION = {
    "produce_energy": {"energy": 10.0},
    "produce_food":   {"food":   10.0},
    "produce_goods":  {"goods":   5.0},
    # Non-producing missions
    "idle": {}, "scan": {},
}

def get_production(mission: str) -> dict:
    return POD_PRODUCTION.get(mission, {})

def get_consumption(pod) -> dict:
    return {"energy": pod["energy_consumption"], "food": pod["food_consumption"]}
